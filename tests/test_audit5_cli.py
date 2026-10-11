"""Audit round 5, batch C: snapshots, the CLI, run, validate and check
(R1, R2, R3, R4, R5, R9, R10, R16). Where the finding came from the real
command line, the test drives the installed ``diffcone`` executable in a
subprocess, as the reviewer's probes did."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from diffcone.snapshot import read_snapshot
from diffcone.testing import FixtureRepo, asv_target, reason, rules, selected

PY = sys.executable
DIFFCONE = shutil.which("diffcone", path=str(Path(sys.executable).parent)) or str(
    Path(sys.executable).parent / "diffcone"
)
PYTEST = f"{PY} -m pytest"

OPS = "def add(a, b):\n    return a + b\n"
OPS_BROKEN = "def add(a, b):\n    return a + b + 1\n"
TEST_OPS = "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
# ASV benchmarks beside the tests, so every scenario mixes both runners.
BENCHES = {
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": (
        "from pkg.ops import add\n\n\nclass Ops:\n    def time_add(self):\n        add(1, 1)\n"
    ),
}
OPS_BENCH = asv_target("Ops.time_add", "benchmarks.bench.Ops.time_add", "pkg.ops.add")


def _cli(repo: FixtureRepo, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DIFFCONE, *args], cwd=cwd or repo.path, capture_output=True, text=True, check=False
    )


def _pytest(repo: FixtureRepo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )


def _write(repo: FixtureRepo, rel: str, content: str) -> None:
    path = repo.path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8", newline="")


# --------------------------------------------------------------------------- R1


def test_r1_ignored_python_file_under_the_roots_is_read_by_worktree_plans(repo):
    """A git-ignored ``.py`` beside tracked code (a ``local_settings.py``, a
    generated ``_version.py``) is what runs on disk: the WORKTREE snapshot
    reads it, a change to it selects its importers, ``run`` executes them,
    and a committed plan refuses to run while it is on disk. An ignored
    directory (a build tree, a virtualenv) is still skipped whole."""
    base = repo.commit(
        {
            ".gitignore": "pkg/local.py\nbuild/\n",
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "tests/__init__.py": "",
            "tests/test_ops.py": TEST_OPS,
            "tests/test_local.py": (
                "from pkg.local import limit\n\n\ndef test_local():\n    assert limit() == 1\n"
            ),
            **BENCHES,
            "benchmarks/bench_local.py": (
                "from pkg.local import limit\n\n\nclass Local:\n    def time_limit(self):\n"
                "        limit()\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": OPS_BROKEN})
    _write(repo, "pkg/local.py", "def limit():\n    return 2\n")
    _write(repo, "build/lib/built.py", "def built():\n    return 1\n")
    assert repo.git("status", "--porcelain", "--ignored").splitlines() == [
        "!! build/",
        "!! pkg/local.py",
    ]
    snap = read_snapshot(repo.path, "WORKTREE", ["."], with_config=True)
    assert "pkg/local.py" in snap.files
    assert not any(p.startswith("build/") for p in snap.files)
    assert not any(p.startswith("build/") for p in snap.python_paths)

    benches = [
        asv_target("Local.time_limit", "benchmarks.bench_local.Local.time_limit"),
        OPS_BENCH,
    ]
    plan = repo.plan(head, "WORKTREE", benches, discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_local.py::test_local", "Local.time_limit"}
    # The import that resolved to nothing at HEAD now resolves: a new dependency.
    assert reason(plan, "tests/test_local.py::test_local").changes == ("dependencies_added",)

    # run executes the selected test from the plan of the working tree.
    proc = _cli(
        repo,
        "run",
        "--base",
        head,
        "--head",
        "WORKTREE",
        "--discover",
        "pytest",
        "--command",
        PYTEST,
        "--",
        "-q",
    )
    assert proc.returncode == 1, proc.stderr
    assert "test_local" in proc.stdout and "1 failed, 1 deselected" in proc.stdout

    # A plan of the commit cannot see the file: run refuses while it is there.
    proc = _cli(
        repo,
        "run",
        "--base",
        base,
        "--head",
        "HEAD",
        "--discover",
        "pytest",
        "--command",
        PYTEST,
        "--dry-run",
    )
    assert proc.returncode == 2
    assert "pkg/local.py" in proc.stderr and "--allow-mismatched-worktree" in proc.stderr


# --------------------------------------------------------------------------- R2


@pytest.mark.skipif(os.name == "nt", reason="a file: URL submodule")
def test_r2_mismatch_check_covers_every_file_the_plan_selects_on(repo, tmp_path):
    """``run`` of a committed plan refuses any working-tree difference the
    planner would have selected on: a data file or configuration under
    the roots, the runners' configuration, build inputs and the
    declarations file anywhere, a dirty submodule; not a README, not
    bytecode. The message names the files."""
    sub = FixtureRepo(tmp_path / "sub")
    sub.commit({"data.txt": "one\n"})
    repo.commit(
        {
            "src/pkg/__init__.py": "",
            "src/pkg/ops.py": OPS,
            "tests/__init__.py": "",
            "tests/test_ops.py": TEST_OPS,
            "tests/data.json": '{"v": 1}\n',
            "pytest.ini": "[pytest]\npythonpath = src\n",
            "requirements.txt": "pytest\n",
            "README.md": "readme\n",
        }
    )
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", str(sub.path), "tests/vendor")
    base = repo.commit({}, "add submodule")
    head = repo.commit({"src/pkg/ops.py": OPS_BROKEN})
    args = [
        "run",
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
        "--source-root",
        "src",
        "--source-root",
        "tests",
        "--command",
        PYTEST,
        "--dry-run",
    ]
    assert _cli(repo, *args).returncode == 0

    def refused(*paths: str) -> None:
        proc = _cli(repo, *args)
        assert proc.returncode == 2, proc.stderr
        assert "would select on" in proc.stderr
        for path in paths:
            assert path in proc.stderr, proc.stderr
        assert _cli(repo, *args, "--allow-mismatched-worktree").returncode == 0

    # A data file under the roots (the planner selects everything on it).
    _write(repo, "tests/data.json", '{"v": 2}\n')
    refused("tests/data.json")
    repo.git("checkout", "--", "tests/data.json")
    # The runner's configuration and a build input, outside every root.
    _write(repo, "pytest.ini", "[pytest]\npythonpath = src\naddopts = -q\n")
    _write(repo, "requirements.txt", "pytest\nnumpy\n")
    _write(repo, "diffcone.toml", "")
    refused("pytest.ini", "requirements.txt", "diffcone.toml")
    repo.git("checkout", "--", "pytest.ini", "requirements.txt")
    (repo.path / "diffcone.toml").unlink()
    # Edits inside a submodule under the roots, which git diff does not show.
    _write(repo, "tests/vendor/data.txt", "two\n")
    refused("tests/vendor")
    subprocess.run(
        ["git", "checkout", "--", "data.txt"], cwd=repo.path / "tests/vendor", check=True
    )
    _write(repo, "tests/vendor/new.txt", "x\n")
    refused("tests/vendor")
    (repo.path / "tests/vendor/new.txt").unlink()
    # What nobody runs does not count.
    _write(repo, "README.md", "changed\n")
    _write(repo, "src/pkg/__pycache__/ops.cpython-313.pyc", "")
    _write(repo, ".diffcone/notes", "")
    assert _cli(repo, *args).returncode == 0


# --------------------------------------------------------------------------- R3

SHARED_STATE = {
    "pkg/__init__.py": "",
    "pkg/ops.py": "def compute():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/shared.py": "STATE = {}\n",
    "tests/test_0_broken.py": "def test_0():\n    assert False\n",
    "tests/test_a.py": (
        "from pkg.ops import compute\nfrom tests import shared\n\n\n"
        "def test_a():\n    shared.STATE['v'] = compute()\n"
    ),
    "tests/test_b.py": (
        "from tests import shared\n\n\ndef test_b():\n    assert shared.STATE.get('v', 1) == 1\n"
    ),
}


def _validate(repo: FixtureRepo, base: str, head: str) -> subprocess.CompletedProcess:
    return _cli(
        repo,
        "validate",
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
        "--command",
        PYTEST,
        "--format",
        "json",
    )


@pytest.mark.parametrize("addopts", ["-x", "--maxfail=1"])
def test_r3_validate_runs_the_whole_suite_despite_a_configured_early_stop(repo, addopts):
    """A test failing at both snapshots with ``-x`` in ``addopts`` used to
    stop both suites before the test whose outcome changed, and the oracle
    said OK. The whole suite runs (``--maxfail=0`` last on the command
    line), so the miss is reported."""
    base = repo.commit({**SHARED_STATE, "pytest.ini": f"[pytest]\naddopts = {addopts}\n"})
    head = repo.commit({"pkg/ops.py": "def compute():\n    return 2\n"})
    proc = _validate(repo, base, head)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert [o["runner_id"] for o in verdict["missed"]] == ["tests/test_b.py::test_b"]


def test_r3_validate_refuses_a_suite_that_stopped_before_reporting_every_test(repo):
    """A session that stops in a way no command-line option overrides (a
    conftest setting ``maxfail``) reports outcomes for fewer tests than it
    collected: refused (exit 2), never compared."""
    conftest = "def pytest_configure(config):\n    config.option.maxfail = 1\n"
    base = repo.commit({**SHARED_STATE, "conftest.py": conftest})
    head = repo.commit({"pkg/ops.py": "def compute():\n    return 2\n"})
    proc = _validate(repo, base, head)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "stopped early" in proc.stderr
    assert "collected 3 test(s) but reported outcomes for 1" in proc.stderr


# --------------------------------------------------------------------------- R4

MONOREPO = {
    "README.md": "monorepo\n",
    "python/pytest.ini": "[pytest]\ntestpaths = tests\n",
    "python/pkg/__init__.py": "",
    "python/pkg/ops.py": OPS,
    "python/tests/__init__.py": "",
    "python/tests/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n\n\n"
        "def test_other():\n    assert True\n"
    ),
}
ADD, OTHER = "python/tests/test_ops.py::test_add", "python/tests/test_ops.py::test_other"


def _check(repo: FixtureRepo, plan: Path, full: Path, *extra: str) -> tuple[int, dict]:
    proc = _cli(repo, "check", "--plan", str(plan), "--full", str(full), "--format", "json", *extra)
    return proc.returncode, json.loads(proc.stdout)


@pytest.mark.parametrize(
    "command",
    [PYTEST, f"{PYTEST} -c python/pytest.ini --rootdir python"],
    ids=["found", "given"],
)
def test_r4_rootdir_below_the_repository_root(repo, tmp_path, command):
    """pytest's rootdir is ``python/`` (its ini file is there), so JUnit
    class names and node ids are relative to it while the plan names tests
    relative to the repository. ``check`` maps through the rootdir the
    plan's discovery determined, and ``validate`` compares
    repository-relative ids: no false miss, no false verdict."""
    base = repo.commit(MONOREPO)
    head = repo.commit({"python/pkg/ops.py": OPS_BROKEN})
    plan = tmp_path / "plan.json"
    proc = _cli(
        repo,
        "plan",
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
        "--source-root",
        "python",
        "--command",
        command,
        "-o",
        str(plan),
        "--",
        "python",
    )
    assert proc.returncode == 3, proc.stderr  # the nested ini is reported, not read
    data = json.loads(plan.read_text("utf-8"))
    assert [t["runner_id"] for t in data["selected_targets"]] == [ADD]
    config = data["discovery"][0]["config"]
    assert config["rootdir"] == "python" and config["configfile"] == "python/pytest.ini"
    notes = {n["kind"] for n in data["discovery"][0]["notes"]}
    assert "unmodelled_runner_option" in notes

    full = tmp_path / "full.xml"
    _pytest(repo, *command.split()[3:], "python", f"--junitxml={full}")
    assert 'classname="tests.test_ops"' in full.read_text("utf-8")
    code, verdict = _check(repo, plan, full)
    assert code == 0, verdict
    assert verdict["full_run"]["unmatched_cases"] == 0
    assert verdict["mapping"] == {"rootdir": "python", "junit_prefix": "", "note": None}
    assert [(f["test"], f["kind"], f["selected"]) for f in verdict["failures"]] == [
        (ADD, "test", True)
    ]
    # Mapped with the wrong rootdir, nothing matches and the report says so.
    code, verdict = _check(repo, plan, full, "--rootdir", ".")
    assert code == 1 and verdict["full_run"]["unmatched_cases"] == 2
    assert "--rootdir" in verdict["mapping"]["note"]

    proc = _cli(
        repo,
        "validate",
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
        "--source-root",
        "python",
        "--command",
        f"{command} python",
        "--format",
        "json",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert {o["runner_id"] for o in verdict["outcomes"]} == {ADD, OTHER}
    assert [(o["runner_id"], o["head"]) for o in verdict["caught"]] == [(ADD, "FAILED")]
    assert verdict["missed"] == []


# --------------------------------------------------------------------------- R5

COLLECTION = {
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_top.py": "def test_top():\n    assert True\n",
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef one():\n    return 1\n",
    "tests/sub/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add(one):\n    assert add(one, 1) == 2\n"
    ),
}
SUB_ADD, TOP = "tests/sub/test_ops.py::test_add", "tests/test_top.py::test_top"
# The fixture's body changes (which selects its tests) and the conftest no
# longer imports (an added import alone is additive by the documented rule).
BROKEN_CONFTEST = {
    "tests/sub/conftest.py": (
        "import pytest\nimport nosuchmodule\n\n\n@pytest.fixture\ndef one():\n    return 2\n"
    )
}


def _plan_file(repo: FixtureRepo, base: str, head: str, out: Path, *extra: str) -> dict:
    proc = _cli(
        repo, "plan", "--base", base, "--head", head, "--discover", "pytest", "-o", str(out), *extra
    )
    assert proc.returncode in (0, 3), proc.stderr
    return json.loads(out.read_text("utf-8"))


def _unselect(plan_path: Path, *tests: str) -> None:
    data = json.loads(plan_path.read_text("utf-8"))
    moved = [t for t in data["selected_targets"] if t["runner_id"] in tests]
    data["selected_targets"] = [t for t in data["selected_targets"] if t not in moved]
    data["unselected_targets"] += moved
    plan_path.write_text(json.dumps(data), "utf-8")


def test_r5_a_directory_that_fails_to_collect_is_a_collection_failure_of_its_tests(repo, tmp_path):
    """A ``conftest.py`` that no longer imports makes its directory a
    collector error (``classname="" name="tests.sub"``): a collection
    failure of every target under it, caught when any is selected, a miss
    of them all otherwise."""
    base = repo.commit(COLLECTION)
    head = repo.commit(BROKEN_CONFTEST)
    plan = tmp_path / "plan.json"
    data = _plan_file(repo, base, head, plan)
    assert [t["runner_id"] for t in data["selected_targets"]] == [SUB_ADD]
    assert [t["runner_id"] for t in data["unselected_targets"]] == [TOP]
    full = tmp_path / "full.xml"
    _pytest(repo, "--continue-on-collection-errors", f"--junitxml={full}")
    assert 'classname="" name="tests.sub"' in full.read_text("utf-8")
    code, verdict = _check(repo, plan, full)
    assert code == 0, verdict
    assert [(f["test"], f["kind"], f["selected"]) for f in verdict["failures"]] == [
        ("tests.sub", "collection", True)
    ]
    _unselect(plan, SUB_ADD)
    code, verdict = _check(repo, plan, full)
    assert code == 1 and verdict["misses"] == ["tests.sub"]
    assert verdict["failures"][0]["kind"] == "collection"


def test_r5_junit_prefix_from_the_plan_or_the_option(repo, tmp_path):
    """``--junit-prefix`` puts a prefix before every class name (alone on
    a collector error): taken off when the plan's pytest arguments carry
    it, or when ``check --junit-prefix`` says so."""
    base = repo.commit(COLLECTION)
    head = repo.commit(BROKEN_CONFTEST)
    full = tmp_path / "full.xml"
    _pytest(repo, "--continue-on-collection-errors", "--junit-prefix=ci", f"--junitxml={full}")
    xml = full.read_text("utf-8")
    assert 'classname="ci" name="tests.sub"' in xml and 'classname="ci.tests.test_top"' in xml

    carried = tmp_path / "carried.json"
    data = _plan_file(repo, base, head, carried, "--", "--junit-prefix=ci")
    assert data["discovery"][0]["config"]["junit_prefix"] == "ci"
    code, verdict = _check(repo, carried, full)
    assert code == 0 and verdict["full_run"]["unmatched_cases"] == 0
    assert verdict["mapping"]["junit_prefix"] == "ci"
    assert [(f["test"], f["kind"]) for f in verdict["failures"]] == [("tests.sub", "collection")]

    plain = tmp_path / "plain.json"
    _plan_file(repo, base, head, plain)
    code, verdict = _check(repo, plain, full, "--junit-prefix", "ci")
    assert code == 0 and verdict["full_run"]["unmatched_cases"] == 0
    # Without either, nothing matches and the report points at the option.
    code, verdict = _check(repo, plain, full)
    assert code == 1 and verdict["full_run"]["unmatched_cases"] == 2
    assert "--junit-prefix" in verdict["mapping"]["note"]


# --------------------------------------------------------------------------- R9


@pytest.mark.skipif(os.name == "nt", reason="no executable bit")
def test_r9_a_file_mode_change_is_a_change(repo):
    """``chmod +x`` on a script a test runs by path flips the test; the
    mode is part of the file's identity in commits and the working tree,
    so the change selects what a content change would."""
    script = "#!/bin/sh\necho hi\n"
    test = (
        "import pathlib\nimport subprocess\n\n\ndef test_run():\n"
        "    p = pathlib.Path(__file__).parent.parent / 'tools' / 'hello.sh'\n"
        "    assert subprocess.run([str(p)], capture_output=True, text=True).stdout == 'hi\\n'\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "tools/hello.sh": script,
            "tools/hello.py": "#!/usr/bin/env python3\nprint('hi')\n",
            "tests/__init__.py": "",
            "tests/test_run.py": test,
            "tests/test_ops.py": TEST_OPS,
            **BENCHES,
        }
    )
    benches = [OPS_BENCH]
    repo.git("update-index", "--chmod=+x", "tools/hello.sh")
    (repo.path / "tools/hello.sh").chmod(0o755)
    head = repo.commit({}, "chmod +x")
    assert repo.git("diff", "--name-only", base, head) == "tools/hello.sh"
    plan = repo.plan(base, head, benches, discover_runners=["pytest"])
    # The index cannot say who runs the script: everything, by the rule
    # for files it does not read.
    assert selected(plan) == {
        "tests/test_run.py::test_run",
        "tests/test_ops.py::test_add",
        "Ops.time_add",
    }
    assert (
        "tools/hello.sh"
        in reason(plan, "tests/test_run.py::test_run", "unanalysed_file_changed").detail
    )
    # The same for a Python file, and on disk (WORKTREE) as well as committed.
    (repo.path / "tools/hello.py").chmod(0o755)
    plan = repo.plan(head, "WORKTREE", benches, discover_runners=["pytest"])
    assert selected(plan) == {
        "tests/test_run.py::test_run",
        "tests/test_ops.py::test_add",
        "Ops.time_add",
    }
    assert "tools/hello.py" in reason(plan, "Ops.time_add", "unanalysed_file_changed").detail
    (repo.path / "tools/hello.py").chmod(0o644)
    assert selected(repo.plan(head, "WORKTREE", benches, discover_runners=["pytest"])) == set()


# --------------------------------------------------------------------------- R10


def test_r10_a_tracked_file_replaced_by_a_directory_on_disk(repo):
    """``git hash-object`` cannot hash a directory: the WORKTREE plan used to
    crash. The entry is a deletion (and the files inside are new untracked
    files), which selects the test reading it."""
    test = (
        "import pathlib\n\n\ndef test_data():\n"
        "    assert (pathlib.Path(__file__).parent / 'data').read_text() == 'x\\n'\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "tests/__init__.py": "",
            "tests/data": "x\n",
            "tests/test_data.py": test,
            "tests/test_ops.py": TEST_OPS,
            **BENCHES,
        }
    )
    (repo.path / "tests/data").unlink()
    (repo.path / "tests/data").mkdir()
    (repo.path / "tests/data/x.txt").write_text("y\n", "utf-8")
    plan = repo.plan(base, "WORKTREE", [OPS_BENCH], discover_runners=["pytest"])
    assert selected(plan) == {
        "tests/test_data.py::test_data",
        "tests/test_ops.py::test_add",
        "Ops.time_add",
    }
    detail = reason(plan, "tests/test_data.py::test_data", "unanalysed_file_changed").detail
    assert "tests/data" in detail
    assert not plan.errors


# --------------------------------------------------------------------------- R16


def test_r16_run_names_the_option_that_kept_selected_tests_out(repo):
    """``addopts = --lf`` with a last-failed cache makes pytest skip the
    files of tests that passed last time: a selected test is then not
    collected, and the message names the option and where it is set
    instead of blaming discovery alone."""
    base = repo.commit(
        {
            "pytest.ini": "[pytest]\naddopts = --lf\n",
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "tests/__init__.py": "",
            "tests/test_a.py": TEST_OPS,
            "tests/test_b.py": (
                "import os\n\nfrom pkg.ops import add\n\n\ndef test_b():\n"
                "    assert os.environ.get('FLAKY') != '1'\n    assert add(2, 2) == 4\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    # A full run leaves test_b in the last-failed cache; test_a passed.
    proc = subprocess.run(
        [PY, "-m", "pytest", "-q"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        env={**os.environ, "FLAKY": "1"},
    )
    assert "1 failed, 1 passed" in proc.stdout, proc.stdout
    proc = _cli(
        repo,
        "run",
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
        "--command",
        PYTEST,
        "--",
        "-q",
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "did not collect 1 selected target(s)" in proc.stderr
    assert "tests/test_a.py::test_add" in proc.stderr
    assert "--lf (pytest.ini: addopts)" in proc.stderr and "-o addopts=" in proc.stderr
    assert rules(
        repo.plan(base, head, [], discover_runners=["pytest"]), "tests/test_a.py::test_add"
    )

"""Execution evidence: the recorder, the store, and what collection refuses.

These run the fixture repository's suite for real under the recorder
(``diffcone collect``), so each asserts exactly what one test executed or
touched.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from diffcone import evidence as evidence_module
from diffcone.cli import main
from diffcone.evidence import (
    FLAG_SUBPROCESS,
    FLAG_UNSTABLE,
    Evidence,
    EvidenceError,
    TestRecord,
    advance,
    list_stores,
    load_store,
    write_store,
)
from diffcone.report import to_text
from diffcone.snapshot import GitError
from diffcone.testing import asv_target, executed, selected, touched

# Recording needs sys.monitoring; the store and fold tests run everywhere.
needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

OPS = """\
import json


def add(a, b):
    return a + b


def mul(a, b):
    return a * b


def load():
    with open("pkg/data.json") as f:
        return json.load(f)
"""

CONFTEST = """\
import pytest


@pytest.fixture(scope="session")
def shared():
    from pkg.ops import mul

    return mul(2, 2)
"""

TESTS = """\
import os
import subprocess
import sys

import pytest

from pkg.ops import add, load


@pytest.mark.parametrize("x", [1, 2])
def test_add(x):
    assert add(x, 1) == x + 1


def test_shared(shared):
    assert shared == 4


def test_shared_again(shared):
    assert shared == 4


def test_shared_by_name(request):
    assert request.getfixturevalue("shared") == 4


def test_load():
    assert load() == [1]


def test_exists():
    assert not os.path.exists("pkg/extra.txt")


def test_listing():
    assert "data.json" in os.listdir("pkg")


def test_subprocess():
    # Project code run from a -c snippet: flagged, the child's record or not.
    subprocess.run([sys.executable, "-c", "import pkg.ops"], check=True)
"""

FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "pkg/data.json": "[1]",
    "tests/__init__.py": "",
    "tests/conftest.py": CONFTEST,
    "tests/test_ops.py": TESTS,
}
T = "tests/test_ops.py::"


@needs_monitoring
def test_each_test_records_what_it_executed_and_touched(repo):
    repo.commit(FILES)
    ev = repo.collect()
    assert executed(ev, T + "test_add") == {"pkg.ops.add", "tests.test_ops.test_add"}
    # A session fixture's setup is credited to every test that used it, the
    # ones after the first included, and to one that asked for it by name.
    for test in ("test_shared", "test_shared_again", "test_shared_by_name"):
        assert {"pkg.ops.mul", "tests.conftest.shared"} <= executed(ev, T + test)
    assert executed(ev, T + "test_load") == {"pkg.ops.load", "tests.test_ops.test_load"}
    assert "pkg/data.json" in touched(ev, T + "test_load")
    assert "pkg/data.json" not in touched(ev, T + "test_add")
    # A stat of a file that does not exist yet, and a directory listing.
    assert "pkg/extra.txt" in touched(ev, T + "test_exists")
    assert "pkg" in ev.listed(ev.tests[T + "test_listing"])
    flags = {t: r.flags for t, r in ev.tests.items()}
    assert {t for t, f in flags.items() if f & FLAG_SUBPROCESS} == {T + "test_subprocess"}
    assert not any(f & FLAG_UNSTABLE for f in flags.values())
    assert ev.environment["variables"]["PYTHONHASHSEED"] == "0"
    assert {"pkg.ops", "tests.conftest", "tests.test_ops"} <= ev.import_phase


@needs_monitoring
def test_code_run_by_an_import_is_attributed_to_the_importing_module(repo):
    repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/registry.py": "def make():\n    return 1\n",
            "pkg/table.py": "from pkg.registry import make\n\nVALUE = make()\n",
            "tests/test_table.py": "from pkg.table import VALUE\n\n\n"
            "def test_value():\n    assert VALUE == 1\n",
        }
    )
    ev = repo.collect()
    assert ev.import_by["pkg.registry.make"] == {"pkg.table"}
    assert "pkg.registry.make" in ev.import_phase
    # It ran before the test, so it is not in the test's own record.
    assert executed(ev, "tests/test_table.py::test_value") == {"tests.test_table.test_value"}


@needs_monitoring
def test_functools_caches_are_cleared_between_tests(repo):
    repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/calc.py": "import functools\n\n\n@functools.lru_cache\n"
            "def compute():\n    return 1\n",
            "tests/test_calc.py": "from pkg.calc import compute\n\n\n"
            "def test_one():\n    assert compute() == 1\n\n\n"
            "def test_two():\n    assert compute() == 1\n",
        }
    )
    ev = repo.collect()
    assert "pkg.calc.compute" in executed(ev, "tests/test_calc.py::test_one")
    assert "pkg.calc.compute" in executed(ev, "tests/test_calc.py::test_two")


@needs_monitoring
def test_a_record_that_depends_on_test_order_is_unstable(repo):
    repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/memo.py": "_VALUE = None\n\n\ndef compute():\n    return 1\n\n\n"
            "def get():\n    global _VALUE\n    if _VALUE is None:\n"
            "        _VALUE = compute()\n    return _VALUE\n",
            "pkg/plain.py": "def f():\n    return 2\n",
            "tests/test_memo.py": "from pkg.memo import get\nfrom pkg.plain import f\n\n\n"
            "def test_one():\n    assert get() == 1\n\n\n"
            "def test_two():\n    assert get() == 1\n\n\n"
            "def test_plain():\n    assert f() == 2\n",
        }
    )
    ev = repo.collect(reverse_check=True)
    unstable = {t for t, r in ev.tests.items() if r.flags & FLAG_UNSTABLE}
    assert unstable == {"tests/test_memo.py::test_one", "tests/test_memo.py::test_two"}
    # The record is the union of both orders.
    assert "pkg.memo.compute" in executed(ev, "tests/test_memo.py::test_two")
    assert ev.reverse_checked


@needs_monitoring
def test_the_store_round_trips(repo, tmp_path):
    repo.commit(FILES)
    ev = repo.collect()
    again = load_store(write_store(ev, tmp_path / "stores"))
    assert again.commit == ev.commit
    assert again.environment == ev.environment
    assert again.environment_hash == ev.environment_hash
    assert {t: (again.executed(r), again.touched(r), r.flags) for t, r in again.tests.items()} == {
        t: (ev.executed(r), ev.touched(r), r.flags) for t, r in ev.tests.items()
    }
    assert again.import_by == ev.import_by
    assert again.import_phase == ev.import_phase
    assert again.import_paths == ev.import_paths
    assert [s.commit for s in list_stores(repo.path)] == [ev.commit]


@needs_monitoring
def test_a_store_write_that_fails_leaves_nothing_behind(repo, tmp_path, monkeypatch):
    repo.commit(FILES)
    ev = repo.collect()
    stores = tmp_path / "stores"

    def fail(db, evidence):
        db.execute("CREATE TABLE meta (key TEXT)")
        raise OSError("disk full")

    monkeypatch.setattr(evidence_module, "_write_tables", fail)
    with pytest.raises(OSError, match="disk full"):
        write_store(ev, stores)
    assert list(stores.iterdir()) == []


@needs_monitoring
def test_collection_at_an_older_commit_uses_a_temporary_worktree(repo):
    first = repo.commit(FILES)
    repo.commit({"pkg/ops.py": OPS + "\n\ndef extra():\n    return 3\n"})
    (repo.path / "pkg" / "ops.py").write_text("broken(", "utf-8")  # a dirty tree is fine here
    ev = repo.collect(first)
    assert ev.commit == first
    assert executed(ev, T + "test_add") == {"pkg.ops.add", "tests.test_ops.test_add"}


@needs_monitoring
def test_the_record_is_finished_before_the_session_reports_done(repo):
    # pytest-xdist reports a worker done after its session finishes and then
    # kills workers that take more than ten seconds to exit, so the record
    # must not wait for unconfigure. This conftest's unconfigure runs before
    # the recorder's (pluggy calls later registrations first).
    conftest = (
        CONFTEST
        + """

def pytest_unconfigure(config):
    import os

    out = os.environ["DIFFCONE_COLLECT_OUT"]
    assert os.path.exists(os.path.join(out, f"process-{os.getpid()}.json"))
"""
    )
    repo.commit({**FILES, "tests/conftest.py": conftest})
    ev = repo.collect()
    assert executed(ev, T + "test_add") == {"pkg.ops.add", "tests.test_ops.test_add"}


@needs_monitoring
def test_a_dirty_tree_is_refused(repo):
    repo.commit(FILES)
    (repo.path / "pkg" / "ops.py").write_text(OPS + "\n# edited\n", "utf-8")
    with pytest.raises(GitError, match="evidence describes a commit"):
        repo.collect()


@needs_monitoring
def test_a_suite_that_runs_an_installed_copy_is_refused(repo, tmp_path):
    installed = tmp_path / "site"
    (installed / "pkg").mkdir(parents=True)
    (installed / "pkg" / "__init__.py").write_text("", "utf-8")
    (installed / "pkg" / "ops.py").write_text(OPS, "utf-8")
    repo.commit(
        {
            **FILES,
            # The copy is imported first, as an installed package would be.
            "tests/conftest.py": f"import sys\n\nsys.path.insert(0, {str(installed)!r})\n"
            "import pkg.ops  # noqa: E402\n\n" + CONFTEST,
        }
    )
    with pytest.raises(EvidenceError, match="installed copy"):
        repo.collect()


@needs_monitoring
def test_e30_collect_warns_when_no_recorded_test_is_a_target(repo, capsys):
    """pytest takes a configuration file in a directory above a repository
    that has none as its rootdir, and every recorded node id carries the
    repository's own directory: plans then find no record for any test,
    silently. ``collect`` says so."""
    repo.commit(FILES)
    command = f"{sys.executable} -m pytest"

    def collect(command: str) -> str:
        argv = ["collect", "--repo", str(repo.path), "--command", command, "--no-cache"]
        assert main(argv) == 0
        return capsys.readouterr().err

    assert "none of the" not in collect(command)
    (repo.path.parent / "pytest.ini").write_text("[pytest]\n")
    err = collect(command)
    assert "none of the 8 recorded tests (e.g. repo/tests/" in err
    assert "pass --rootdir=. in --command" in err
    assert "none of the" not in collect(f"{command} --rootdir=.")


@needs_monitoring
def test_collect_and_evidence_commands(repo, capsys):
    repo.commit(FILES)
    command = f"{sys.executable} -m pytest"
    assert main(["collect", "--repo", str(repo.path), "--command", command, "--no-cache"]) == 0
    out, err = capsys.readouterr()
    assert out.strip().endswith(".sqlite")
    assert "recorded 8 tests" in err
    assert "1 started a subprocess" in err
    assert main(["evidence", "--repo", str(repo.path)]) == 0
    assert "8 tests" in capsys.readouterr().out


def _edited_add(repo):
    repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    return ev, head


@needs_monitoring
def test_run_with_evidence_runs_only_the_tests_that_executed_the_change(repo, capfd):
    ev, head = _edited_add(repo)
    command = f"{sys.executable} -m pytest"
    args = ["run", "--repo", str(repo.path), "--base", ev.commit, "--head", head]
    args += ["--discover", "pytest", "--command", command, "--evidence", "auto", "--no-cache"]
    assert main(args + ["--", "-q"]) == 0
    out, err = capfd.readouterr()
    assert "2 of 8 pytest target(s) selected" in err  # test_add, and test_subprocess
    assert "3 passed" in out  # test_add's two cases and test_subprocess
    assert "environment differs" not in err


@needs_monitoring
def test_run_falls_back_to_static_planning_in_another_environment(repo, capsys, monkeypatch):
    from diffcone import execution
    from diffcone.testing import selected

    ev, head = _edited_add(repo)
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {T + "test_add", T + "test_subprocess"}
    plan.evidence["environment_hash"] = "0" * 16  # recorded somewhere else
    static = repo.plan(ev.commit, head, [], discover_runners=["pytest"])
    run = execution.run_with_evidence(
        plan, lambda: static, cwd=repo.path, command=f"{sys.executable} -m pytest", extra=["-q"]
    )
    assert run.mismatch is not None and run.mismatch["python"] == ev.environment["python"]
    assert run.result.returncode != 0  # stopped before any test ran
    assert run.static is not None and run.static.returncode == 0
    assert {t.runner_id for t in run.static.selected} == selected(static)


@needs_monitoring
def test_a_project_variable_is_part_of_the_environment(repo, monkeypatch):
    """``collect --env-var`` (roadmap item 9): pandas' PANDAS_FUTURE changes
    what tests do, so a run with another value uses no evidence."""
    from diffcone import execution

    monkeypatch.setenv("DIFFCONE_TEST_FLAG", "on")
    repo.commit(FILES)
    ev = repo.collect(env_variables=["DIFFCONE_TEST_FLAG"])
    assert ev.environment["variables"]["DIFFCONE_TEST_FLAG"] == "on"
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert "DIFFCONE_TEST_FLAG" in plan.evidence["variables"]
    static = repo.plan(ev.commit, head, [], discover_runners=["pytest"])
    command = f"{sys.executable} -m pytest"

    def run():
        return execution.run_with_evidence(
            plan, lambda: static, cwd=repo.path, command=command, extra=["-q"]
        )

    assert run().mismatch is None
    monkeypatch.setenv("DIFFCONE_TEST_FLAG", "off")
    checked = run()
    assert checked.mismatch is not None
    assert checked.mismatch["variables"]["DIFFCONE_TEST_FLAG"] == "off"


INSTALLS = (
    "import os\n\n\ndef test_installs():\n"
    "    info = os.path.join(os.environ['DIFFCONE_TEST_SITE'], 'fakepkg-1.0.dist-info')\n"
    "    os.makedirs(info, exist_ok=True)\n"
    "    with open(os.path.join(info, 'METADATA'), 'w') as f:\n"
    "        f.write('Metadata-Version: 2.1\\nName: fakepkg\\nVersion: 1.0\\n')\n"
)


@needs_monitoring
def test_a_test_run_that_changes_its_own_environment_is_named(repo, tmp_path, monkeypatch, capfd):
    """Roadmap item 12: strata's tests upgraded orjson while recording, so
    the recording never matched a fresh environment, and nothing said why."""
    import shutil
    from pathlib import Path

    from diffcone.evidence import load_store

    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setenv("DIFFCONE_TEST_SITE", str(site))
    monkeypatch.setenv("PYTHONPATH", str(site))
    base = repo.commit({**FILES, "tests/test_installs.py": INSTALLS})
    command = f"{sys.executable} -m pytest"
    assert main(["collect", "--repo", str(repo.path), "--command", command, "--no-cache"]) == 0
    out, err = capfd.readouterr()
    assert "changed its own environment" in err
    assert "installed during the run: fakepkg==1.0" in err
    store = load_store(Path(out.strip()))
    assert store.environment_at_start is not None
    assert "fakepkg==1.0" in store.environment["distributions"]
    assert "fakepkg==1.0" not in store.environment_at_start["distributions"]

    # A fresh environment: the one the recording started in.
    shutil.rmtree(site / "fakepkg-1.0.dist-info")
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    ran = tmp_path / "ran.json"
    args = ["run", "--repo", str(repo.path), "--base", base, "--head", head, "--no-cache"]
    args += ["--discover", "pytest", "--command", command, "--evidence", "auto", "-o", str(ran)]
    main(args + ["--", "-q"])
    err = capfd.readouterr().err
    assert "the recording's own test run" in err
    unused = json.loads(ran.read_text())["evidence_not_used"]
    assert unused["reason"] == "environment"
    assert unused["recording_changed_its_environment"] is True
    assert any("fakepkg==1.0" in line for line in unused["differences"])


@needs_monitoring
def test_a_recording_that_keeps_its_environment_says_nothing(repo, capfd):
    repo.commit(FILES)
    command = f"{sys.executable} -m pytest"
    assert main(["collect", "--repo", str(repo.path), "--command", command, "--no-cache"]) == 0
    assert "changed its own environment" not in capfd.readouterr().err
    assert repo.collect().environment_at_start is None


@needs_monitoring
def test_evidence_at_a_commit_the_checkout_lacks_says_what_to_fetch(repo):
    from diffcone.evidence import find_store

    repo.commit(FILES)
    ev = repo.collect()
    absent = replace(ev, commit="0123456789abcdef0123456789abcdef01234567")
    with pytest.raises(EvidenceError, match="git fetch --depth=1 origin 0123456789ab"):
        repo.plan(ev.commit, ev.commit, [], discover_runners=["pytest"], evidence=absent)
    for path in (repo.path / ".diffcone" / "evidence").glob("*.sqlite"):
        path.unlink()
    write_store(absent, repo.path / ".diffcone" / "evidence")
    with pytest.raises(EvidenceError, match="commits this checkout does not have"):
        find_store(repo.path, "auto", ["."], ev.commit)


@needs_monitoring
def test_validate_with_evidence(repo):
    from diffcone.execution import validate_pytest

    ev, head = _edited_add(repo)
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    v = validate_pytest(plan, repo=repo.path, command=f"{sys.executable} -m pytest", coverage=True)
    assert v.ok
    assert v.coverage is not None and not v.coverage.missed


# --------------------------------------------------------------------------- completeness

HELPER = """\
class TestThing:
    class Helper:
        def test_method(self):
            pass

    def test_thing(self):
        assert self.Helper()
"""
PLUGIN = """\
import pytest


def pytest_pycollect_makeitem(collector, name, obj):
    if name.startswith("Check") and isinstance(obj, type):
        return pytest.Class.from_parent(collector, name=name, obj=obj)
"""


def _kinds(plan):
    return sorted(n.kind for n in plan.incomplete_discovery)


@needs_monitoring
def test_the_recording_settles_a_class_pytest_does_not_collect(repo):
    """Discovery cannot know no plugin collects a helper class with a
    ``test_`` method; the recording ran the real collection (roadmap item 9)."""
    repo.commit({**FILES, "tests/test_helper.py": HELPER})
    ev = repo.collect()
    assert "tests/test_helper.py::TestThing::test_thing" in ev.collected
    assert T + "test_add" in ev.collected  # parameters folded
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    static = repo.plan(ev.commit, head, [], discover_runners=["pytest"])
    assert _kinds(static) == ["uncollected_test_class"]
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert _kinds(plan) == []
    notes = [n for d in plan.discovery for n in d.notes if n.kind == "settled_by_evidence"]
    assert len(notes) == 1 and notes[0].path == "tests/test_helper.py"
    # Its file changed since the recording: it may now be collected.
    edited = repo.commit({"tests/test_helper.py": HELPER.replace("pass", "return 1")})
    plan = repo.plan(ev.commit, edited, [], discover_runners=["pytest"], evidence=ev)
    assert _kinds(plan) == ["uncollected_test_class"]
    # A store that did not record its collection settles nothing.
    plan = repo.plan(
        head, head, [], discover_runners=["pytest"], evidence=replace(ev, collected=None)
    )
    assert _kinds(plan) == ["uncollected_test_class"]


@needs_monitoring
def test_tests_a_plugin_collected_keep_the_plan_incomplete(repo):
    repo.commit(
        {
            **FILES,
            "tests/plug/__init__.py": "",
            "tests/plug/conftest.py": PLUGIN,
            "tests/plug/test_plug.py": "class CheckThing:\n    def test_x(self):\n        pass\n",
        }
    )
    ev = repo.collect()
    assert "tests/plug/test_plug.py::CheckThing::test_x" in ev.collected
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert _kinds(plan) == [
        "collected_not_target",
        "plugin_collects_files",
        "uncollected_test_class",
    ]
    (gap,) = [n for n in plan.incomplete_discovery if n.kind == "collected_not_target"]
    assert "tests/plug/test_plug.py::CheckThing::test_x" in gap.detail


@needs_monitoring
def test_an_incomplete_static_fallback_runs_the_whole_suite(repo):
    from diffcone import execution

    repo.commit({**FILES, "tests/test_helper.py": HELPER})
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert not plan.incomplete_discovery
    plan.evidence["environment_hash"] = "0" * 16  # recorded somewhere else
    static = repo.plan(ev.commit, head, [], discover_runners=["pytest"])
    command = f"{sys.executable} -m pytest"
    run = execution.run_with_evidence(
        plan, lambda: static, cwd=repo.path, command=command, extra=["-q"]
    )
    assert run.mismatch is not None and run.static_whole
    assert run.static is not None and run.static.returncode == 0
    assert len(run.static.selected) == run.static.total
    # Allowed, it runs the static selection.
    run = execution.run_with_evidence(
        plan, lambda: static, cwd=repo.path, command=command, extra=["-q"], allow_incomplete=True
    )
    assert not run.static_whole


# --------------------------------------------------------------------------- advancing


def _run_collect(repo, base, head, *extra):
    args = ["run", "--repo", str(repo.path), "--base", base, "--head", head]
    args += ["--discover", "pytest", "--command", f"{sys.executable} -m pytest"]
    return main(args + ["--evidence", "auto", "--collect", "--no-cache", *extra])


@needs_monitoring
def test_run_collect_advances_the_store_to_head(repo, capfd):
    repo.commit(FILES)
    ev = repo.collect()
    helper = OPS.replace(
        "def add(a, b):\n    return a + b",
        "def _plus(a, b):\n    return a + b\n\n\ndef add(a, b):\n    return _plus(a, b)",
    )
    head = repo.commit({"pkg/ops.py": helper})
    assert _run_collect(repo, ev.commit, head) == 0
    err = capfd.readouterr().err
    assert "2 of 8 pytest target(s) selected" in err  # test_add, and test_subprocess
    assert "2 test(s) recorded afresh" in err
    stores = {s.commit: s for s in list_stores(repo.path)}
    head_commit = repo.git("rev-parse", head).strip()
    assert set(stores) == {ev.commit, head_commit}
    assert (stores[head_commit].advanced_from, stores[head_commit].full_commit) == (
        ev.commit,
        ev.commit,
    )
    advanced = load_store(stores[head_commit].path)
    assert set(advanced.tests) == set(ev.tests)
    # The selected test's record is new; the others carry over unchanged.
    assert executed(advanced, T + "test_add") == {
        "pkg.ops._plus",
        "pkg.ops.add",
        "tests.test_ops.test_add",
    }
    for test in set(ev.tests) - {T + "test_add", T + "test_subprocess"}:
        assert executed(advanced, test) == executed(ev, test)
        assert touched(advanced, test) == touched(ev, test)
    assert advanced.tests[T + "test_subprocess"].flags & FLAG_SUBPROCESS

    # The next plan starts from the advanced store, so it sees only its own change.
    later = repo.commit(
        {"pkg/ops.py": helper.replace("return json.load(f)", "return list(json.load(f))")}
    )
    plan = repo.plan(head, later, [], discover_runners=["pytest"], evidence=advanced)
    assert plan.evidence["commit"] == head_commit
    assert selected(plan) == {T + "test_load", T + "test_subprocess"}
    assert main(["evidence", "--repo", str(repo.path)]) == 0
    assert f"advanced from {ev.commit[:12]}" in capfd.readouterr().out


@needs_monitoring
def test_run_collect_with_nothing_selected_relabels_the_store(repo, capfd):
    # A test that starts a subprocess is always selected: leave it out.
    tests = TESTS[: TESTS.index("\n\ndef test_subprocess")] + "\n"
    repo.commit({**FILES, "tests/test_ops.py": tests})
    ev = repo.collect()
    repo.git("commit", "--allow-empty", "-q", "-m", "empty")
    head = repo.git("rev-parse", "HEAD").strip()
    assert _run_collect(repo, ev.commit, head) == 0
    assert "nothing selected" in capfd.readouterr().err
    head_commit = repo.git("rev-parse", head).strip()
    (advanced,) = [s for s in list_stores(repo.path) if s.commit == head_commit]
    relabelled = load_store(advanced.path)
    assert {t: executed(relabelled, t) for t in relabelled.tests} == {
        t: executed(ev, t) for t in ev.tests
    }


@needs_monitoring
def test_run_collect_refusals(repo, capsys):
    repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    # Other pytest arguments could deselect cases the old record covered.
    assert _run_collect(repo, ev.commit, head, "--", "-q") == 2
    assert "differ from the ones the recording was made with" in capsys.readouterr().err
    (repo.path / "pkg" / "ops.py").write_text(OPS + "\n# edited\n", "utf-8")
    assert _run_collect(repo, ev.commit, "WORKTREE") == 2
    assert "a recording describes a commit" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["run", "--repo", str(repo.path), "--base", ev.commit, "--head", head])
    with pytest.raises(SystemExit):
        main(["run", "--repo", str(repo.path), "--base", "HEAD", "--head", "HEAD", "--collect"])


@needs_monitoring
def test_run_collect_in_another_environment_does_not_advance(repo):
    from diffcone import execution

    repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    plan.evidence["environment_hash"] = "0" * 16  # recorded somewhere else
    static = repo.plan(ev.commit, head, [], discover_runners=["pytest"])
    run = execution.run_with_evidence(
        plan,
        lambda: static,
        cwd=repo.path,
        command=f"{sys.executable} -m pytest",
        advance_from=ev,
    )
    assert run.mismatch is not None and run.advanced is None
    assert [s.commit for s in list_stores(repo.path)] == [ev.commit]


def _evidence(commit, tests, **process):
    symbols = sorted({s for symbols, _ in tests.values() for s in symbols})
    paths = sorted({p for _, paths in tests.values() for p in paths})
    return Evidence(
        commit=commit,
        source_roots=["."],
        environment={},
        environment_hash="e",
        command="pytest",
        created=0.0,
        symbols=symbols,
        paths=paths,
        tests={
            name: TestRecord(
                frozenset(symbols.index(s) for s in syms),
                frozenset(paths.index(p) for p in ps),
                frozenset(),
                flags,
            )
            for name, ((syms, ps), flags) in (
                (n, (v, process.get("flags", {}).get(n, 0))) for n, v in tests.items()
            )
        },
        import_phase=frozenset(process.get("import_phase", ())),
        import_by=process.get("import_by", {}),
    )


def test_advance_merges_records_by_name():
    previous = _evidence(
        "c",
        {
            "t::kept": ({"m.a", "m.b"}, {"data.json"}),
            "t::rerun": ({"m.a"}, set()),
            "t::gone": ({"m.c"}, set()),
        },
        flags={"t::rerun": FLAG_UNSTABLE},
        import_phase={"m"},
        import_by={"m.a": frozenset({"m"})},
    )
    fresh = _evidence(
        "h",
        {"t::rerun": ({"m.z"}, {"other.txt"}), "t::new": ({"m.b"}, set())},
        import_phase={"n"},
        import_by={"m.a": frozenset({"n"})},
    )
    out = advance(previous, fresh, {"t::rerun", "t::gone", "t::new"}, "h")
    # A carried record of a test that is no longer a target is dropped.
    pruned = advance(previous, fresh, {"t::rerun"}, "h", alive={"t::rerun", "t::new"})
    assert set(pruned.tests) == {"t::rerun", "t::new"}
    # Unselected: carried. Rerun: the new record, still unstable. Selected but
    # not recorded: dropped, so it has no evidence from here on.
    assert set(out.tests) == {"t::kept", "t::rerun", "t::new"}
    assert out.executed(out.tests["t::kept"]) == {"m.a", "m.b"}
    assert out.touched(out.tests["t::kept"]) == {"data.json"}
    assert out.executed(out.tests["t::rerun"]) == {"m.z"}
    assert out.touched(out.tests["t::rerun"]) == {"other.txt"}
    assert out.tests["t::rerun"].flags & FLAG_UNSTABLE
    assert out.import_phase == {"m", "n"}
    assert out.import_by == {"m.a": frozenset({"m", "n"})}
    assert (out.commit, out.advanced_from, out.full_commit) == ("h", "c", "c")
    with pytest.raises(EvidenceError, match="environment"):
        advance(previous, replace(fresh, environment_hash="x"), set(), "h")


@needs_monitoring
def test_run_collect_drops_the_records_of_deleted_tests(repo, capfd):
    repo.commit(FILES)
    ev = repo.collect()
    assert T + "test_listing" in ev.tests
    listing = '\n\ndef test_listing():\n    assert "data.json" in os.listdir("pkg")\n'
    head = repo.commit({"tests/test_ops.py": TESTS.replace(listing, "")})
    assert _run_collect(repo, ev.commit, head) == 0
    capfd.readouterr()
    head_commit = repo.git("rev-parse", head).strip()
    (store,) = [s for s in list_stores(repo.path) if s.commit == head_commit]
    advanced = load_store(store.path)
    assert set(advanced.tests) == set(ev.tests) - {T + "test_listing"}


@needs_monitoring
def test_w28_optimize_and_pythonpath_differences_are_reported():
    """``-O`` and PYTHONPATH are in the recorded environment and its hash, so
    a run that differs only there names them instead of an empty list."""
    from diffcone.collect import environment
    from diffcone.evidence import environment_differences

    recorded = environment()
    level = recorded["optimize"]
    assert environment_differences(recorded, {**recorded, "optimize": level + 1}) == [
        f"optimization level (python -O, PYTHONOPTIMIZE): {level} recorded, {level + 1} now"
    ]
    met = {**recorded, "pythonpath": [*recorded["pythonpath"], "/opt/extra"]}
    (line,) = environment_differences(recorded, met)
    assert line.startswith("PYTHONPATH entries: ") and "/opt/extra" in line


@needs_monitoring
def test_w28_every_recorded_field_has_a_difference_line():
    """Whatever field differs, the list names it; a field only one side has
    (a variable added with ``--env-var``, a later recorder's field) too."""
    from diffcone.collect import environment
    from diffcone.evidence import environment_differences

    recorded = environment()
    for key, value in recorded.items():
        if isinstance(value, list):
            changed: object = [*value, "x==1"]
        elif isinstance(value, dict):
            changed = {**value, "EXTRA_FLAG": "on"}
        else:
            changed = f"{value}-other"
        assert environment_differences(recorded, {**recorded, key: changed}), key
    assert environment_differences(recorded, {**recorded, "newfield": 1}) == [
        "newfield: nothing recorded, 1 now"
    ]


@needs_monitoring
def test_w28_a_mismatch_is_never_explained_by_an_empty_list():
    """The caller has a hash mismatch: when no field differs, the list still
    says so rather than printing nothing."""
    from diffcone.collect import environment
    from diffcone.evidence import environment_differences

    recorded = environment()
    (line,) = environment_differences(recorded, dict(recorded))
    assert "hash differs" in line


# --------------------------------------------------------------------------- environment check

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _plan(repo, base, head, evidence, targets=()):
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], evidence=evidence)


PYTEST = f"{sys.executable} -m pytest -p no:cacheprovider"
CALC = {
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a + b\n\n\ndef other():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_ops.py": (
        "import os\n\nfrom calc.ops import add, other\n\n\n"
        "def test_x():\n    if os.environ.get('FLAG') == 'on':\n        assert other() == 1\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    ),
}


@needs_monitoring
@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_r1_an_empty_evidence_selection_still_checks_the_environment(repo, monkeypatch, capsys):
    """Recorded with FLAG off, test_x never called other(); run with FLAG on,
    the evidence selects nothing for a change to other(), but it does not
    apply here: the static plan runs, and test_x fails."""
    base = repo.commit(CALC)
    monkeypatch.setenv("FLAG", "off")
    repo.collect(env_variables=["FLAG"])
    # A different size: a same-size edit within the second of the recording
    # run would reuse the stale .pyc (Python checks mtime and size).
    ops = CALC["calc/ops.py"].replace("return 1", "return 1 + 1")
    head = repo.commit({"calc/ops.py": ops})
    monkeypatch.setenv("FLAG", "on")
    code = main(
        [
            "run",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--discover",
            "pytest",
            "--evidence",
            "auto",
            "--command",
            PYTEST,
            "--",
            "-q",
        ]
    )
    err = capsys.readouterr().err
    assert "environment differs" in err
    assert code == 1  # test_x ran under the static plan, and failed


@needs_monitoring
def test_evp9_plan_says_the_environment_was_not_checked(repo):
    repo.commit(
        {
            **BASE,
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_ops.py": "from pkg.ops import add\n\n\ndef test_add():\n"
            "    assert add(1, 2) == 3\n",
        }
    )
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = _plan(repo, ev.commit, head, ev)
    assert plan.evidence["environment_checked"] is False
    assert "environment NOT checked" in to_text(plan)
    # ``run`` checks it in the test process, and says so in its report.
    out = repo.path.parent / "ran.json"
    args = ["run", "--repo", str(repo.path), "--base", ev.commit, "--head", head]
    args += ["--discover", "pytest", "--command", f"{sys.executable} -m pytest"]
    assert main([*args, "--evidence", "auto", "--no-cache", "-o", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["analysis"]["evidence"]["environment_checked"] is True


# --------------------------------------------------------------------------- order checks

CACHED_LIB = """\
_STATE = {}


def compute():
    return 42


def get():
    if "v" not in _STATE:
        _STATE["v"] = compute()
    return _STATE["v"]
"""

PASSING = "def test_a():\n    pass\n"

USES_GET = """\
from pkg.lib import get


def test_m1():
    assert get() == 42


def test_m2():
    assert get() == 42
"""


@needs_monitoring
def test_evp4_run_collect_order_checks_fresh_records(repo, capfd):
    """A reverse-checked recording advanced by ``run --collect``: the fresh
    records are checked in reverse order too. ``test_m2`` reads the value
    ``test_m1`` cached, so its forward record lacks ``compute``; in reverse
    it is the one that computes, so both are unstable."""
    c0 = repo.commit({**BASE, "pkg/lib.py": CACHED_LIB, "tests/test_a.py": PASSING})
    ev0 = repo.collect(reverse_check=True)
    assert ev0.reverse_checked
    c1 = repo.commit({"tests/test_m.py": USES_GET})
    assert _run_collect(repo, c0, c1) == 0
    assert "running the selected tests again in reverse order" in capfd.readouterr().err
    head1 = repo.git("rev-parse", c1).strip()
    (info,) = [s for s in list_stores(repo.path) if s.commit == head1]
    ev1 = load_store(info.path)
    assert ev1.reverse_checked
    unstable = {t for t, r in ev1.tests.items() if r.flags & FLAG_UNSTABLE}
    assert unstable == {"tests/test_m.py::test_m1", "tests/test_m.py::test_m2"}
    c2 = repo.commit({"pkg/lib.py": CACHED_LIB.replace("return 42", "return 41")})
    bench = asv_target("benchmarks.TimeGet.time_get", "pkg.lib.get")
    plan = _plan(repo, c1, c2, ev1, [bench])
    assert selected(plan) == {
        "tests/test_m.py::test_m1",
        "tests/test_m.py::test_m2",
        "benchmarks.TimeGet.time_get",
    }


@needs_monitoring
def test_evp4_advance_marks_unchecked_fresh_records_unstable(repo):
    """``advance`` itself: fresh records from a run in one order, onto a
    reverse-checked recording, are unstable, so the store's claim holds."""
    a = "tests/test_a.py::test_a"
    repo.commit({**BASE, "pkg/lib.py": CACHED_LIB, "tests/test_a.py": PASSING})
    checked = repo.collect(reverse_check=True)
    assert not checked.tests[a].flags & FLAG_UNSTABLE
    fresh = repo.collect()
    assert not fresh.reverse_checked
    advanced = advance(checked, fresh, {a}, checked.commit)
    assert advanced.reverse_checked
    assert advanced.tests[a].flags & FLAG_UNSTABLE
    # Onto a recording that was never order-checked, nothing is claimed.
    plain = advance(fresh, fresh, {a}, fresh.commit)
    assert not plain.reverse_checked
    assert not plain.tests[a].flags & FLAG_UNSTABLE


# --------------------------------------------------------------------------- stores


@pytest.mark.parametrize(
    "name",
    [
        "we#ird",
        pytest.param(
            "what?now",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="? is not allowed in a path"),
        ),
        "pct%41",
        "a b",
    ],
)
def test_q16_a_store_path_with_uri_characters_opens(tmp_path, name):
    """The store is opened read-only through a URI: ``#``, ``?`` and ``%`` in
    the path are percent-encoded, not read as a fragment, query or escape."""
    evidence = Evidence(
        commit="c" * 40,
        source_roots=["."],
        environment={"python": "3.13.1 (main)"},
        environment_hash="e",
        command="pytest",
        created=time.time(),
        symbols=["pkg.lib.f"],
        paths=[],
        tests={"tests/test_a.py::test_a": TestRecord(frozenset({0}), frozenset())},
    )
    repo = tmp_path / name
    store = write_store(evidence, repo / ".diffcone" / "evidence")
    loaded = load_store(store)
    assert loaded.tests == evidence.tests
    assert loaded.symbols == ["pkg.lib.f"]
    (info,) = list_stores(repo)
    assert (info.commit, info.tests, info.python) == ("c" * 40, 1, "3.13.1")


def test_q16_a_store_without_a_python_version_lists(tmp_path):
    """A recorded environment with an empty ``python`` field is listed, not
    an IndexError (quality item 19)."""
    evidence = Evidence(
        commit="d" * 40,
        source_roots=["."],
        environment={"python": ""},
        environment_hash="e",
        command="pytest",
        created=time.time(),
        symbols=[],
        paths=[],
        tests={},
    )
    write_store(evidence, tmp_path / ".diffcone" / "evidence")
    (info,) = list_stores(tmp_path)
    assert info.python == ""


# What ran only through annotations evaluated outside every test: kept by
# the store, and merged when a store advances.

ADD_OPS = "def add(a, b):\n    return a + b\n"

TYPED = """\
from pathlib import Path

from pkg.ops import add


def test_typed(tmp_path: Path) -> None:
    assert add(1, 2) == 3


def test_plain():
    assert add(2, 2) == 4
"""

TYPED_OTHER = "from pkg.ops import add\n\n\ndef test_other():\n    assert add(0, 0) == 0\n"
TYPED_FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": ADD_OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TYPED,
    "tests/test_other.py": TYPED_OTHER,
}


@needs_monitoring
def test_e7_the_store_keeps_what_ran_only_through_annotations(repo, tmp_path):
    repo.commit(TYPED_FILES)
    ev = repo.collect()
    again = load_store(write_store(ev, tmp_path / "stores"))
    assert again.annotations_only == ev.annotations_only
    if sys.version_info >= (3, 14):
        assert "tests.test_ops.test_typed" in ev.annotations_only


def test_e7_a_symbol_is_annotations_only_only_if_every_source_says_so():
    """Advancing a store merges recordings: a symbol whose own code ran
    outside the tests in any of them is not annotations-only."""

    def source(only, hook):
        return SimpleNamespace(
            annotations_only=set(only),
            import_phase=set(hook),
            hook_phase=set(hook),
            import_by={},
            collect_by={},
        )

    merged = evidence_module._annotations_only(
        [source({"a.f", "a.g"}, {"a.f", "a.g"}), source({"a.h"}, {"a.g", "a.h"})]
    )
    assert merged == {"a.f", "a.h"}

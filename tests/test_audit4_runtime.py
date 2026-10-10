"""Regression tests for the fourth audit round, batch E (runtime: execution,
CLI, recorder, CI actions). Each failed before its fix."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from diffcone.cli import main

ACTIONS = Path(__file__).resolve().parent.parent / "actions"
PYTEST = f"{sys.executable} -m pytest"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")

OPS = "def add(a, b):\n    return a + b\n"
FILES = {
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
    ),
    "benchmarks/asv.conf.json": '{"benchmark_dir": "benchmarks"}\n',
    "benchmarks/benchmarks/__init__.py": "",
    "benchmarks/benchmarks/bench_ops.py": (
        "from pkg.ops import add\n\n\n"
        "class Ops:\n    def time_add(self):\n        return add(1, 1)\n"
    ),
}


# Q18: the working tree's status, read with ``-z``.


def test_q18_a_non_ascii_name_is_filtered_and_a_rename_is_two_changes(repo):
    from diffcone.execution import _dirty

    config = '{"benchmark_dir": "benchmarks", "results_dir": "résultats"}\n'
    files = {**FILES, "benchmarks/asv.conf.json": config}
    repo.commit({**files, "pkg/é.py": "X = 1\n", "pkg/old.py": "Y = 1\n"})
    # Untracked bytecode and ASV output under a non-ASCII name: nothing that
    # runs, so the tree is clean (git quoted the name, and the quote defeated
    # both filters).
    (repo.path / "pkg" / "é.pyc").write_bytes(b"")
    results = repo.path / "benchmarks" / "résultats" / "machine.json"
    results.parent.mkdir()
    results.write_text("{}")
    assert _dirty(repo.path) == []
    # A tracked non-ASCII file edited is named as git stores it.
    (repo.path / "pkg" / "é.py").write_text("X = 2\n")
    assert _dirty(repo.path) == ["pkg/é.py"]
    repo.git("checkout", "--", "pkg/é.py")
    # A rename: the new name and the original, not "old -> new".
    repo.git("mv", "pkg/old.py", "pkg/new.py")
    assert sorted(_dirty(repo.path)) == ["pkg/new.py", "pkg/old.py"]


# Quality: validate's command, run's dry run, discover's caches, Windows paths.


def test_validate_reports_the_command_that_ran_without_the_obsolete_v(repo):
    from diffcone.execution import validate_pytest

    base = repo.commit(FILES)
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    v = validate_pytest(plan, repo=repo.path, command=PYTEST)
    assert v.ok
    assert v.command[: len(PYTEST.split())] == PYTEST.split()
    assert "-v" not in v.command
    assert v.command[v.command.index("diffcone_select") - 1] == "-p"


def test_an_evidence_dry_run_prints_a_command_without_the_recorder(repo):
    from diffcone.execution import PLUGIN, run_with_evidence

    base = repo.commit(FILES)
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    plan.evidence = {"environment_hash": "0" * 16}

    def static_plan():
        raise AssertionError("a dry run checks nothing, so it never falls back")

    ran = run_with_evidence(
        plan, static_plan, cwd=repo.path, command=PYTEST, extra=["-x"], dry_run=True
    )
    # The recorder is importable only in diffcone's own run: the printed
    # command is the one a user can run.
    assert PLUGIN not in ran.result.command
    assert ran.result.command == [*PYTEST.split(), "-x", "tests/test_ops.py::test_add"]
    assert ran.result.returncode is None and not ran.environment_checked


def test_discover_uses_the_caches_and_they_change_nothing(repo, tmp_path, capsys):
    repo.commit(FILES)
    cache = tmp_path / "cache"
    common = ["discover", "--repo", str(repo.path), "--discover", "pytest", "--discover", "asv"]

    def run(*extra: str) -> dict:
        assert main([*common, *extra]) == 0
        return json.loads(capsys.readouterr().out)

    uncached = run("--no-cache")
    assert not cache.exists()
    cold = run("--cache-dir", str(cache))
    # The index, the module records and the discovery results were stored.
    assert any((cache / "index").iterdir()) and any((cache / "discovery").iterdir())
    assert (cache / "modules.sqlite").exists()
    warm = run("--cache-dir", str(cache))
    assert uncached == cold == warm
    assert {t["runner"] for t in warm["targets"]} == {"pytest", "asv"}


def test_resolve_command_reads_a_forward_slash_path_on_windows(tmp_path, monkeypatch):
    from diffcone import execution

    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution.os, "sep", "\\")
    resolved = execution.resolve_command(".venv/bin/python -m pytest", tmp_path / "elsewhere")
    assert resolved is not None
    assert execution.split_command(resolved) == [str(python), "-m", "pytest"]
    # A bare program name is looked up on PATH, as before.
    assert execution.resolve_command("python -m pytest", tmp_path) == "python -m pytest"


# Quality: the copies that must agree.


def test_the_three_fold_nodeid_copies_agree():
    from diffcone import collect, execution, selection

    ids = [
        "tests/test_a.py::test_x",
        "tests/test_a.py::test_x[1]",
        "tests/test_a.py::TestK::test_m[a-b]",
        "tests/test_o.py::test_choice[choices4-[TEXT: a|b]]",
        "tests/test_o.py::test_[weird]name[1]",
        "tests/test_o.py::test_x[1]extra",
        "tests/test_o.py",
    ]
    for nodeid in ids:
        folded = execution.fold_nodeid(nodeid)
        assert collect.fold_nodeid(nodeid) == folded, nodeid
        assert selection.fold_nodeid(nodeid) == folded, nodeid


def test_a_recorder_window_is_merged_and_written_whole():
    from diffcone import collect

    window = collect._window()
    window.update(
        codes={1},
        paths={"a.txt"},
        dirs={"data"},
        fixtures={"k"},
        spawns={(1, 2, 3, ("python", "-c", "pass"), False)},
        flags=collect.FLAG_TEXT,
    )
    merged = collect._window()
    collect._merge(merged, window)
    assert merged == window
    data = collect._serialised(window)
    assert set(data) == set(window)
    assert data["spawns"] == [[1, 2, 3, ["python", "-c", "pass"], False]]


def test_the_recorders_share_their_constants():
    from diffcone import child, collect

    for name in (
        "TOOL",
        "SUBPROCESS_EVENTS",
        "C_OPEN_EVENTS",
        "LISTING_EVENTS",
        "IGNORED_DIRS",
        "INSTALLED",
        "CYTHON_SUFFIXES",
        "DATA_READERS",
        "METADATA_MODULES",
    ):
        assert getattr(collect, name) is getattr(child, name), name
    assert collect._INTERPRETER is child.INTERPRETER


def test_outcomes_are_written_as_reports_come_in_through_one_handle(tmp_path, monkeypatch):
    from diffcone import selection

    monkeypatch.setattr(selection, "OUTCOMES", str(tmp_path))
    monkeypatch.setattr(selection, "_outcomes", None)
    opened = []
    real_open = open

    def counting_open(*args, **kwargs):
        opened.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    report = SimpleNamespace(when="call", outcome="passed", passed=True, failed=False)
    for name in ("t.py::a", "t.py::b"):
        selection.pytest_runtest_logreport(SimpleNamespace(**vars(report), nodeid=name))
        # Written through at once: a process that crashes later keeps it.
        written = (tmp_path / f"outcomes-{os.getpid()}").read_text("utf-8")
        assert written.endswith(f"{name}\tPASSED\n")
    assert len([p for p in opened if "outcomes-" in str(p)]) == 1
    handle = selection._outcomes
    selection.pytest_sessionfinish(None)
    assert handle is not None and handle.closed and selection._outcomes is None
    assert (tmp_path / f"outcomes-{os.getpid()}").read_text("utf-8").splitlines() == [
        "t.py::a\tPASSED",
        "t.py::b\tPASSED",
    ]


# Quality: the report and the actions.


def test_a_not_checked_reason_cannot_break_the_report_table(tmp_path):
    from diffcone import ci_report

    run = tmp_path / "1"
    (run / "diffcone-unit").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"id": 1, "event": "push", "url": "https://ci/1"}))
    context = {"action": "record", "event": "push", "commit": "c" * 40, "recorded": True,
               "checked": False, "check_note": "a | b", "misses": 0, "flaky": 0,
               "check_error": False, "sticky": False}  # fmt: skip
    (run / "diffcone-unit" / "context.json").write_text(json.dumps(context))
    markdown = ci_report.to_markdown(ci_report.build(ci_report.load(tmp_path)))
    row = next(line for line in markdown.splitlines() if line.startswith("| diffcone-unit"))
    assert "1 (a \\| b)" in row
    assert row.count(" | ") == 7  # eight cells


def test_the_actions_share_one_install_and_the_report_quotes_as_diffcone_does():
    installs = []
    for action in ("check", "record", "report", "run"):
        text = (ACTIONS / action / "action.yml").read_text()
        step = text[text.index("- name: Install diffcone") :].split("- name:", 2)[1]
        installs.append(step.split("run:", 1)[1].strip())
        assert "pip install" not in step
    assert set(installs) == {'bash "$GITHUB_ACTION_PATH/../install.sh"'}
    install = (ACTIONS / "install.sh").read_text()
    assert "pip install" in install and "DIFFCONE_PYTHON" in install
    report = (ACTIONS / "report" / "action.yml").read_text()
    body = next(b for b in re.findall(r"<<'PY'\n(.*?)\n *PY\n", report, re.S) if "UPSTREAM" in b)
    assert "def code(" not in body
    assert "from diffcone.ci_report import code_span as code" in body
    assert "\"$DIFFCONE_PYTHON\" - <<'PY'" in report


def _bash(script: str, cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        [shutil.which("bash") or "bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )


@posix_only
def test_run_and_record_find_the_recording_the_same_way(repo):
    sha = repo.commit(FILES)
    # Both actions source the one function.
    for action in ("run", "record"):
        text = (ACTIONS / action / "action.yml").read_text()
        assert 'source "$GITHUB_ACTION_PATH/../recording.sh"' in text
        assert "ls .diffcone/evidence" not in text
    script = (
        f'source "{ACTIONS / "recording.sh"}"\n'
        'found=0; find_recording || found=$?\necho "$found|$store|$commit"\n'
    )
    assert _bash(script, repo.path).stdout.strip() == "1||"
    evidence = repo.path / ".diffcone" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / f"{sha}-abcd.sqlite").write_bytes(b"")
    found = _bash(script, repo.path).stdout.strip()
    assert found == f"0|.diffcone/evidence/{sha}-abcd.sqlite|{sha}"
    (evidence / f"{sha}-abcd.sqlite").unlink()
    gone = "f" * 40
    (evidence / f"{gone}-abcd.sqlite").write_bytes(b"")
    found = _bash(script, repo.path).stdout.strip()
    assert found == f"2|.diffcone/evidence/{gone}-abcd.sqlite|{gone}"


def _step(action: str, name: str) -> str:
    """The ``run:`` script of a named step of an action."""
    text = (ACTIONS / action / "action.yml").read_text()
    lines = text[text.index(f"- name: {name}") :].split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == "run: |") + 1
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


@posix_only
def test_the_record_check_reruns_a_quoted_command_as_written(tmp_path):
    """The re-run of a miss split ``command`` on words, so a program whose
    path holds a space (or a quoted argument) did not run, and the miss
    stood unconfirmed."""
    work, results, bin_dir = tmp_path / "repo", tmp_path / "results", tmp_path / "my bin"
    for d in (work / ".diffcone", work / "tests", results, bin_dir):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty",
         "-m", "x"],
        cwd=work, check=True,
    )  # fmt: skip
    (work / "tests" / "test_a.py").write_text("")
    target = {"runner": "pytest", "runner_id": "tests/test_a.py::test_x"}
    plan = {"status": "complete", "selected_targets": [], "unselected_targets": [target]}
    (results / "plan.json").write_text(json.dumps(plan))
    case = '<testcase classname="tests.test_a" name="test_x"'
    (results / "previous.xml").write_text(
        f"<testsuites><testsuite>{case}/></testsuite></testsuites>"
    )
    (work / "full.xml").write_text(
        f"<testsuites><testsuite>{case}><failure/></testcase></testsuite></testsuites>"
    )
    fake = bin_dir / "fake pytest"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{results}/rerun-args"\nexit 1\n')
    fake.chmod(0o755)
    real = Path(sys.executable).parent / "diffcone"
    plain = tmp_path / "bin"
    plain.mkdir()
    (plain / "diffcone").symlink_to(real)
    outputs = tmp_path / "outputs"
    done = _bash(
        _step("record", "Check the plan against this full run"),
        work,
        {
            "PATH": f"{plain}{os.pathsep}{os.environ['PATH']}",
            "RESULTS": str(results),
            "JUNIT": "full.xml",
            "COMMAND": f'"{fake}" -X "a b"',
            "PYTEST_ARGS": "",
            "GITHUB_OUTPUT": str(outputs),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    args = (results / "rerun-args").read_text().splitlines()
    assert args[:2] == ["-X", "a b"]
    assert "tests/test_a.py::test_x" in args

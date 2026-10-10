"""Regression scenarios for the third audit round (internal/audit.md, round 3).

Each test names the finding it guards and failed before its fix.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ACTIONS = Path(__file__).resolve().parent.parent / "actions"

# Shell scripts as commands: POSIX only.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


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


# CI-3, CI-4, CI-7: the record action's check step.

PLAN = {
    "status": "complete",
    "selected_targets": [],
    "unselected_targets": [
        {"runner": "pytest", "runner_id": "tests/test_a.py::test_x"},
        {"runner": "pytest", "runner_id": "tests/test_a.py::test_other"},
    ],
}


def _junit(failing: list[str], passing: tuple[str, ...] | list[str] = ()) -> str:
    case = '<testcase classname="tests.test_a" name="{}"'
    cases = [case.format(n) + "><failure/></testcase>" for n in failing]
    cases += [case.format(n) + "/>" for n in passing]
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


def _check_step(tmp_path, rerun_junit: str | None, *, pytest_args="", diffcone=None):
    """Run the check step with ``test_x`` a new failure the plan did not
    select; the re-run (a fake pytest) writes ``rerun_junit``. Returns the
    step's outputs and the arguments the re-run got."""
    work, results, bin_dir = tmp_path / "repo", tmp_path / "results", tmp_path / "bin"
    for d in (work / ".diffcone", work / "tests", results, bin_dir):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty",
         "-m", "x"],
        cwd=work, check=True,
    )  # fmt: skip
    (work / "tests" / "test_a.py").write_text("")
    (results / "plan.json").write_text(json.dumps(PLAN))
    (results / "previous.xml").write_text(_junit([], ["test_x", "test_other"]))
    (work / "full.xml").write_text(_junit(["test_x"], ["test_other"]))
    (results / "rerun.junit").write_text(rerun_junit or "")
    fake = bin_dir / "fakepytest"
    fake.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > {results}/rerun-args\n'
        + (
            ""
            if rerun_junit is None
            else "for a; do case $a in --junitxml=*) "
            f'cp {results}/rerun.junit "${{a#--junitxml=}}";; esac; done\n'
        )
        + "exit 1\n"
    )
    fake.chmod(0o755)
    real = Path(sys.executable).parent / "diffcone"
    if diffcone is None:
        (bin_dir / "diffcone").symlink_to(real)
    else:
        (bin_dir / "diffcone").write_text(diffcone.replace("REAL", str(real)))
        (bin_dir / "diffcone").chmod(0o755)
    outputs = tmp_path / "outputs"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RESULTS": str(results),
        "JUNIT": "full.xml",
        "COMMAND": "fakepytest",
        "PYTEST_ARGS": pytest_args,
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    done = subprocess.run(
        [shutil.which("bash") or "bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         _step("record", "Check the plan against this full run")],
        cwd=work, env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    got = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    args = (results / "rerun-args").read_text().split() if (results / "rerun-args").exists() else []
    return got, json.loads((work / ".diffcone" / "check.json").read_text()), args


@posix_only
def test_ci3_a_rerun_whose_junit_cannot_be_read_leaves_the_miss_standing(tmp_path):
    truncated = '<testsuites><testsuite><testcase classname="tests.te'
    got, kept, _ = _check_step(tmp_path, truncated)
    assert got["misses"] == "1" and got["flaky"] == "0"
    assert kept["misses"] == 1 and kept["checked"]


@posix_only
def test_ci3_a_rerun_that_fails_again_is_a_miss(tmp_path):
    got, _, _ = _check_step(tmp_path, _junit(["test_x"]))
    assert (got["misses"], got["flaky"], got["check-error"]) == ("1", "0", "false")


@posix_only
def test_ci4_a_miss_exit_without_a_verdict_is_a_check_error(tmp_path):
    # check exits 1 (a miss) but writes no verdict: before, the misses were
    # read from a process substitution that failed silently, and the push
    # was recorded as checked with no miss.
    silent = '#!/bin/bash\ncase "$*" in *json*) exit 1;; esac\nexec REAL "$@"\n'
    got, kept, _ = _check_step(tmp_path, None, diffcone=silent)
    assert got["check-error"] == "true" and kept["check_error"]


@posix_only
def test_ci7_only_the_rerun_tests_count_and_a_path_after_a_flag_is_dropped(tmp_path):
    # ``-ra tests``: tests is a path, not -r's value, so the re-run names only
    # the missed test. And a failure of anything else the re-run ran is not
    # this check's miss (it made the flaky count negative).
    got, _, args = _check_step(
        tmp_path, _junit(["test_other"], ["test_x"]), pytest_args="-ra tests"
    )
    assert "tests" not in args and "tests/test_a.py::test_x" in args
    assert (got["misses"], got["flaky"]) == ("0", "1")


def test_ci5_a_rerun_restores_its_own_commits_recording_first():
    record = (ACTIONS / "record" / "action.yml").read_text()
    restore = record[record.index("- name: Restore the previous recording") :]
    restore = restore[: restore.index("- name:", 10)]
    keys = [line.strip() for line in restore.split("restore-keys: |\n", 1)[1].splitlines()]
    keys = [key for key in keys if key]
    assert keys == [
        "${{ inputs.key-prefix }}--${{ github.sha }}-",
        "${{ inputs.key-prefix }}--",
    ]
    # And a recording not from before this commit is not checked against.
    plan = _step("record", "Plan the commit's change")
    assert 'merge-base --is-ancestor "$commit" HEAD^1' in plan


# CI-2, CI-9: diffcone report.


def _artifact(root: Path, run_id: int, event: str, files: dict[str, dict]) -> None:
    run = root / str(run_id)
    (run / "diffcone-unit").mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps({"id": run_id, "event": event, "url": f"https://ci/{run_id}"})
    )
    for name, data in files.items():
        (run / "diffcone-unit" / name).write_text(json.dumps(data))


def _record(misses) -> dict:
    return {"action": "record", "event": "push", "commit": "c" * 40, "recorded": True,
            "checked": True, "misses": misses, "flaky": 0, "check_error": False,
            "sticky": False}  # fmt: skip


FORGED_VERDICT = {
    "failures": [{"test": "t.py::x` @someone", "kind": "test", "already_failing": False}],
    "misses": ["t.py::x` @someone"],
}


def test_ci2_a_recording_artifact_from_a_pull_request_run_is_not_trusted(tmp_path):
    from diffcone import ci_report

    forged = {"context.json": _record(1), "verdict.json": FORGED_VERDICT}
    _artifact(tmp_path, 1, "pull_request", forged)
    report = ci_report.build(ci_report.load(tmp_path))
    assert report.ok and not report.misses
    assert report.cells[0].untrusted == 1


def test_ci2_a_malformed_artifact_is_counted_not_a_crash(tmp_path, capsys):
    from diffcone import ci_report
    from diffcone.cli import main

    _artifact(tmp_path, 1, "push", {"context.json": _record("lots")})
    _artifact(tmp_path, 2, "push", {"context.json": _record(0)})
    report = ci_report.build(ci_report.load(tmp_path))
    assert report.malformed == 1 and not report.ok
    assert report.cells[0].pushes == 1  # the readable one, and nothing of the other
    assert main(["report", "--dir", str(tmp_path)]) == 1
    assert "could not be read" in capsys.readouterr().out


def test_ci2_a_test_id_with_backticks_stays_inside_its_code_span(tmp_path):
    from diffcone import ci_report

    _artifact(tmp_path, 1, "push", {"context.json": _record(1), "verdict.json": FORGED_VERDICT})
    text = ci_report.to_markdown(ci_report.build(ci_report.load(tmp_path)))
    assert "- `` t.py::x` @someone `` (test)" in text


def test_ci9_selected_tests_pytest_did_not_collect_are_a_problem(tmp_path):
    from diffcone import ci_report

    context = {"action": "run", "event": "pull_request", "plan_exit": 0, "run_exit": 3}
    _artifact(tmp_path, 1, "pull_request", {"context.json": context})
    report = ci_report.build(ci_report.load(tmp_path))
    assert report.not_collected == 1 and not report.ok
    assert "did not run selected tests" in ci_report.to_markdown(report)


# CI-6: the report's window.

FAKE_GH = """#!/bin/bash
case "$*" in
  *"runs/11/artifacts"*) ;;
  *"runs/10/artifacts"*) echo posted-diffcone-report ;;
  *"runs/7/artifacts"*) echo 5 diffcone-ubuntu ;;
  *"--jq .workflow_id"*) echo 42 ;;
  *"per_page=30"*) printf '11 2026-10-09T05:00:00Z\\n10 2026-10-09T02:00:00Z\\n' ;;
  *"status=success&per_page=1 "*) echo 2026-10-09T05:00:00Z ;;
  "run list"*) echo '[{"databaseId":7,"url":"u","event":"push",'\
'"createdAt":"2026-10-09T03:30:00Z","updatedAt":"2026-10-09T04:00:00Z","headSha":"c",'\
'"headBranch":"main","workflowName":"ci","conclusion":"failure"}]' ;;
  *"actions/artifacts/5/zip"*) "$FAKE_PY" -c 'import sys, zipfile
with zipfile.ZipFile(sys.stdout.buffer, "w") as z: z.writestr("context.json", "{}")' ;;
esac
"""


@posix_only
def test_ci6_a_report_that_posted_nothing_does_not_move_the_window(tmp_path):
    # Run 11 (05:00) wrote only its job summary; run 10 (02:00) posted. The
    # CI run that completed at 04:00 must be in this report.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    runs = tmp_path / "runs"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "x", "HOURS": "24", "WORKFLOW": "ci.yml", "PREFIX": "diffcone-",
        "RUNS": str(runs), "RUNS_REPO": "o/r", "GITHUB_REPOSITORY": "o/diffcone",
        "GITHUB_RUN_ID": "1", "MARKER": "posted-diffcone-report",
        "FAKE_PY": sys.executable,
    }  # fmt: skip
    done = subprocess.run(
        [shutil.which("bash") or "bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         _step("report", "Download the runs' results")],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(p.name for p in (runs / "runs").iterdir()) == ["7"]
    assert (runs / "runs" / "7" / "diffcone-ubuntu" / "context.json").exists()


# CI-8: a resolved command is quoted for the platform that splits it.


def test_ci8_a_resolved_command_is_joined_as_windows_reads_it(monkeypatch):
    from diffcone import execution

    monkeypatch.setattr(execution.os, "name", "nt")
    joined = execution.join_command([r"C:\Program Files\venv\python.exe", "-m", "pytest"])
    assert joined == r'"C:\Program Files\venv\python.exe" -m pytest'
    assert "'" not in execution.join_command([r"C:\a b\python.exe"])


# W8: every selection is explained by its path, not a placeholder.

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'

DOCS = {
    "asv.conf.json": ASV_CONF,
    "pkg/__init__.py": "",
    "pkg/deco.py": (
        "def doc(**kw):\n    def wrap(f):\n        f.__doc__ = f.__doc__.format(**kw)\n"
        "        return f\n    return wrap\n"
    ),
    "pkg/a.py": (
        "from pkg.deco import doc\n\n\n@doc(klass='Frame')\ndef reduce():\n"
        '    """Reduce a {klass}."""\n    return 1\n\n\n'
        'def greet():\n    """Hello."""\n    return 2\n\n\n'
        'def plain():\n    """Plain."""\n    return 3\n'
    ),
    "tests/__init__.py": "",
    "tests/test_reduce.py": "from pkg.a import reduce\n\n\ndef test_reduce():\n    reduce()\n",
    "tests/test_doc.py": (
        "from pkg.a import greet\n\n\ndef test_doc():\n    assert greet.__doc__ == 'Hello.'\n"
    ),
    "tests/test_plain.py": "from pkg.a import plain\n\n\ndef test_plain():\n    plain()\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}


@pytest.mark.parametrize(
    "old, new",
    [
        ("Reduce a {klass}.", "Reduce a {klass} along {axis}."),  # a decorator reads it
        ('"""Hello."""', '"""Hi."""'),  # a test reads ``greet.__doc__``
    ],
)
def test_w8_a_docstring_seed_is_explained_by_its_steps(repo, old, new):
    base = repo.commit(DOCS)
    head = repo.commit({"pkg/a.py": DOCS["pkg/a.py"].replace(old, new)})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    reasons = [r for d in plan.decisions if d.selected for r in d.reasons]
    assert reasons
    assert not any("dynamic import/attribute access ()" in r.detail for r in reasons)
    assert all(r.rule == "dependency" and r.changed_symbol for r in reasons)


# W10: a namespace read off any object is a getattr.


@pytest.mark.parametrize(
    "read",
    [
        "obj.__dict__[k]",
        "obj.__dict__.get(k)",
        "obj.__class__.__dict__[k]",
    ],
)
def test_w10_a_namespace_read_off_any_object_is_a_getattr(repo, read):
    """``x.__dict__[k]`` on an object of any type reads what ``getattr(x,
    k)`` reads; it recorded nothing unless ``x`` was a known module or
    class."""
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    def facts(body):
        repo.commit({"pkg/__init__.py": "", "pkg/reader.py": f"def read(obj, k):\n    {body}\n"})
        index = build_index(read_snapshot(repo.path, "HEAD", ["."]))
        return {
            (u.kind, u.name)
            for u in index.unresolved
            if u.symbol == "pkg.reader.read" and u.kind == "dynamic"
        }

    via_getattr = facts("return getattr(obj, k)")
    assert via_getattr
    assert facts(f"return {read}") == via_getattr


@pytest.mark.parametrize(
    "use",
    [
        "return k in obj.__dict__",
        "return [n for n in obj.__dict__]",
        "return len(obj.__dict__)",
        "return list(obj.__dict__.keys())",
    ],
)
def test_w10_a_use_of_the_names_only_reads_no_attribute(repo, use):
    """``m in cls.__dict__`` (flask's ``MethodView``) sees names, no value:
    no dynamic read."""
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    repo.commit({"pkg/__init__.py": "", "pkg/reader.py": f"def read(obj, k):\n    {use}\n"})
    index = build_index(read_snapshot(repo.path, "HEAD", ["."]))
    assert not [
        u for u in index.unresolved if u.symbol == "pkg.reader.read" and u.kind == "dynamic"
    ]

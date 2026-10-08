"""``diffcone report``: a report over the results the CI actions upload."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from diffcone import ci_report
from diffcone.cli import main

ACTIONS = Path(__file__).resolve().parent.parent / "actions"


def _plan(selected: int, targets: int, evidence: bool = True, unselected=()) -> dict:
    return {
        "analysis": {
            "counts": {"targets": targets, "selected": selected},
            "evidence": {"commit": "e" * 40} if evidence else None,
        },
        "selected_targets": [],
        "unselected_targets": [{"runner_id": t, "reason": why} for t, why in unselected],
    }


def _verdict(failures: list[tuple[str, str, bool]], misses: list[str]) -> dict:
    return {
        "ok": not misses,
        "failures": [
            {
                "test": t,
                "outcome": "failed",
                "kind": kind,
                "selected": False,
                "already_failing": old,
            }
            for t, kind, old in failures
        ],
        "misses": misses,
    }


def _write(directory: Path, name: str, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(data))


def _run(root: Path, run_id: int, event: str, sha: str, artifacts: dict[str, dict]) -> None:
    run = root / str(run_id)
    _write(
        run,
        "run.json",
        {
            "id": run_id,
            "url": f"https://ci.example/runs/{run_id}",
            "event": event,
            "created_at": f"2026-10-0{run_id}T00:00:00Z",
            "head_sha": sha,
        },
    )
    for name, files in artifacts.items():
        for filename, data in files.items():
            _write(run / name, filename, data)


def _pr(plan_exit=0, run_exit=0, recording="r" * 40, note=None, allow=False) -> dict:
    return {
        "action": "run",
        "event": "pull_request",
        "commit": "c" * 40,
        "base": "b" * 40,
        "recording": recording,
        "recording_note": note,
        "allow_incomplete_discovery": allow,
        "plan_exit": plan_exit,
        "run_exit": run_exit,
    }


def _push(sha: str, misses=0, flaky=0, checked=True, note=None, error=False) -> dict:
    return {
        "action": "record",
        "event": "push",
        "commit": sha,
        "recorded": True,
        "checked": checked,
        "check_note": note,
        "recording": "p" * 40,
        "misses": misses,
        "flaky": flaky,
        "check_error": error,
        "sticky": False,
    }


def test_pull_requests_and_clean_pushes(tmp_path):
    _run(
        tmp_path,
        1,
        "pull_request",
        "1" * 40,
        {
            "diffcone-unit-linux": {"context.json": _pr(), "ran.json": _plan(10, 100)},
            "diffcone-e2e": {"context.json": _pr(), "ran.json": _plan(5, 10)},
        },
    )
    _run(
        tmp_path,
        2,
        "pull_request",
        "2" * 40,
        {
            "diffcone-unit-linux": {
                "context.json": _pr(recording=None, note="no recording restored"),
                "plan.json": _plan(100, 100, evidence=False),
            },
            # The environment differed: ran.json is the static plan that ran.
            "diffcone-e2e": {"context.json": _pr(), "ran.json": _plan(10, 10, evidence=False)},
        },
    )
    _run(
        tmp_path,
        3,
        "push",
        "3" * 40,
        {
            "diffcone-unit-linux": {
                "context.json": _push("3" * 40),
                "verdict.json": _verdict([("tests/t.py::old", "test", True)], []),
            },
            "diffcone-e2e": {
                "context.json": _push("3" * 40, checked=False, note="the commit has no parent")
            },
        },
    )
    report = ci_report.build(ci_report.load(tmp_path))
    assert report.ok
    assert (report.runs, report.pull_request_runs, report.push_runs) == (3, 2, 1)
    assert (report.first, report.last) == ("2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
    cells = {c.name: c for c in report.cells}
    unit, e2e = cells["diffcone-unit-linux"], cells["diffcone-e2e"]
    assert unit.selected_shares == [0.1, 1.0]
    assert dict(unit.from_code) == {"no recording restored": 1}
    assert dict(e2e.from_code) == {"the environment differed from the recording's": 1}
    assert (unit.pushes, unit.checked, unit.new_failures, unit.misses) == (1, 1, 0, 0)
    assert dict(e2e.not_checked) == {"the commit has no parent": 1}
    text = ci_report.to_markdown(report)
    assert "no miss" in text and "| diffcone-unit-linux | 2 | 55.0 % | 100.0 % |" in text
    assert "1 (no recording restored)" in text and "1 (the commit has no parent)" in text


def test_a_confirmed_miss_and_a_flaky_one(tmp_path):
    sha = "4" * 40
    verdict = _verdict(
        [
            ("tests/t.py::test_flaky", "test", False),
            ("tests/t.py::test_missed", "test", False),
            ("tests/broken.py", "collection", False),
        ],
        ["tests/t.py::test_flaky", "tests/t.py::test_missed", "tests/broken.py"],
    )
    plan = _plan(
        1, 10, unselected=[("tests/t.py::test_missed", "no dependency on a changed symbol")]
    )
    rerun = _verdict([("tests/t.py::test_missed", "test", False)], ["tests/t.py::test_missed"])
    _run(
        tmp_path,
        4,
        "push",
        sha,
        {
            "diffcone-unit-linux": {
                "context.json": _push(sha, misses=2, flaky=1),
                "plan.json": plan,
                "verdict.json": verdict,
                "rerun-verdict.json": rerun,
            }
        },
    )
    report = ci_report.build(ci_report.load(tmp_path))
    assert not report.ok
    assert [(m.test, m.kind, m.reason) for m in report.misses] == [
        ("tests/broken.py", "collection", None),
        ("tests/t.py::test_missed", "test", "no dependency on a changed symbol"),
    ]
    assert report.misses[1].commit == sha and report.misses[1].run_url.endswith("/4")
    cell = report.cells[0]
    assert (cell.new_failures, cell.flaky, cell.misses) == (3, 1, 2)
    assert report.earlier_misses == 0
    text = ci_report.to_markdown(report)
    assert "MISSES" in text and "`tests/t.py::test_missed` (test)" in text
    assert "the plan: no dependency on a changed symbol" in text
    data = ci_report.to_dict(report)
    assert data["ok"] is False and len(data["misses"]) == 2


def test_misses_stand_when_the_rerun_did_not_run(tmp_path):
    """No re-run verdict: the re-run ran nothing, and every miss stands."""
    sha = "5" * 40
    _run(
        tmp_path,
        5,
        "push",
        sha,
        {
            "diffcone-r": {
                "context.json": _push(sha, misses=1),
                "verdict.json": _verdict([("tests/t.py::a", "test", False)], ["tests/t.py::a"]),
            }
        },
    )
    report = ci_report.build(ci_report.load(tmp_path))
    assert [m.test for m in report.misses] == ["tests/t.py::a"]
    assert report.earlier_misses == 0


def test_a_kept_verdict_and_a_failed_check_are_not_ok(tmp_path):
    sha = "6" * 40
    sticky = _push(sha, misses=2, checked=False, note="an earlier run of this commit was checked")
    sticky["sticky"] = True
    _run(tmp_path, 6, "push", sha, {"diffcone-a": {"context.json": sticky}})
    _run(tmp_path, 7, "push", sha, {"diffcone-b": {"context.json": _push(sha, 1, error=True)}})
    report = ci_report.build(ci_report.load(tmp_path))
    assert not report.ok
    assert (report.earlier_misses, report.unlisted_misses, report.check_errors) == (2, 0, 1)
    assert report.misses == []
    text = ci_report.to_markdown(report)
    assert "2 kept from an earlier run" in text and "1 check(s) failed" in text


def test_misses_the_job_counted_but_no_verdict_names(tmp_path):
    """A verdict missing from the artifact: the job's count still stands."""
    sha = "8" * 40
    _run(tmp_path, 8, "push", sha, {"diffcone-a": {"context.json": _push(sha, misses=1)}})
    report = ci_report.build(ci_report.load(tmp_path))
    assert not report.ok and report.unlisted_misses == 1
    assert "1 counted by a job whose uploaded verdicts" in ci_report.to_markdown(report)


def test_the_rerun_misses_are_the_confirmed_ones(tmp_path):
    """As the record action counts: the re-run's misses, whatever their key."""
    sha = "9" * 40
    verdict = _verdict([("tests/t.py::a", "test", False)], ["tests/t.py::a"])
    # The re-run failed to collect the file: its miss is the file.
    rerun = _verdict([("tests/t.py", "collection", False)], ["tests/t.py"])
    _run(
        tmp_path,
        9,
        "push",
        sha,
        {
            "diffcone-a": {
                "context.json": _push(sha, misses=1),
                "verdict.json": verdict,
                "rerun-verdict.json": rerun,
            }
        },
    )
    report = ci_report.build(ci_report.load(tmp_path))
    assert [(m.test, m.kind) for m in report.misses] == [("tests/t.py", "collection")]
    assert report.unlisted_misses == 0


def test_an_artifact_downloaded_without_its_directory(tmp_path):
    """gh run download -n NAME extracts into the directory itself: refused,
    not read as a run without results."""
    _write(tmp_path / "1", "context.json", _push("1" * 40))
    with pytest.raises(ci_report.ReportError, match="--pattern"):
        ci_report.load(tmp_path)
    assert main(["report", "--dir", str(tmp_path)]) == 2


def test_the_report_names_what_differed(tmp_path):
    """Roadmap item 12: run -o says why the recording was not used."""
    changed = _plan(100, 100, evidence=False)
    changed["evidence_not_used"] = {
        "reason": "environment",
        "differences": [
            "distribution orjson==3.13.0 recorded, not installed now",
            "distribution orjson==3.12.0 installed now, not recorded",
        ],
        "recording_changed_its_environment": True,
    }
    other = _plan(100, 100, evidence=False)
    other["evidence_not_used"] = {
        "reason": "environment",
        "differences": ["python: '3.12.1' recorded, '3.12.2' now", "a", "b"],
        "recording_changed_its_environment": False,
    }
    _run(
        tmp_path,
        1,
        "pull_request",
        "1" * 40,
        {"diffcone-a": {"context.json": _pr(), "ran.json": changed}},
    )
    _run(
        tmp_path,
        2,
        "pull_request",
        "2" * 40,
        {"diffcone-a": {"context.json": _pr(), "ran.json": other}},
    )
    cell = ci_report.build(ci_report.load(tmp_path)).cells[0]
    assert dict(cell.from_code) == {
        "the recording's own test run changed its environment: distribution "
        "orjson==3.13.0 recorded, not installed now; distribution orjson==3.12.0 "
        "installed now, not recorded": 1,
        "the environment differed from the recording's: python: '3.12.1' recorded, "
        "'3.12.2' now; a; 1 more": 1,
    }


def test_refusals_and_failed_plans(tmp_path):
    _run(
        tmp_path,
        1,
        "pull_request",
        "1" * 40,
        {
            "diffcone-a": {"context.json": _pr(plan_exit=3, run_exit=3)},
            # Allowed: exit 3 is a selected test that was not collected.
            "diffcone-b": {"context.json": _pr(plan_exit=3, run_exit=3, allow=True)},
            "diffcone-c": {"context.json": _pr(plan_exit=2, run_exit=None)},
        },
    )
    cells = {c.name: c for c in ci_report.build(ci_report.load(tmp_path)).cells}
    assert (cells["diffcone-a"].refused, cells["diffcone-a"].plan_failed) == (1, 0)
    assert (cells["diffcone-b"].refused, cells["diffcone-b"].plan_failed) == (0, 0)
    assert (cells["diffcone-c"].refused, cells["diffcone-c"].plan_failed) == (0, 1)


def test_artifacts_from_an_older_action(tmp_path):
    _run(tmp_path, 1, "push", "1" * 40, {"diffcone-a": {"plan.json": _plan(1, 1)}})
    report = ci_report.build(ci_report.load(tmp_path))
    assert report.ok and report.cells[0].without_context == 1
    assert "without the context" in ci_report.to_markdown(report)


def test_runs_downloaded_by_hand(tmp_path):
    """Without the action's run.json, the run is its directory's name and
    its event the context's."""
    _write(tmp_path / "9" / "diffcone-a", "context.json", _pr())
    report = ci_report.build(ci_report.load(tmp_path))
    assert (report.runs, report.pull_request_runs, report.first) == (1, 1, None)


def test_cli(tmp_path, capsys):
    sha = "7" * 40
    _run(
        tmp_path,
        1,
        "push",
        sha,
        {
            "diffcone-a": {
                "context.json": _push(sha, misses=1),
                "verdict.json": _verdict([("t.py", "collection", False)], ["t.py"]),
            }
        },
    )
    assert main(["report", "--dir", str(tmp_path), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["misses"][0]["test"] == "t.py"
    out = tmp_path / "report.md"
    assert main(["report", "--dir", str(tmp_path / "1" / "diffcone-a"), "-o", str(out)]) == 0
    assert main(["report", "--dir", str(tmp_path / "missing")]) == 2


def _context_snippet(action: str) -> str:
    """The Python the action runs to write context.json."""
    text = (ACTIONS / action / "action.yml").read_text()
    for body in re.findall(r"<<'PY'\n(.*?)\n *PY\n", text, re.S):
        if "context.json" in body:
            return textwrap.dedent(body)
    raise AssertionError(f"no context snippet in {action}")


def test_the_actions_write_what_the_report_reads(tmp_path):
    """Run the actions' own context snippets and report on what they wrote."""
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "pull_request",
        "HEAD_SHA": "c" * 40,
        "BASE_SHA": "b" * 40,
        "RECORDING": "",
        "NOTE": "no recording restored",
        "ALLOW_INCOMPLETE": "false",
    }
    pr = tmp_path / "runs" / "1" / "diffcone-a"
    (pr / "diffcone-results").mkdir(parents=True)
    subprocess.run(
        [sys.executable, "-", "3", "3"], input=_context_snippet("run"), text=True,
        cwd=pr, env=env, check=True,
    )  # fmt: skip
    (pr / "diffcone-results" / "context.json").rename(pr / "context.json")

    results = tmp_path / "runs" / "2" / "diffcone-a"
    results.mkdir(parents=True)
    (results / "check-note").write_text("the commit has no parent\n")
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "push",
        "RESULTS": str(results),
        "RECORDED": "success",
        "MISSES": "0",
        "FLAKY": "0",
        "CHECK_ERROR": "false",
    }
    describe = _context_snippet("record")
    subprocess.run([sys.executable, "-"], input=describe, text=True, env=env, check=True)

    # A recording that failed: the check step never ran.
    failed = tmp_path / "runs" / "3" / "diffcone-a"
    failed.mkdir(parents=True)
    env = {**env, "RESULTS": str(failed), "RECORDED": "failure", "MISSES": "", "FLAKY": ""}
    env["CHECK_ERROR"] = ""
    subprocess.run([sys.executable, "-"], input=describe, text=True, env=env, check=True)

    report = ci_report.build(ci_report.load(tmp_path / "runs"))
    cell = report.cells[0]
    assert report.ok
    assert (cell.pull_requests, cell.refused, cell.without_context) == (1, 1, 0)
    assert (cell.pushes, cell.checked) == (2, 0)
    assert dict(cell.not_checked) == {"the commit has no parent": 1, "the recording failed": 1}

"""``diffcone report``: a report over the results the CI actions upload."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from diffcone import ci_report
from diffcone.cli import main
from diffcone.testing import FixtureRepo

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


def test_the_share_leaves_out_targets_always_run(tmp_path):
    """Targets diffcone.toml says to run on every change are not a plan's
    saving or cost: 30 of 100 selected, 20 of them always run, is 10 of 80."""
    always = _plan(30, 100)
    always["analysis"]["counts"]["always_run"] = 20
    everything = _plan(20, 20)
    everything["analysis"]["counts"]["always_run"] = 20
    _run(
        tmp_path,
        1,
        "pull_request",
        "1" * 40,
        {"diffcone-unit": {"context.json": _pr(), "plan.json": always}},
    )
    _run(
        tmp_path,
        2,
        "pull_request",
        "2" * 40,
        {"diffcone-unit": {"context.json": _pr(), "plan.json": everything}},
    )
    report = ci_report.build(ci_report.load(tmp_path))
    (unit,) = report.cells
    assert unit.selected_shares == [0.125]  # every target always run: no share


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


def test_only_what_the_report_reads_of_a_plan_is_kept(tmp_path):
    """A plan from a recording explains each of its targets: strata's are
    78 MB of JSON, and a day of them held whole stopped the nightly report's
    runner. The report keeps the counts, the evidence and each unselected
    target's reason, and reports the same."""
    plan = _plan(1, 3, unselected=[("tests/t.py::b", "no change reaches it")])
    plan["selected_targets"] = [{"runner_id": "tests/t.py::a", "rules": ["executed_changed"]}]
    plan["dependency_explanations"] = [{"reasons": [{"detail": "x" * 1000}]}]
    plan["unselected_targets"][0]["rules"] = ["none"]
    _run(
        tmp_path,
        1,
        "pull_request",
        "1" * 40,
        {"diffcone-unit-linux": {"context.json": _pr(), "ran.json": plan}},
    )
    (art,) = ci_report.load(tmp_path)
    assert art.plan == {
        "analysis": {"counts": {"targets": 3, "selected": 1}, "evidence": {"commit": "e" * 40}},
        "unselected_targets": [{"runner_id": "tests/t.py::b", "reason": "no change reaches it"}],
    }
    assert ci_report.build([art]).cells[0].selected_shares == [1 / 3]


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
        "CHECK": "true",
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

    # A nightly recording without check: described too.
    nightly = tmp_path / "runs" / "4" / "diffcone-a"
    env = {**env, "RESULTS": str(nightly), "CHECK": "false", "RECORDED": "success"}
    env["GITHUB_EVENT_NAME"] = "schedule"
    subprocess.run([sys.executable, "-"], input=describe, text=True, env=env, check=True)
    assert (nightly / "context.json").exists()  # the step makes the directory

    report = ci_report.build(ci_report.load(tmp_path / "runs"))
    cell = report.cells[0]
    assert report.ok
    assert (cell.pull_requests, cell.refused, cell.without_context) == (1, 1, 0)
    assert (cell.pushes, cell.checked) == (3, 0)
    assert dict(cell.not_checked) == {
        "the commit has no parent": 1,
        "the recording failed": 1,
        "check is off": 1,
    }


FAKE_GH = """#!{python}
import json, os, sys
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["issue", "create"]:
    print("https://github.com/x/y/issues/7")
elif args[:2] == ["issue", "list"] and "number,title" in args:
    print(os.environ.get("GH_OPEN", "[]"))
"""


def _post(tmp_path, *, source, target, comment="always", ok="true", existing="[]"):
    """Run the action's posting step against a fake gh; return its calls."""
    if os.name == "nt":
        pytest.skip("the fake gh is a POSIX script")
    text = (ACTIONS / "report" / "action.yml").read_text()
    body = next(b for b in re.findall(r"<<'PY'\n(.*?)\n *PY\n", text, re.S) if "UPSTREAM" in b)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable))
    gh.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    (out / "report.md").write_text("### diffcone report\n")
    miss = {
        "cell": "diffcone-unit",
        "commit": "a" * 40,
        "run": "https://github.com/o/s/actions/runs/1",
        "test": "tests/test_x.py::test_y",
        "kind": "test",
        "plan_reason": "no dependency on a changed symbol",
    }
    (out / "report.json").write_text(json.dumps({"misses": [miss]}))
    log = tmp_path / "gh.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log),
        "GH_OPEN": existing,
        "OUT": str(out),
        "RUNS_REPO": source,
        "ISSUES_REPO": target,
        "COMMENT": comment,
        "OK": ok,
        "REPORT_LABEL": "diffcone-report",
        "MISS_LABEL": "diffcone-miss",
        "VERSION": "diffcone 0.3.0",
    }
    subprocess.run(
        [sys.executable, "-"], input=textwrap.dedent(body), text=True, env=env, check=True
    )
    return [json.loads(line) for line in log.read_text().splitlines()]


def _created(calls):
    return [c for c in calls if c[:2] == ["issue", "create"]]


def test_reports_into_another_repository_name_their_source(tmp_path):
    calls = _post(tmp_path, source="o/strata", target="bearing-research/diffcone")
    assert all(c[-2:] == ["--repo", "bearing-research/diffcone"] for c in calls)
    report, miss = _created(calls)
    assert report[report.index("--title") + 1] == "diffcone report: o/strata"
    title = miss[miss.index("--title") + 1]
    assert title == f"[o/strata] diffcone missed tests/test_x.py::test_y at {'a' * 12}"
    body = miss[miss.index("--body") + 1]
    assert f"<!-- diffcone-miss o/strata {'a' * 40} tests/test_x.py::test_y -->" in body
    assert "report it to diffcone" not in body  # already there
    assert any(c[:3] == ["issue", "comment", "7"] for c in calls)


def test_reports_at_home_offer_a_link_to_diffcone(tmp_path):
    calls = _post(tmp_path, source="o/app", target="o/app")
    report, miss = _created(calls)
    assert report[report.index("--title") + 1] == "diffcone report"
    body = miss[miss.index("--body") + 1]
    assert "https://github.com/bearing-research/diffcone/issues/new?" in body
    assert "tests%2Ftest_x.py%3A%3Atest_y" in body
    assert miss[miss.index("--title") + 1].startswith("diffcone missed ")


def test_the_report_issue_is_found_by_its_title(tmp_path):
    """Several projects report into one tracker: each has its own issue."""
    existing = json.dumps(
        [{"number": 3, "title": "diffcone report: o/other"},
         {"number": 5, "title": "diffcone report: o/strata"}]
    )  # fmt: skip
    calls = _post(tmp_path, source="o/strata", target="t/t", existing=existing)
    assert ["issue", "comment", "5", "--body-file"] == [
        c for c in calls if c[:2] == ["issue", "comment"]
    ][0][:4]
    assert len(_created(calls)) == 1  # the miss only


def test_on_problem_comments_only_when_something_is_wrong(tmp_path):
    quiet = _post(tmp_path / "a", source="o/a", target="o/a", comment="on-problem", ok="true")
    assert not [c for c in quiet if c[:2] == ["issue", "comment"]]
    loud = _post(tmp_path / "b", source="o/a", target="o/a", comment="on-problem", ok="false")
    assert [c for c in loud if c[:2] == ["issue", "comment"]]


FAKE_ARTIFACTS_GH = """#!{python}
import io, json, os, sys, zipfile
args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[0] == "api" and any("artifacts?per_page=100" in a for a in args):
    # What the --jq filter prints: a re-run job's artifact twice, newest last.
    print("11 diffcone-a")
    print("12 diffcone-b")
    print("13 diffcone-a")
elif args[0] == "api" and args[1].endswith("/zip"):
    attempt = args[1].split("/")[-2]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("context.json", json.dumps({{"artifact": attempt}}))
    sys.stdout.buffer.write(buffer.getvalue())
"""


def test_the_report_downloads_a_re_run_jobs_latest_artifact(tmp_path):
    """A re-run job uploads its artifact again under the same name: the
    report downloads each name once, from the latest attempt."""
    if os.name == "nt":
        pytest.skip("the fake gh is a POSIX script")
    text = (ACTIONS / "report" / "action.yml").read_text()
    start = text.index("        count=0; errors=0")
    loop = textwrap.dedent(text[start : text.index('        echo "$errors"', start)])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_ARTIFACTS_GH.format(python=sys.executable))
    gh.chmod(0o755)
    runs = tmp_path / "runs"
    (runs / "runs" / "7").mkdir(parents=True)
    (runs / "ids").write_text("7\n")
    log = tmp_path / "gh.log"
    script = f'py="{sys.executable}"\n{loop}echo "$count $errors"\n'
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log),
        "RUNS": str(runs),
        "RUNS_REPO": "o/s",
        "PREFIX": "diffcone-",
    }
    done = subprocess.run(
        ["bash", "-e", "-o", "pipefail"], input=script, text=True, env=env,
        capture_output=True, check=True,
    )  # fmt: skip
    assert done.stdout.split() == ["1", "0"]
    downloads = [c[1] for c in map(json.loads, log.read_text().splitlines()) if "/zip" in c[1]]
    assert downloads == ["repos/o/s/actions/artifacts/13/zip", "repos/o/s/actions/artifacts/12/zip"]
    context = json.loads((runs / "runs" / "7" / "diffcone-a" / "context.json").read_text())
    assert context == {"artifact": "13"}
    assert (runs / "runs" / "7" / "diffcone-b" / "context.json").exists()


# --------------------------------------------------------------------------- the actions' shell


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


def _bash(script: str, cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        [shutil.which("bash") or "bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------- record: the check


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


def _check_step(
    tmp_path, rerun_junit: str | None, *, pytest_args="", diffcone=None, failing=("test_x",),
    fake_pytest: str | None = None,
):  # fmt: skip
    """Run the check step with ``failing`` (``test_x``) new failures the
    plan did not select; the re-run (a fake pytest) writes ``rerun_junit``,
    or is ``fake_pytest`` when given. Returns the step's outputs and the
    arguments the re-run got."""
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
    names = ["test_x", "test_other", *(n for n in failing if n not in ("test_x", "test_other"))]
    plan = {
        **PLAN,
        "unselected_targets": [
            {"runner": "pytest", "runner_id": f"tests/test_a.py::{n}"} for n in names
        ],
    }
    (results / "plan.json").write_text(json.dumps(plan))
    (results / "previous.xml").write_text(_junit([], names))
    (work / "full.xml").write_text(_junit(list(failing), [n for n in names if n not in failing]))
    (results / "rerun.junit").write_text(rerun_junit or "")
    fake = bin_dir / "fakepytest"
    fake.write_text(
        fake_pytest.replace("RESULTS", str(results))
        if fake_pytest is not None
        else "#!/bin/bash\n"
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


def test_record_checks_a_push_against_its_full_run_and_fails_on_a_confirmed_miss():
    """The post-merge mode the strata setup uses: the plan of the commit's
    own change comes from the previous recording, a miss is re-run before it
    counts, and the recording is saved before the job fails."""
    from pathlib import Path

    record = (Path(__file__).resolve().parent.parent / "actions/record/action.yml").read_text()
    steps = [line.strip() for line in record.splitlines() if line.strip().startswith("- name:")]
    order = [s.split(": ", 1)[1] for s in steps]
    assert order.index("Restore the previous recording") < order.index("Plan the commit's change")
    # The verdict is reached before the recording is saved, and saved with
    # it, so a re-run of a job that found a miss finds it again.
    assert order.index("Plan the commit's change") < order.index("Record")
    assert order.index("Record") < order.index("Check the plan against this full run")
    assert order.index("Check the plan against this full run") < order.index("Save")
    assert order.index("Save") < order.index("Fail on test failures or misses")
    assert "sticky.json" in record and ".diffcone/check.json" in record
    assert "--base HEAD^1 --head HEAD" in record
    assert "rerun.xml" in record and '--baseline "$RESULTS/previous.xml"' in record


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


# A fake pytest that runs the tests it is named in order, every one failing,
# and stops as -x / --maxfail (the last one given) says: an unreached test
# has no case in the JUnit.
STOPPING_PYTEST = f"""#!{sys.executable}
import sys
args = sys.argv[1:]
open("RESULTS/rerun-args", "w").write("\\n".join(args))
maxfail, out, ids = 0, None, []
for a in args:
    if a == "-x":
        maxfail = 1
    elif a.startswith("--maxfail="):
        maxfail = int(a.split("=", 1)[1])
    elif a.startswith("--junitxml="):
        out = a.split("=", 1)[1]
    elif "::" in a:
        ids.append(a.split("::", 1)[1])
if maxfail:
    ids = ids[:maxfail]
case = '<testcase classname="tests.test_a" name="{{}}"><failure/></testcase>'
cases = "".join(case.format(n) for n in ids)
open(out, "w").write(f"<testsuites><testsuite>{{cases}}</testsuite></testsuites>")
sys.exit(1)
"""


@posix_only
def test_r1_a_rerun_runs_every_miss_despite_an_early_stop(tmp_path):
    # -x in the run's options stopped the re-run at its first failure: the
    # second miss was never run, so it was absent from the re-run's JUnit and
    # counted as passed (a real miss reported as flaky).
    got, _, args = _check_step(
        tmp_path, None, pytest_args="-x", failing=("test_x", "test_y"),
        fake_pytest=STOPPING_PYTEST,
    )  # fmt: skip
    assert args.index("--maxfail=0") > args.index("-x")
    assert (got["misses"], got["flaky"], got["check-error"]) == ("2", "0", "false")


@posix_only
def test_r1_a_miss_the_rerun_did_not_run_still_stands(tmp_path):
    # A re-run that leaves a named test out (here: it stops after one
    # failure whatever the command line says) does not make it flaky.
    stops = STOPPING_PYTEST.replace("if maxfail:", "if True:").replace("ids[:maxfail]", "ids[:1]")
    got, _, _ = _check_step(tmp_path, None, failing=("test_x", "test_y"), fake_pytest=stops)
    assert (got["misses"], got["flaky"], got["check-error"]) == ("2", "0", "false")


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


# --------------------------------------------------------------------------- recording keys


def test_r17_r18_record_keys_end_the_prefix_and_never_repeat():
    from pathlib import Path

    actions = Path(__file__).resolve().parent.parent / "actions"
    record = (actions / "record" / "action.yml").read_text()
    run = (actions / "run" / "action.yml").read_text()
    assert 'key=$PREFIX--$commit-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in record
    assert "restore-keys: ${{ inputs.key-prefix }}--\n" in run
    # R12: a recording that cannot be fetched leaves a static plan.
    assert "planning statically" in run.split("cannot be fetched", 1)[1].split("\n", 1)[0]


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


@posix_only
def test_a_shallow_checkout_is_deepened_by_depth_not_deepen(repo, tmp_path):
    # GitHub serves --deepen only from a commit a ref still points at: a
    # pull request's merge commit, once replaced (a re-run checks out its
    # first attempt's), deepens to nothing and the base never resolves.
    run = _step("run", "Plan and run")
    record = _step("record", "Plan the commit's change")
    base_sh = (ACTIONS / "base.sh").read_text()
    fetches = [line for line in (run + record + base_sh).splitlines() if "git fetch" in line]
    assert fetches and not any("--deepen" in line for line in fetches)
    assert 'git fetch --quiet --depth=2 origin "$head"' in record
    # The run action's resolve_base deepens a depth-1 clone until the base is there.
    assert 'source "$GITHUB_ACTION_PATH/../base.sh"' in run and "resolve_base" in run
    block = f'source "{ACTIONS / "base.sh"}"\nresolve_base\n'
    repo.commit({"a.py": "x = 1\n"})
    repo.commit({"a.py": "x = 2\n"})
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", f"file://{repo.path}", str(clone)], check=True
    )
    assert _bash('git rev-parse --verify --quiet "HEAD^1^{commit}"', clone).returncode != 0
    assert _bash(block, clone, {"BASE": "HEAD^1"}).returncode == 0
    assert _bash('git rev-parse --verify --quiet "HEAD^1^{commit}"', clone).returncode == 0
    # A base still missing is not blamed on the recording.
    retry = run[run.index('if [ "$rc" -eq 2 ] && [ ${#evidence[@]} -gt 0 ]') :]
    assert 'git rev-parse --verify --quiet "$BASE^{commit}"' in retry.split("then", 1)[0]


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


# --------------------------------------------------------------------------- report: artifacts


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


# --------------------------------------------------------------------------- report: its window


FAKE_WINDOW_GH = """#!/bin/bash
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
    (bin_dir / "gh").write_text(FAKE_WINDOW_GH)
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


# --------------------------------------------------------------------------- run: the base


def test_t16_a_run_that_failed_after_a_good_plan_is_not_ok(tmp_path):
    """``run`` exits 2 when it refuses (the working tree differs from the
    plan's) and passes pytest's own failures through (2 interrupted, 4
    usage error): the selected tests did not run, which the report counted
    as a good pull-request run."""
    _run(
        tmp_path, 1, "pull_request", "1" * 40,
        {
            "diffcone-a": {"context.json": _pr(run_exit=2), "plan.json": _plan(1, 1)},
            "diffcone-b": {"context.json": _pr(run_exit=4)},
            "diffcone-c": {"context.json": _pr(run_exit=1)},  # a selected test failed
        },
    )  # fmt: skip
    report = ci_report.build(ci_report.load(tmp_path))
    cells = {c.name: c for c in report.cells}
    assert [cells[n].run_failed for n in ("diffcone-a", "diffcone-b", "diffcone-c")] == [1, 1, 0]
    assert report.run_failed == 2 and not report.ok
    assert "**2 pull-request run(s) failed to run the selection**" in ci_report.to_markdown(report)
    assert ci_report.to_dict(report)["run_failed"] == 2


@posix_only
def test_t16_the_check_action_says_when_it_checked_the_plan_only(tmp_path):
    results = tmp_path / "diffcone-results"
    results.mkdir()
    plan = {
        "status": "complete",
        "selected_targets": [{"runner": "pytest", "runner_id": "tests/test_a.py::test_x"}],
        "unselected_targets": [],
    }
    (results / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "full.xml").write_text(_junit(["test_x"]))
    env = {"RESULTS": str(results), "FULL": str(tmp_path / "full.xml"), "OTHERS": "",
           "FAIL": "true", "RUNNER_TEMP": str(tmp_path),
           "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}  # fmt: skip
    done = _bash(_step("check", "Check"), tmp_path, env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "checking the plan only" in done.stdout
    summary = (tmp_path / "summary").read_text()
    assert "diffcone's run not checked" in summary and "Only the plan was checked" in summary
    (results / "diffcone.xml").write_text(_junit(["test_x"]))
    (tmp_path / "summary").unlink()
    done = _bash(_step("check", "Check"), tmp_path, env)
    assert done.returncode == 0 and "checking the plan only" not in done.stdout
    assert "### diffcone check: no miss\n" in (tmp_path / "summary").read_text()


FAKE_PLAN_DICFFONE = """#!/bin/bash
# exits 2 for a plan with a recording; without one, $STATIC
case "$*" in
  *--evidence*) exit 2 ;;
  *) exit "$STATIC" ;;
esac
"""


@posix_only
@pytest.mark.parametrize(
    ("static", "note", "error"),
    [
        ("0", "the previous recording could not be used", False),
        ("2", "the plan of the commit's change failed", True),
    ],
)
def test_t16_only_an_unusable_recording_skips_the_record_check(repo, tmp_path, static, note,
                                                              error):  # fmt: skip
    """The record action read any plan exit 2 as "the previous recording
    could not be used" and recorded the push as not checked, misses 0. Only
    a plan that succeeds without the recording says that; a plan that fails
    either way is a failed check."""
    parent = repo.commit({"a.py": "x = 1\n"})
    repo.commit({"a.py": "x = 2\n"})
    (repo.path / ".diffcone" / "evidence").mkdir(parents=True)
    (repo.path / ".diffcone" / "evidence" / f"{parent}-e.sqlite").write_bytes(b"")
    (repo.path / ".diffcone" / "baseline.xml").write_text(_junit([], ["test_x"]))
    bin_dir, results = tmp_path / "bin", tmp_path / "results"
    bin_dir.mkdir()
    (bin_dir / "diffcone").write_text(FAKE_PLAN_DICFFONE)
    (bin_dir / "diffcone").chmod(0o755)
    outputs = tmp_path / "outputs"
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "RESULTS": str(results),
           "COMMAND": "pytest", "PYTEST_ARGS": "", "ROOTS": ".", "STATIC": static,
           "GITHUB_OUTPUT": str(outputs), "GITHUB_ACTION_PATH": str(ACTIONS / "record"),
           "JUNIT": "full.xml", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}  # fmt: skip
    done = _bash(_step("record", "Plan the commit's change"), repo.path, env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (results / "check-note").read_text().strip() == note
    (repo.path / "full.xml").write_text(_junit([], ["test_x"]))
    done = _bash(_step("record", "Check the plan against this full run"), repo.path, env)
    assert done.returncode == 0, done.stdout + done.stderr
    kept = json.loads((repo.path / ".diffcone" / "check.json").read_text())
    assert kept["check_error"] is error and not kept["checked"]
    got = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    assert got["check-error"] == str(error).lower()


def _event(tmp_path: Path, name: str, payload: dict) -> dict[str, str]:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload))
    return {"GITHUB_EVENT_NAME": name, "GITHUB_EVENT_PATH": str(path)}


def _shallow(origin: FixtureRepo, sha: str, where: Path, depth: int = 1) -> Path:
    """A checkout of ``sha`` as actions/checkout makes it: fetched alone, at
    ``depth``, detached."""
    subprocess.run(["git", "init", "-q", str(where)], check=True)
    for args in (
        ["remote", "add", "origin", f"file://{origin.path}"],
        ["fetch", "-q", "--no-tags", f"--depth={depth}", "origin", f"+{sha}:refs/remotes/x"],
        ["checkout", "-q", "--detach", sha],
    ):
        subprocess.run(["git", *args], cwd=where, check=True, capture_output=True)
    return where


EVENT_BASE = f'source "{ACTIONS / "base.sh"}"\nevent_base\necho "$base|$base_note"\n'


@posix_only
def test_t4_the_base_is_the_events_own(repo, tmp_path):
    """The run action planned ``HEAD^1..HEAD``: right for a pull request's
    merge commit only. A push of several commits planned its last, a
    checkout of a pull request's head its last commit, a merge group's
    rebased head its last pull request."""
    repo.git("config", "uploadpack.allowAnySHA1InWant", "true")
    base = repo.commit({"a.py": "x = 1\n"})
    repo.git("checkout", "-q", "-b", "pr")
    repo.commit({"a.py": "x = 2\n"})
    pr_head = repo.commit({"a.py": "x = 3\n"})
    repo.git("checkout", "-q", "main")
    target = repo.commit({"b.py": "y = 1\n"})
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "pr")
    merge = repo.git("rev-parse", "HEAD")

    def found(sha, env, name):
        clone = _shallow(repo, sha, tmp_path / name)
        done = _bash(EVENT_BASE, clone, env)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip().split("|")

    pr = {"pull_request": {"base": {"sha": base}, "head": {"sha": pr_head}}}
    # The merge commit: its first parent, the target branch it merged onto.
    assert found(merge, _event(tmp_path, "pull_request", pr), "merge")[0] == target
    # The pull request's head checked out (pull_request_target, or a
    # checkout of head.sha): the pull request's base, not HEAD^1.
    for event in ("pull_request", "pull_request_target"):
        assert found(pr_head, _event(tmp_path, event, pr), f"head-{event}")[0] == base
    # A push of three commits: the commit before it.
    push = _event(tmp_path, "push", {"before": base, "after": merge})
    assert found(merge, push, "push")[0] == base
    group = _event(tmp_path, "merge_group", {"merge_group": {"base_sha": target}})
    assert found(merge, group, "group")[0] == target
    # No base: a push that created the branch, a manual run.
    created, note = found(merge, _event(tmp_path, "push", {"before": "0" * 40}), "created")
    assert created == "" and "created the branch" in note
    manual, note = found(merge, _event(tmp_path, "workflow_dispatch", {}), "manual")
    assert manual == "" and "workflow_dispatch" in note


RESOLVE_BASE = f'source "{ACTIONS / "base.sh"}"\nresolve_base\necho "$base_sha"\n'


@posix_only
def test_t4_cli7_the_base_is_fetched_whatever_names_it(repo, tmp_path):
    """The deepening fetched HEAD one level deeper, then ``origin $BASE``
    into FETCH_HEAD: ``main``, ``origin/main``, a tag and ``HEAD~3`` never
    resolved in a depth-1 checkout, and the plan exited 2."""
    repo.git("config", "uploadpack.allowAnySHA1InWant", "true")
    first = repo.commit({"a.py": "x = 0\n"})
    repo.git("tag", "v0")
    commits = [repo.commit({"a.py": f"x = {i}\n"}) for i in range(1, 5)]
    head = commits[-1]
    repo.git("checkout", "-q", "-b", "side", first)
    side = repo.commit({"b.py": "y = 1\n"})
    repo.git("checkout", "-q", "main")
    expected = {
        "HEAD^1": commits[-2],
        "HEAD~3": commits[0],
        first: first,  # a push's before, several commits back
        "main": head,
        "origin/main": head,
        "v0": first,
        side: side,  # a pull request's base, not an ancestor
    }
    for i, (name, sha) in enumerate(expected.items()):
        clone = _shallow(repo, head, tmp_path / f"c{i}")
        done = _bash(RESOLVE_BASE, clone, {"BASE": name})
        assert done.returncode == 0, (name, done.stderr)
        assert done.stdout.strip() == sha, name
        assert _bash(f"git cat-file -e {sha}^{{commit}}", clone).returncode == 0
    clone = _shallow(repo, head, tmp_path / "gone")
    assert _bash(RESOLVE_BASE, clone, {"BASE": "f" * 40}).returncode == 1
    # A complete clone is not made shallow.
    full = tmp_path / "full"
    subprocess.run(["git", "clone", "-q", f"file://{repo.path}", str(full)], check=True)
    assert _bash(RESOLVE_BASE, full, {"BASE": "v0"}).stdout.strip() == first
    shallow = _bash("git rev-parse --is-shallow-repository", full).stdout.strip()
    assert shallow == "false"


T4_OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
T4_TESTS = (
    "from pkg.ops import add, mul\n\n\n"
    "def test_add():\n    assert add(1, 1) == 2\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)


@posix_only
def test_t4_a_push_of_several_commits_plans_them_all(repo, tmp_path):
    """The run step itself, on a depth-1 checkout of a push of three
    commits whose first breaks ``test_mul``: planned ``HEAD^1..HEAD`` it
    selected nothing and passed. A push that creates the branch has no base,
    and runs every test."""
    repo.git("config", "uploadpack.allowAnySHA1InWant", "true")
    before = repo.commit(
        {"pkg/__init__.py": "", "pkg/ops.py": T4_OPS, "tests/__init__.py": "",
         "tests/test_ops.py": T4_TESTS}
    )  # fmt: skip
    repo.commit({"pkg/ops.py": T4_OPS.replace("a * b", "a * b + 1")})
    repo.commit({"pkg/other.py": "x = 1\n"})
    head = repo.commit({"pkg/other.py": "x = 2\n"})
    bin_dir = Path(sys.executable).parent

    def step(name: str, payload: dict):
        clone = _shallow(repo, head, tmp_path / name)
        outputs = tmp_path / f"{name}.out"
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_ACTION_PATH": str(ACTIONS / "run"),
            "GITHUB_OUTPUT": str(outputs),
            "COMMAND": f"{sys.executable} -m pytest -p no:cacheprovider",
            "PYTEST_ARGS": "-q",
            "ROOTS": ".",
            "BASE": "",
            "ALLOW_INCOMPLETE": "false",
            **_event(tmp_path, "push", payload),
        }
        done = _bash(_step("run", "Plan and run"), clone, env)
        results = clone / "diffcone-results"
        plan = json.loads((results / "plan.json").read_text())
        context = json.loads((results / "context.json").read_text())
        return done, {t["runner_id"] for t in plan["selected_targets"]}, context

    done, chosen, context = step("push", {"before": before, "after": head})
    assert done.returncode == 1, done.stdout + done.stderr  # test_mul ran, and fails
    assert chosen == {"tests/test_ops.py::test_mul"}
    assert context["base"] == before and context["run_exit"] == 1
    assert "before the push" in context["base_note"]

    done, chosen, context = step("created", {"before": "0" * 40, "after": head})
    assert done.returncode == 1, done.stdout + done.stderr
    assert chosen == {"tests/test_ops.py::test_add", "tests/test_ops.py::test_mul"}
    assert "created the branch" in context["base_note"]
    assert "no base to plan against" in done.stdout + done.stderr
    # Gone (a force push dropped it): every test, too.
    done, chosen, context = step("gone", {"before": "f" * 40, "after": head})
    assert chosen == {"tests/test_ops.py::test_add", "tests/test_ops.py::test_mul"}
    assert "cannot be fetched" in context["base_note"]

"""A report over the diffcone steps of many CI runs (roadmap item 11).

``diffcone report --dir DIR`` reads what the ``run`` and ``record`` actions
uploaded, downloaded into one directory per workflow run::

    DIR/<run id>/run.json                     the run: id, url, event, created_at,
                                              head_sha, pull_requests
    DIR/<run id>/<artifact>/context.json      what the action did
    DIR/<run id>/<artifact>/ran.json          the plan that ran (run)
    DIR/<run id>/<artifact>/plan.json         the plan (run before ran.json; record)
    DIR/<run id>/<artifact>/verdict.json      the check (record with check)
    DIR/<run id>/<artifact>/rerun-verdict.json  the check of the re-run misses

and reports, per artifact name (one job, or one cell of a job's matrix):
pull-request runs (selected share, plans made from the code instead of a
recording and why, refusals) and checked pushes (checked or not and why,
new failures, flaky re-runs, confirmed misses), then each confirmed miss.
It reads files only and runs nothing, like ``check``.
"""

from __future__ import annotations

import copy
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ARTIFACT_FILES = frozenset(
    {"context.json", "plan.json", "ran.json", "verdict.json", "rerun-verdict.json"}
)


# The events a recording runs on. A ``record`` context from any other run (a
# pull request from a fork runs the fork's code, which can upload anything)
# is not trusted: it could file a forged miss.
RECORD_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})


class ReportError(Exception):
    """The directory is not a report input."""


@dataclass
class Artifact:
    """One uploaded artifact of one workflow run: one job or matrix cell."""

    name: str
    run: dict[str, Any]
    context: dict[str, Any] | None
    plan: dict[str, Any] | None
    verdict: dict[str, Any] | None
    rerun: dict[str, Any] | None


@dataclass
class Miss:
    cell: str
    commit: str
    run_url: str
    test: str
    kind: str
    reason: str | None


@dataclass
class CellReport:
    name: str
    pull_requests: int = 0
    selected_shares: list[float] = field(default_factory=list)
    from_code: Counter[str] = field(default_factory=Counter)
    refused: int = 0
    plan_failed: int = 0
    not_collected: int = 0  # runs where pytest did not collect selected tests
    pushes: int = 0
    checked: int = 0
    not_checked: Counter[str] = field(default_factory=Counter)
    new_failures: int = 0
    flaky: int = 0
    misses: int = 0
    check_errors: int = 0
    without_context: int = 0
    untrusted: int = 0  # record artifacts of runs that are not recordings
    malformed: int = 0  # artifacts whose files are not what the actions write


@dataclass
class Report:
    runs: int
    pull_request_runs: int
    push_runs: int
    first: str | None
    last: str | None
    cells: list[CellReport]
    misses: list[Miss]
    earlier_misses: int = 0
    unlisted_misses: int = 0
    check_errors: int = 0
    not_collected: int = 0
    malformed: int = 0

    @property
    def ok(self) -> bool:
        """No confirmed miss, no check that failed to reach a verdict, no
        selected test left uncollected and no artifact that cannot be read."""
        return not (
            self.misses
            or self.earlier_misses
            or self.unlisted_misses
            or self.check_errors
            or self.not_collected
            or self.malformed
        )


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load(directory: str | Path) -> list[Artifact]:
    root = Path(directory)
    if not root.is_dir():
        raise ReportError(f"{root} is not a directory")
    artifacts: list[Artifact] = []
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        run = _read(run_dir / "run.json") or {"id": run_dir.name}
        loose = sorted(
            p.name for p in run_dir.iterdir() if p.is_file() and p.name in ARTIFACT_FILES
        )
        if loose:
            raise ReportError(
                f"{run_dir} holds an artifact's files ({', '.join(loose)}) instead of one "
                "directory per artifact; download with --pattern (gh run download RUN "
                "--pattern 'diffcone-*' --dir DIR/RUN) so each artifact has its own"
            )
        for art in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            artifacts.append(
                Artifact(
                    name=art.name,
                    run=run,
                    context=_read(art / "context.json"),
                    plan=_read(art / "ran.json") or _read(art / "plan.json"),
                    verdict=_read(art / "verdict.json"),
                    rerun=_read(art / "rerun-verdict.json"),
                )
            )
    return artifacts


def _confirmed(art: Artifact) -> list[tuple[str, str]]:
    """The misses a push's check confirmed, as (test, kind), counted as the
    record action counts them: what is not a test stands (nothing to
    re-run); tests stand when there is no re-run verdict (the re-run ran
    nothing), and otherwise the re-run's own misses are the confirmed ones."""
    if art.verdict is None:
        return []
    kinds = {f["test"]: f.get("kind", "unknown") for f in art.verdict.get("failures", [])}
    standing = [(test, kinds.get(test, "unknown")) for test in art.verdict.get("misses", [])]
    if art.rerun is None:
        return standing
    again = {f["test"]: f.get("kind", "unknown") for f in art.rerun.get("failures", [])}
    return [(t, k) for t, k in standing if k != "test"] + [
        (test, again.get(test, kinds.get(test, "unknown"))) for test in art.rerun.get("misses", [])
    ]


def _unused_note(plan: dict[str, Any]) -> str | None:
    """Why ``run`` did not use the recording, from what ``run -o`` wrote."""
    unused = plan.get("evidence_not_used")
    if not isinstance(unused, dict):
        return None
    differences = [str(d) for d in unused.get("differences") or []]
    shown = "; ".join(differences[:2]) + (
        f"; {len(differences) - 2} more" if len(differences) > 2 else ""
    )
    if unused.get("recording_changed_its_environment"):
        note = "the recording's own test run changed its environment"
    else:
        note = "the environment differed from the recording's"
    return f"{note}: {shown}" if shown else note


def _unselected_reason(plan: dict[str, Any] | None, test: str) -> str | None:
    for target in (plan or {}).get("unselected_targets", []):
        if target.get("runner_id") == test:
            return target.get("reason")
    return None


def _count(value: Any) -> int:
    """A count from a context: a non-negative integer, or ``ValueError``."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"not a count: {value!r}")
    count = int(value)
    if count < 0:
        raise ValueError(f"not a count: {value!r}")
    return count


@dataclass
class _Totals:
    misses: list[Miss] = field(default_factory=list)
    earlier: int = 0
    unlisted: int = 0
    check_errors: int = 0
    not_collected: int = 0


def _add(art: Artifact, context: dict[str, Any], cell: CellReport, totals: _Totals) -> None:
    """Count one artifact into its cell; raises on files the actions do not
    write (``TypeError``, ``ValueError``, ``KeyError``, ``AttributeError``)."""
    if context.get("action") == "run":
        cell.pull_requests += 1
        counts = ((art.plan or {}).get("analysis") or {}).get("counts") or {}
        # Over what diffcone decided: targets the project always runs
        # (diffcone.toml) are neither saved nor spent by a plan.
        always = _count(counts.get("always_run"))
        targets = _count(counts.get("targets"))
        if targets > always:
            share = (_count(counts.get("selected")) - always) / (targets - always)
            cell.selected_shares.append(share)
        evidence = ((art.plan or {}).get("analysis") or {}).get("evidence")
        if art.plan is not None and not evidence:
            note = (
                _unused_note(art.plan)
                or context.get("recording_note")
                or (
                    "the environment differed from the recording's"
                    if context.get("recording")
                    else "no recording"
                )
            )
            cell.from_code[str(note)] += 1
        plan_exit = context.get("plan_exit")
        refused = (
            plan_exit == 3
            and not context.get("allow_incomplete_discovery")
            and context.get("run_exit") == 3
        )
        if plan_exit not in (0, 1, 3):
            cell.plan_failed += 1
        elif refused:
            cell.refused += 1
        elif context.get("run_exit") == 3:
            # ``run`` exits 3 when pytest did not collect selected targets:
            # they did not run.
            cell.not_collected += 1
            totals.not_collected += 1
    elif context.get("action") == "record":
        cell.pushes += 1
        if context.get("checked"):
            cell.checked += 1
        else:
            cell.not_checked[str(context.get("check_note") or "not checked")] += 1
        if art.verdict is not None:
            cell.new_failures += sum(
                1 for f in art.verdict.get("failures", []) if not f.get("already_failing")
            )
        cell.flaky += _count(context.get("flaky"))
        count = _count(context.get("misses"))
        if context.get("check_error"):
            cell.check_errors += 1
            totals.check_errors += 1
            return
        cell.misses += count
        if context.get("sticky"):
            # A verdict kept from an earlier run of the commit: a count,
            # its details are in that run.
            totals.earlier += count
            return
        confirmed = _confirmed(art)
        for test, kind in confirmed:
            if not isinstance(test, str) or not isinstance(kind, str):
                raise TypeError(f"not a test name: {test!r}")
            reason = _unselected_reason(art.plan, test)
            totals.misses.append(
                Miss(
                    cell=art.name,
                    commit=str(context.get("commit") or art.run.get("head_sha") or ""),
                    run_url=str(art.run.get("url") or ""),
                    test=test,
                    kind=kind,
                    reason=None if reason is None else str(reason),
                )
            )
        # The job counted more than its verdicts name (a verdict file
        # missing from the artifact): still misses.
        totals.unlisted += max(0, count - len(confirmed))


def build(artifacts: list[Artifact]) -> Report:
    cells: dict[str, CellReport] = {}
    runs: dict[str, dict[str, Any]] = {}
    totals = _Totals()
    malformed = 0
    for art in artifacts:
        runs[str(art.run.get("id"))] = art.run
        cell = cells.setdefault(art.name, CellReport(art.name))
        context = art.context
        if context is None:
            cell.without_context += 1
            continue
        # Downloaded without run.json (by hand): the action's context knows.
        art.run.setdefault("event", context.get("event"))
        if context.get("action") == "record" and art.run.get("event") not in RECORD_EVENTS:
            cell.untrusted += 1
            continue
        # Counted into copies, so an artifact that turns out malformed
        # leaves nothing half counted.
        trial, sink = copy.deepcopy(cell), copy.deepcopy(totals)
        try:
            _add(art, context, trial, sink)
        except (TypeError, ValueError, KeyError, AttributeError):
            cell.malformed += 1
            malformed += 1
            continue
        cells[art.name], totals = trial, sink
    created = sorted(str(r["created_at"]) for r in runs.values() if r.get("created_at"))
    events = Counter(r.get("event") for r in runs.values())
    return Report(
        runs=len(runs),
        pull_request_runs=events.get("pull_request", 0) + events.get("pull_request_target", 0),
        push_runs=events.get("push", 0),
        first=created[0] if created else None,
        last=created[-1] if created else None,
        cells=sorted(cells.values(), key=lambda c: c.name),
        misses=totals.misses,
        earlier_misses=totals.earlier,
        unlisted_misses=totals.unlisted,
        check_errors=totals.check_errors,
        not_collected=totals.not_collected,
        malformed=malformed,
    )


def to_dict(report: Report) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "window": {"first": report.first, "last": report.last},
        "runs": {
            "total": report.runs,
            "pull_request": report.pull_request_runs,
            "push": report.push_runs,
        },
        "cells": [
            {
                "name": c.name,
                "pull_requests": {
                    "runs": c.pull_requests,
                    "selected_median": statistics.median(c.selected_shares)
                    if c.selected_shares
                    else None,
                    "selected_max": max(c.selected_shares) if c.selected_shares else None,
                    "planned_from_code": dict(c.from_code),
                    "refused": c.refused,
                    "plan_failed": c.plan_failed,
                    "not_collected": c.not_collected,
                },
                "pushes": {
                    "runs": c.pushes,
                    "checked": c.checked,
                    "not_checked": dict(c.not_checked),
                    "new_failures": c.new_failures,
                    "flaky": c.flaky,
                    "misses": c.misses,
                    "check_errors": c.check_errors,
                },
                "without_context": c.without_context,
                "untrusted": c.untrusted,
                "malformed": c.malformed,
            }
            for c in report.cells
        ],
        "misses": [
            {
                "cell": m.cell,
                "commit": m.commit,
                "run": m.run_url,
                "test": m.test,
                "kind": m.kind,
                "plan_reason": m.reason,
            }
            for m in report.misses
        ],
        "earlier_misses": report.earlier_misses,
        "unlisted_misses": report.unlisted_misses,
        "check_errors": report.check_errors,
        "not_collected": report.not_collected,
        "malformed": report.malformed,
    }


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _code(text: str) -> str:
    """``text`` as a Markdown code span, whatever backticks it holds (a test
    id is the project's text, and the report becomes an issue)."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or longest else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _share(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.1f} %"


def to_markdown(report: Report) -> str:
    lines = [
        f"### diffcone report: {'no miss' if report.ok else 'MISSES'}",
        "",
        f"{report.runs} workflow run(s) ({report.pull_request_runs} pull request, "
        f"{report.push_runs} push"
        + (
            f", {other} other"
            if (other := report.runs - report.pull_request_runs - report.push_runs)
            else ""
        )
        + ")"
        + (f", {report.first} to {report.last}" if report.first else "")
        + ".",
        "",
    ]
    if report.misses or report.earlier_misses or report.unlisted_misses:
        lines += [
            "**Misses**: tests that failed on a push, were not selected by the plan "
            "of its change, and failed again when re-run (or could not be re-run).",
            "",
        ]
        for m in report.misses:
            where = f"[{m.commit[:12]}]({m.run_url})" if m.run_url else m.commit[:12]
            reason = f"; the plan: {m.reason}" if m.reason else ""
            lines.append(f"- {_code(m.test)} ({m.kind}) in {m.cell} at {where}{reason}")
        if report.earlier_misses:
            lines.append(
                f"- {report.earlier_misses} kept from an earlier run of the same commit "
                "(see that run's job summary)"
            )
        if report.unlisted_misses:
            lines.append(
                f"- {report.unlisted_misses} counted by a job whose uploaded verdicts do not "
                "name them (see the jobs' summaries)"
            )
        lines.append("")
    if report.check_errors:
        lines += [
            f"**{report.check_errors} check(s) failed** to reach a verdict; see the jobs' logs.",
            "",
        ]
    if report.not_collected:
        lines += [
            f"**{report.not_collected} pull-request run(s) did not run selected tests**: "
            "pytest did not collect them (discovery and collection disagree); see the jobs' logs.",
            "",
        ]
    if report.malformed:
        lines += [
            f"**{report.malformed} artifact(s) could not be read** (their files are not what "
            "the actions write) and are left out: "
            + ", ".join(f"{c.name} ({c.malformed})" for c in report.cells if c.malformed),
            "",
        ]
    prs = [c for c in report.cells if c.pull_requests]
    if prs:
        lines += [
            "**Pull requests**",
            "",
            "| job | runs | selected (median) | selected (max) | planned from the code "
            "| refused | plan failed | selected, not collected |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for c in prs:
            median = statistics.median(c.selected_shares) if c.selected_shares else None
            peak = max(c.selected_shares) if c.selected_shares else None
            from_code = (
                ", ".join(f"{n} ({_cell(why)})" for why, n in c.from_code.most_common()) or "0"
            )
            lines.append(
                f"| {c.name} | {c.pull_requests} | {_share(median)} | {_share(peak)} | "
                f"{from_code} | {c.refused} | {c.plan_failed} | {c.not_collected} |"
            )
        lines.append("")
    pushes = [c for c in report.cells if c.pushes]
    if pushes:
        lines += [
            "**Recordings** (full runs: pushes to the default branch, scheduled runs)",
            "",
            "| job | runs | checked | not checked | new failures | flaky | misses | check errors |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for c in pushes:
            not_checked = ", ".join(f"{n} ({why})" for why, n in c.not_checked.most_common()) or "0"
            lines.append(
                f"| {c.name} | {c.pushes} | {c.checked} | {not_checked} | {c.new_failures} | "
                f"{c.flaky} | {c.misses} | {c.check_errors} |"
            )
        lines.append("")
    untrusted = [c for c in report.cells if c.untrusted]
    if untrusted:
        lines.append(
            "Recording artifacts from runs that are not recordings (a pull request), "
            "ignored: " + ", ".join(f"{c.name} ({c.untrusted})" for c in untrusted)
        )
        lines.append("")
    blind = [c for c in report.cells if c.without_context]
    if blind:
        lines.append(
            "Artifacts without the context the actions write (made by an older version?): "
            + ", ".join(f"{c.name} ({c.without_context})" for c in blind)
        )
        lines.append("")
    return "\n".join(lines)

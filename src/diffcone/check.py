"""Check a plan against what a runner found (roadmap item 9).

JUnit XML from a full pytest run says which tests failed; every one must be
among the plan's selected targets, or it is a *miss*. A failure that a
baseline run (the nightly run at the evidence commit) also had is reported
as already failing instead: the change did not cause it. Failures are
compared case by case, so a parametrized case that broke at the baseline
does not excuse a new failure of another case of the same test.

Other selective runs are read the same way, the tests in their JUnit being
the tests they ran: diffcone's own run of the plan (its outcomes should agree
with the full run's) and another selector such as pytest-testmon, compared
on the same failures.

pytest's junitxml names a test by ``classname`` and ``name``, built from the
node ID (``_pytest.junitxml.mangle_test_address``): the file path with ``/``
as ``.`` and ``.py`` dropped, then the classes, joined by ``.``; ``name`` is
the function with its parameters. The path is relative to pytest's rootdir,
which lies below the repository when the configuration file does (the
monorepo shape, ``python/pytest.ini``), while the plan names tests relative
to the repository: the rootdir the plan's discovery determined (or
``--rootdir``) is put back in front. ``--junit-prefix`` (from the plan's
pytest arguments, or given) is taken off the front of every ``classname``.
A collection error is a case whose ``classname`` is empty (the prefix
alone, with one) and whose ``name`` is the dotted file, or the dotted
directory when a directory failed to collect (its ``conftest.py`` does not
import): it stands for every target of that file or under that directory,
caught when any of them is selected. This module inverts all that against
the plan's targets (functions, parameters folded); a case matching no
target is reported as unmatched, and a failing one is a miss (the plan did
not know the test).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"
BROKEN = frozenset({FAILED, ERROR})


class CheckError(Exception):
    """An input that cannot be read."""


@dataclass(frozen=True)
class Case:
    classname: str
    name: str
    outcome: str
    time: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


def read_junit(path: str | Path) -> list[Case]:
    """Every test case in a JUnit XML file (pytest's, xunit1 or xunit2)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CheckError(f"cannot read JUnit XML {path}: {exc}") from exc
    cases = []
    for node in root.iter("testcase"):
        tags = {child.tag for child in node}
        outcome = (
            ERROR
            if "error" in tags
            else FAILED
            if "failure" in tags
            else SKIPPED
            if "skipped" in tags
            else PASSED
        )
        try:
            seconds = float(node.get("time") or 0)
        except ValueError:
            seconds = 0.0
        cases.append(Case(node.get("classname") or "", node.get("name") or "", outcome, seconds))
    return cases


def _mangle(runner_id: str, rootdir: str) -> tuple[str, str, str, list[str]]:
    """(classname, name, dotted file, dotted directories above the file)
    pytest's junitxml gives a node ID when its rootdir is ``rootdir``
    (repository-relative, ``""`` for the repository): the file relative to
    the rootdir, ``/`` as ``.``, ``.py`` dropped. A file outside the rootdir
    keeps its repository path (pytest names it with ``..`` segments, which
    no target carries; such a case stays unmatched)."""
    path, _, _params = runner_id.partition("[")
    names = path.split("::")
    file = names[0]
    if rootdir and file.startswith(rootdir + "/"):
        file = file[len(rootdir) + 1 :]
    dotted = file.replace("/", ".")
    if dotted.endswith(".py"):
        dotted = dotted[: -len(".py")]
    parts = dotted.split(".")
    directories = [".".join(parts[:i]) for i in range(1, len(parts))]
    return ".".join([dotted, *names[1:-1]]), names[-1], dotted, directories


def _normalise_rootdir(rootdir: str | None) -> str:
    """A rootdir as a repository-relative path without a trailing slash;
    ``""`` for the repository itself."""
    value = (rootdir or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    value = value.strip("/")
    return "" if value == "." else value


@dataclass
class _Targets:
    """The plan's pytest targets, findable from a JUnit case."""

    selected: set[str]
    all: set[str]
    by_case: dict[tuple[str, str], list[str]]
    by_file: dict[str, list[str]]
    by_directory: dict[str, list[str]]
    rootdir: str = ""
    prefix: str = ""

    @classmethod
    def from_plan(cls, plan: dict, rootdir: str = "", prefix: str = "") -> _Targets:
        selected: set[str] = set()
        every: set[str] = set()
        by_case: dict[tuple[str, str], list[str]] = defaultdict(list)
        by_file: dict[str, list[str]] = defaultdict(list)
        by_directory: dict[str, list[str]] = defaultdict(list)
        for key, chosen in (("selected_targets", True), ("unselected_targets", False)):
            for target in plan.get(key, ()):
                if target.get("runner") != "pytest":
                    continue
                runner_id = target["runner_id"]
                classname, name, dotted, directories = _mangle(runner_id, rootdir)
                by_case[(classname, name)].append(runner_id)
                by_file[dotted].append(runner_id)
                for directory in directories:
                    by_directory[directory].append(runner_id)
                every.add(runner_id)
                if chosen:
                    selected.add(runner_id)
        return cls(
            selected, every, dict(by_case), dict(by_file), dict(by_directory), rootdir, prefix
        )

    def collection_error(self, case: Case) -> bool:
        """Whether a case is a collector's failure (no class name, or only
        the prefix) rather than a test's."""
        return case.classname == self.prefix

    def collected(self, name: str) -> list[str]:
        """The targets a collection error of ``name`` (a dotted file or
        directory) stands for."""
        return self.by_file.get(name) or self.by_directory.get(name) or []

    def match(self, case: Case) -> list[str]:
        """The targets a case is a run of; for a collection error, every
        target of its file or under its directory."""
        if self.collection_error(case):
            return self.collected(case.name)
        classname = case.classname
        if self.prefix:
            if not classname.startswith(self.prefix + "."):
                return []
            classname = classname[len(self.prefix) + 1 :]
        return self.by_case.get((classname, case.name.partition("[")[0]), [])


def plan_mapping(plan: dict) -> tuple[str, str]:
    """(rootdir, junit prefix) the plan's pytest discovery recorded, as
    pytest will name the cases: ``""`` when it recorded none."""
    for found in plan.get("discovery", ()):
        if found.get("runner") == "pytest":
            config = found.get("config") or {}
            return _normalise_rootdir(config.get("rootdir")), str(config.get("junit_prefix") or "")
    return "", ""


@dataclass(frozen=True)
class Failure:
    test: str  # a target, or an unmatched case's key
    outcome: str
    kind: str  # test, collection (a file failed to collect) or unknown (no target)
    selected: bool
    already: bool  # the baseline run failed it too


# The ``--run`` name of diffcone's own selective run: its misses fail the check.
OWN_RUN = "diffcone"


@dataclass
class RunReport:
    name: str
    cases: int
    time: float
    ran: int  # targets with at least one case in the run
    misses: list[str] = field(default_factory=list)  # new failures it did not run
    disagreements: list[str] = field(default_factory=list)  # ran, broke in only one run


@dataclass
class CheckReport:
    plan_status: str
    discovery_incomplete: bool
    targets: int
    selected: int
    full_cases: int
    full_time: float
    selected_time: float  # the full run's time for the selected targets' cases
    unmatched: int  # full-run cases matching no target
    failures: list[Failure]
    runs: list[RunReport]
    # How the cases were mapped to targets: pytest's rootdir relative to
    # the repository ("" for the repository) and the --junit-prefix.
    rootdir: str = ""
    junit_prefix: str = ""

    @property
    def misses(self) -> list[Failure]:
        return [f for f in self.failures if not f.selected and not f.already]

    @property
    def mapping_doubtful(self) -> bool:
        """Every case of a full run that has cases matched no target: the
        rootdir or prefix the cases were mapped with is likely wrong."""
        return self.full_cases > 0 and self.unmatched == self.full_cases and self.targets > 0

    @property
    def mapping_note(self) -> str:
        where = f"rootdir {self.rootdir!r}" if self.rootdir else "the repository as rootdir"
        prefix = f" and junit prefix {self.junit_prefix!r}" if self.junit_prefix else ""
        return (
            f"no case matches a target when mapped with {where}{prefix}: if pytest's rootdir "
            "or --junit-prefix differ from what the plan recorded, pass --rootdir / "
            "--junit-prefix"
        )

    @property
    def not_run(self) -> list[str]:
        """New failures diffcone's own selective run (``--run diffcone=...``)
        did not run: the plan may have selected them, but they were never
        executed (collection disagreed, a fallback ran something else)."""
        return [t for r in self.runs if r.name == OWN_RUN for t in r.misses]

    @property
    def ok(self) -> bool:
        return not self.misses and not self.not_run

    @property
    def own_run_checked(self) -> bool:
        """Whether diffcone's own run was given (``--run diffcone=...``):
        without it the check says the plan selected every new failure, not
        that the selected tests ran."""
        return any(r.name == OWN_RUN for r in self.runs)

    @property
    def verdict(self) -> str:
        if not self.ok:
            return "MISS"
        if self.own_run_checked:
            return "OK: every new failure was selected and run"
        return (
            "OK: every new failure was selected; whether diffcone's run ran them was not "
            f"checked (no --run {OWN_RUN}=JUNIT)"
        )


def _broken(cases: list[Case], targets: _Targets) -> dict[str, tuple[str, str, set[str]]]:
    """Test -> (outcome, kind, broken case keys) for every broken case, by
    target where one matches. The keys keep a parametrized case's
    parameters: a baseline failure of ``test_x[a]`` says nothing about
    ``test_x[b]``."""
    out: dict[str, tuple[str, str, set[str]]] = {}
    for case in cases:
        if case.outcome not in BROKEN:
            continue
        matched = targets.match(case)
        if targets.collection_error(case) and matched:
            # A file or directory that failed to collect: one failure for
            # it, caught when any of its targets is selected.
            tests, kind = [case.name], "collection"
        elif matched:
            tests, kind = matched, "test"
        else:
            tests, kind = [case.key], "unknown"
        for test in tests:
            out.setdefault(test, (case.outcome, kind, set()))[2].add(case.key)
    return out


def _ran(cases: list[Case], targets: _Targets) -> set[str]:
    """The targets a run ran (and the keys of cases matching none)."""
    ran: set[str] = set()
    for case in cases:
        matched = targets.match(case)
        if not matched:
            ran.add(case.key)
        elif not targets.collection_error(case):
            ran.update(matched)
    return ran


def check(
    plan: dict,
    full: list[Case],
    *,
    baseline: list[Case] | None = None,
    runs: dict[str, list[Case]] | None = None,
    rootdir: str | None = None,
    junit_prefix: str | None = None,
) -> CheckReport:
    """``rootdir`` (pytest's, relative to the repository) and
    ``junit_prefix`` default to what the plan's discovery recorded."""
    plan_rootdir, plan_prefix = plan_mapping(plan)
    rootdir = _normalise_rootdir(rootdir) if rootdir is not None else plan_rootdir
    prefix = junit_prefix if junit_prefix is not None else plan_prefix
    targets = _Targets.from_plan(plan, rootdir, prefix)
    broken = _broken(full, targets)
    before = _broken(baseline, targets) if baseline is not None else {}
    # Already failing: every case that broke also broke at the baseline.
    already = {
        test for test, (_, _, keys) in broken.items() if test in before and keys <= before[test][2]
    }

    def selected(test: str, kind: str) -> bool:
        if kind == "collection":
            return any(t in targets.selected for t in targets.collected(test))
        return test in targets.selected

    failures = [
        Failure(test, outcome, kind, selected(test, kind), test in already)
        for test, (outcome, kind, _) in sorted(broken.items())
    ]
    new = {f.test for f in failures if not f.already}
    run_reports = []
    for name, cases in (runs or {}).items():
        ran = _ran(cases, targets)
        run_broken = _broken(cases, targets)
        # A collection failure was run when any target it stands for was.
        missed = sorted(
            f.test
            for f in failures
            if f.test in new
            and not (
                ran.intersection(targets.collected(f.test))
                if f.kind == "collection"
                else f.test in ran
            )
        )
        # A test both runs ran that broke in only one: flaky, or dependent on
        # what ran before it.
        disagreements = sorted(
            t for t in ran & targets.all if (t in run_broken) != (t in broken) and t not in before
        )
        run_reports.append(
            RunReport(
                name,
                len(cases),
                sum(c.time for c in cases),
                len(ran & targets.all),
                missed,
                disagreements,
            )
        )
    selected_time = sum(
        c.time
        for c in full
        if not targets.collection_error(c) and any(t in targets.selected for t in targets.match(c))
    )
    return CheckReport(
        plan_status=plan.get("status", "complete"),
        discovery_incomplete=bool(plan.get("discovery_incomplete")),
        targets=len(targets.all),
        selected=len(targets.selected),
        full_cases=len(full),
        full_time=sum(c.time for c in full),
        selected_time=selected_time,
        unmatched=sum(1 for c in full if not targets.match(c)),
        failures=failures,
        runs=run_reports,
        rootdir=rootdir,
        junit_prefix=prefix,
    )


def load_plan(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(f"cannot read plan {path}: {exc}") from exc
    if not isinstance(data, dict) or "selected_targets" not in data:
        raise CheckError(f"{path} is not a diffcone plan (JSON format)")
    return data


# --------------------------------------------------------------------------- reports


def to_dict(report: CheckReport) -> dict:
    return {
        "ok": report.ok,
        "not_run": report.not_run,
        "own_run_checked": report.own_run_checked,
        "plan": {
            "status": report.plan_status,
            "discovery_incomplete": report.discovery_incomplete,
            "targets": report.targets,
            "selected": report.selected,
        },
        "full_run": {
            "cases": report.full_cases,
            "test_time": round(report.full_time, 2),
            "selected_test_time": round(report.selected_time, 2),
            "unmatched_cases": report.unmatched,
        },
        "mapping": {
            "rootdir": report.rootdir,
            "junit_prefix": report.junit_prefix,
            "note": report.mapping_note if report.mapping_doubtful else None,
        },
        "failures": [
            {
                "test": f.test,
                "outcome": f.outcome,
                "kind": f.kind,
                "selected": f.selected,
                "already_failing": f.already,
            }
            for f in report.failures
        ],
        "misses": [f.test for f in report.misses],
        "runs": [
            {
                "name": r.name,
                "cases": r.cases,
                "test_time": round(r.time, 2),
                "targets_run": r.ran,
                "misses": r.misses,
                "disagreements": r.disagreements,
            }
            for r in report.runs
        ],
    }


def _share(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f} %" if whole else "-"


def to_text(report: CheckReport) -> str:
    lines = [
        f"plan: {report.selected} of {report.targets} targets selected "
        f"({_share(report.selected, report.targets)}), status {report.plan_status}"
        + (", discovery may be incomplete" if report.discovery_incomplete else ""),
        f"full run: {report.full_cases} cases, {report.full_time:.0f} s of test time; "
        f"the selected targets took {report.selected_time:.0f} s "
        f"({_share(report.selected_time, report.full_time)})",
    ]
    if report.unmatched:
        lines.append(f"  {report.unmatched} cases match no target of the plan")
    if report.mapping_doubtful:
        lines.append(f"  {report.mapping_note}")
    new = [f for f in report.failures if not f.already]
    lines.append(
        f"failures: {len(new)} new, {len(report.failures) - len(new)} already failing at "
        f"the baseline; missed by the plan: {len(report.misses)}"
    )
    for f in report.misses:
        lines.append(f"  MISSED {f.test} ({f.outcome}, {f.kind})")
    for r in report.runs:
        lines.append(
            f"run {r.name}: {r.cases} cases, {r.time:.0f} s of test time, "
            f"{len(r.misses)} new failures not run, {len(r.disagreements)} outcome "
            "disagreements with the full run"
        )
        lines.extend(f"  NOT RUN {t}" for t in r.misses)
        lines.extend(f"  DISAGREES {t}" for t in r.disagreements)
    if report.not_run:
        lines.append(
            f"diffcone's run did not run {len(report.not_run)} new failure(s) (see NOT RUN above)"
        )
    lines.append(report.verdict)
    return "\n".join(lines) + "\n"


def to_markdown(report: CheckReport) -> str:
    new = [f for f in report.failures if not f.already]
    missed = len({f.test for f in report.misses} | set(report.not_run))
    if not report.ok:
        verdict = f"**{missed} missed**"
    elif report.own_run_checked:
        verdict = "no miss"
    else:
        verdict = "no miss in the plan (diffcone's run not checked)"
    lines = [
        f"### diffcone check: {verdict}",
        "",
        "| | tests | test time | new failures not covered |",
        "|---|---|---|---|",
        f"| full run | {report.full_cases} cases | {report.full_time:.0f} s | - |",
        f"| diffcone plan | {report.selected} of {report.targets} targets "
        f"({_share(report.selected, report.targets)}) | {report.selected_time:.0f} s "
        f"({_share(report.selected_time, report.full_time)}) | {len(report.misses)} |",
    ]
    for r in report.runs:
        lines.append(
            f"| {r.name} run | {r.cases} cases | {r.time:.0f} s "
            f"({_share(r.time, report.full_time)}) | {len(r.misses)} |"
        )
    lines += [
        "",
        f"{len(new)} new failures in the full run, "
        f"{len(report.failures) - len(new)} already failing at the baseline.",
    ]
    if report.plan_status != "complete" or report.discovery_incomplete:
        lines.append(
            f"Plan status: {report.plan_status}"
            + ("; discovery may be incomplete." if report.discovery_incomplete else ".")
        )
    if report.mapping_doubtful:
        lines += ["", report.mapping_note + "."]
    if not report.own_run_checked:
        lines += [
            "",
            "Only the plan was checked: without the JUnit XML of diffcone's own run "
            f"(`--run {OWN_RUN}=...`), whether the selected tests ran is not known.",
        ]
    if report.misses:
        lines += ["", "Missed by the plan:", ""]
        lines += [f"- `{f.test}` ({f.outcome}, {f.kind})" for f in report.misses]
    for r in report.runs:
        if r.misses or r.disagreements:
            lines += ["", f"{r.name} run:", ""]
            lines += [f"- not run: `{t}`" for t in r.misses]
            lines += [f"- outcome differs from the full run: `{t}`" for t in r.disagreements]
    return "\n".join(lines) + "\n"

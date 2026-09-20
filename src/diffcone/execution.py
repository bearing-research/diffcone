"""Execution integration: run selected targets, and validate plans.

Planning never executes project code. Everything in this module runs
*after* a plan exists and is invoked only by the ``run`` and ``validate``
commands, in a subprocess, with a command the user controls.

``run``      executes the selected targets of a plan with the runner's CLI.
``validate`` runs the full pytest suite at both snapshots (in temporary
             ``git worktree`` checkouts for commits, in place for WORKTREE)
             and checks that every test whose outcome changed was selected.
             This is outcome-based validation: a test whose behaviour
             changed without changing its pass/fail outcome is not detected.
             With ``coverage=True`` it also runs the head suite under
             pytest-cov with per-test contexts and checks that every test
             that *executed* a changed symbol was selected, which measures
             recall (and reports precision) against dynamic ground truth.
"""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from diffcone.classify import DELETED
from diffcone.manifest import Target
from diffcone.model import KIND_COMMIT, KIND_WORKTREE, Symbol
from diffcone.planner import Plan
from diffcone.snapshot import GitError, _git

DEFAULT_COMMANDS = {"pytest": "python -m pytest", "asv": "asv run"}

_PYTEST_LINE = re.compile(
    r"^(?P<nodeid>\S+::\S+?) (?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)


# --------------------------------------------------------------------------- run


def build_command(
    runner: str, targets: list[Target], command: str | None, extra: list[str]
) -> list[str]:
    """The command line that runs exactly ``targets`` with ``runner``."""
    base = shlex.split(command or DEFAULT_COMMANDS[runner])
    ids = [t.runner_id for t in sorted(targets)]
    if runner == "pytest":
        return [*base, *extra, *ids]
    if runner == "asv":
        pattern = "^(" + "|".join(re.escape(i) for i in ids) + ")$"
        return [*base, *extra, "--bench", pattern]
    raise ValueError(f"unknown runner {runner!r}")


@dataclass
class RunResult:
    runner: str
    command: list[str]
    selected: list[Target]
    total: int
    returncode: int | None  # None when nothing was run (dry run or empty selection)


def run_selected(
    plan: Plan,
    runner: str,
    *,
    cwd: Path,
    command: str | None = None,
    extra: list[str] | None = None,
    dry_run: bool = False,
) -> RunResult:
    selected = [d.target for d in plan.decisions if d.selected and d.target.runner == runner]
    total = sum(1 for d in plan.decisions if d.target.runner == runner)
    argv = build_command(runner, selected, command, list(extra or [])) if selected else []
    result = RunResult(runner, argv, selected, total, None)
    if dry_run or not selected:
        return result
    proc = subprocess.run(argv, cwd=cwd)
    result.returncode = proc.returncode
    return result


# --------------------------------------------------------------------------- validate


@dataclass(frozen=True)
class TargetOutcome:
    runner_id: str
    base: str | None  # None: absent at that snapshot
    head: str | None
    selected: bool

    @property
    def changed(self) -> bool:
        return self.base != self.head


@dataclass(frozen=True)
class CoverageHit:
    runner_id: str
    selected: bool
    executed_changed: tuple[str, ...]  # changed symbols whose lines the test ran


@dataclass
class CoverageValidation:
    hits: list[CoverageHit] = field(default_factory=list)  # one per test seen under coverage
    changed_symbols: tuple[str, ...] = ()
    log: str = ""

    @property
    def affected(self) -> list[CoverageHit]:
        return [h for h in self.hits if h.executed_changed]

    @property
    def caught(self) -> list[CoverageHit]:
        return [h for h in self.affected if h.selected]

    @property
    def missed(self) -> list[CoverageHit]:
        return [h for h in self.affected if not h.selected]

    @property
    def recall(self) -> float | None:
        return len(self.caught) / len(self.affected) if self.affected else None

    @property
    def precision(self) -> float | None:
        selected = [h for h in self.hits if h.selected]
        return len(self.caught) / len(selected) if selected else None


@dataclass
class Validation:
    runner: str
    command: list[str]
    outcomes: list[TargetOutcome] = field(default_factory=list)
    base_log: str = ""
    head_log: str = ""
    coverage: CoverageValidation | None = None

    @property
    def caught(self) -> list[TargetOutcome]:
        return [o for o in self.outcomes if o.changed and o.selected]

    @property
    def missed(self) -> list[TargetOutcome]:
        return [o for o in self.outcomes if o.changed and not o.selected]

    @property
    def selected_count(self) -> int:
        return sum(1 for o in self.outcomes if o.selected)

    @property
    def ok(self) -> bool:
        return not self.missed and (self.coverage is None or not self.coverage.missed)


def parse_pytest_verbose(output: str) -> dict[str, str]:
    """Map pytest node ids (parameter cases folded into their function) to
    the worst outcome observed for that function."""
    rank = {"PASSED": 0, "SKIPPED": 0, "XFAIL": 0, "XPASS": 1, "FAILED": 2, "ERROR": 3}
    outcomes: dict[str, str] = {}
    for line in output.splitlines():
        m = _PYTEST_LINE.match(line.strip())
        if not m:
            continue
        nodeid = re.sub(r"\[.*\]$", "", m.group("nodeid"))
        outcome = m.group("outcome")
        current = outcomes.get(nodeid)
        if current is None or rank[outcome] > rank[current]:
            outcomes[nodeid] = outcome
    return outcomes


class _Checkout:
    """A directory holding a snapshot: a temporary detached worktree for a
    commit, or the repository itself for WORKTREE."""

    def __init__(self, repo: Path, kind: str, commit: str) -> None:
        self.repo = repo
        self.kind = kind
        self.commit = commit
        self.path = repo
        self._tmp: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        if self.kind == KIND_COMMIT:
            self._tmp = tempfile.TemporaryDirectory(prefix="diffcone-validate-")
            self.path = Path(self._tmp.name) / "checkout"
            _git(self.repo, ["worktree", "add", "--detach", "-q", str(self.path), self.commit])
        elif self.kind != KIND_WORKTREE:
            raise GitError("validate supports commit and WORKTREE snapshots, not INDEX")
        return self.path

    def __exit__(self, *exc: object) -> None:
        if self._tmp is not None:
            try:
                _git(self.repo, ["worktree", "remove", "--force", str(self.path)])
            except GitError:
                pass
            self._tmp.cleanup()


def _run_full_pytest(cwd: Path, command: str | None) -> tuple[dict[str, str], str]:
    argv = [
        *shlex.split(command or DEFAULT_COMMANDS["pytest"]),
        "-v",
        "-p",
        "no:cacheprovider",
        "--no-header",
        "-rN",
    ]
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    log = proc.stdout + proc.stderr
    return parse_pytest_verbose(proc.stdout), log


# --------------------------------------------------------------------------- coverage


def _numbits_to_lines(blob: bytes) -> list[int]:
    """Decode coverage.py's numbits bitmap: bit n set means line n executed."""
    lines: list[int] = []
    for byte_index, byte in enumerate(blob):
        for bit in range(8):
            if byte & (1 << bit):
                lines.append(byte_index * 8 + bit)
    return lines


def read_coverage_contexts(db_path: Path, root: Path) -> dict[str, dict[str, set[int]]]:
    """Per pytest node id (parameter cases and setup/run/teardown phases
    folded), the executed lines per repo-relative file."""
    result: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT context.context, file.path, line_bits.numbits FROM line_bits "
            "JOIN context ON context.id = line_bits.context_id "
            "JOIN file ON file.id = line_bits.file_id"
        ).fetchall()
    finally:
        con.close()
    root = root.resolve()
    for context, path, numbits in rows:
        if not context:
            continue  # collection / import time, not attributable to one test
        nodeid = context.split("|", 1)[0]
        nodeid = re.sub(r"\[.*\]$", "", nodeid)
        try:
            rel = str(Path(path).resolve().relative_to(root))
        except ValueError:
            continue
        result[nodeid][rel].update(_numbits_to_lines(numbits))
    return result


def _changed_head_symbols(plan: Plan) -> list[Symbol]:
    return [
        c.head
        for c in plan.changes
        if c.head is not None and DELETED not in c.changes and c.head.line_ranges
    ]


def _run_coverage_pytest(
    cwd: Path, command: str | None
) -> tuple[Path, str, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory(prefix="diffcone-cov-")
    db = Path(tmp.name) / ".coverage"
    argv = [
        *shlex.split(command or DEFAULT_COMMANDS["pytest"]),
        "-p",
        "no:cacheprovider",
        "-q",
        "--cov=.",
        "--cov-context=test",
        "--cov-report=",
        "-o",
        "addopts=",
    ]
    env = dict(os.environ, COVERAGE_FILE=str(db))
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env)
    log = proc.stdout + proc.stderr
    if not db.exists():
        tmp.cleanup()
        raise GitError(
            "coverage validation produced no coverage database; is pytest-cov installed in the "
            f"environment that runs {argv[0]!r}?\n{log[-2000:]}"
        )
    return db, log, tmp


def coverage_validation(plan: Plan, head_dir: Path, command: str | None) -> CoverageValidation:
    db, log, tmp = _run_coverage_pytest(head_dir, command)
    try:
        contexts = read_coverage_contexts(db, head_dir)
    finally:
        tmp.cleanup()
    changed = _changed_head_symbols(plan)
    by_path: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in changed:
        by_path[symbol.path].append(symbol)
    selected = {
        d.target.runner_id for d in plan.decisions if d.selected and d.target.runner == "pytest"
    }
    hits: list[CoverageHit] = []
    for nodeid in sorted(contexts):
        executed: set[str] = set()
        for rel, lines in contexts[nodeid].items():
            for symbol in by_path.get(rel, ()):
                if any(symbol.covers_line(line) for line in lines):
                    executed.add(symbol.id)
        hits.append(CoverageHit(nodeid, nodeid in selected, tuple(sorted(executed))))
    return CoverageValidation(hits, tuple(sorted(s.id for s in changed)), log)


def validate_pytest(
    plan: Plan, *, repo: Path, command: str | None = None, coverage: bool = False
) -> Validation:
    """Run the full suite at base and head and compare outcome changes with
    the plan's pytest selection; with ``coverage`` also check dynamic
    execution of changed symbols at head."""
    if plan.head.kind == KIND_WORKTREE and plan.base.kind == KIND_WORKTREE:
        raise GitError("validate needs at least one committed snapshot")
    with _Checkout(repo, plan.base.kind, plan.base.commit) as base_dir:
        base_outcomes, base_log = _run_full_pytest(base_dir, command)
    cov: CoverageValidation | None = None
    with _Checkout(repo, plan.head.kind, plan.head.commit) as head_dir:
        head_outcomes, head_log = _run_full_pytest(head_dir, command)
        if coverage:
            cov = coverage_validation(plan, head_dir, command)
    selected = {
        d.target.runner_id for d in plan.decisions if d.selected and d.target.runner == "pytest"
    }
    known = {d.target.runner_id for d in plan.decisions if d.target.runner == "pytest"}
    ids = sorted(known | set(base_outcomes) | set(head_outcomes))
    validation = Validation(
        "pytest",
        [*shlex.split(command or DEFAULT_COMMANDS["pytest"]), "-v"],
        base_log=base_log,
        head_log=head_log,
        coverage=cov,
    )
    for runner_id in ids:
        validation.outcomes.append(
            TargetOutcome(
                runner_id,
                base_outcomes.get(runner_id),
                head_outcomes.get(runner_id),
                runner_id in selected,
            )
        )
    return validation


def validation_to_dict(v: Validation) -> dict:
    return {
        "runner": v.runner,
        "ok": v.ok,
        "counts": {
            "targets": len(v.outcomes),
            "selected": v.selected_count,
            "outcome_changed": sum(1 for o in v.outcomes if o.changed),
            "caught": len(v.caught),
            "missed": len(v.missed),
        },
        "missed": [{"runner_id": o.runner_id, "base": o.base, "head": o.head} for o in v.missed],
        "caught": [{"runner_id": o.runner_id, "base": o.base, "head": o.head} for o in v.caught],
        "outcomes": [
            {"runner_id": o.runner_id, "base": o.base, "head": o.head, "selected": o.selected}
            for o in v.outcomes
        ],
        "coverage": None
        if v.coverage is None
        else {
            "changed_symbols": list(v.coverage.changed_symbols),
            "counts": {
                "tests": len(v.coverage.hits),
                "executed_a_changed_symbol": len(v.coverage.affected),
                "caught": len(v.coverage.caught),
                "missed": len(v.coverage.missed),
            },
            "recall": v.coverage.recall,
            "precision": v.coverage.precision,
            "missed": [
                {"runner_id": h.runner_id, "executed": list(h.executed_changed)}
                for h in v.coverage.missed
            ],
            "hits": [
                {
                    "runner_id": h.runner_id,
                    "selected": h.selected,
                    "executed": list(h.executed_changed),
                }
                for h in v.coverage.hits
            ],
        },
    }


def validation_to_text(v: Validation) -> str:
    lines = [
        f"validation ({v.runner}): {'OK' if v.ok else 'MISSED OUTCOME CHANGES'}",
        f"  targets: {len(v.outcomes)}, selected: {v.selected_count}, "
        f"outcome changed: {sum(1 for o in v.outcomes if o.changed)} "
        f"(caught {len(v.caught)}, missed {len(v.missed)})",
    ]
    for o in v.missed:
        lines.append(f"  MISSED {o.runner_id}: {o.base} -> {o.head}")
    for o in v.caught:
        lines.append(f"  caught {o.runner_id}: {o.base} -> {o.head}")
    if v.coverage is None:
        lines.append(
            "  note: outcome-based only; behaviour changes that keep the same pass/fail outcome "
            "are invisible to this check (use --coverage)"
        )
    else:
        c = v.coverage
        fmt = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
        lines.append(
            f"coverage: {len(c.affected)} of {len(c.hits)} test(s) executed a changed symbol; "
            f"recall {fmt(c.recall)} (caught {len(c.caught)}, missed {len(c.missed)}), "
            f"precision {fmt(c.precision)}"
        )
        for h in c.missed:
            lines.append(f"  MISSED {h.runner_id}: executed {', '.join(h.executed_changed)}")
    return "\n".join(lines) + "\n"

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
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from diffcone.manifest import Target
from diffcone.model import KIND_COMMIT, KIND_WORKTREE
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


@dataclass
class Validation:
    runner: str
    command: list[str]
    outcomes: list[TargetOutcome] = field(default_factory=list)
    base_log: str = ""
    head_log: str = ""

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
        return not self.missed


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


def validate_pytest(plan: Plan, *, repo: Path, command: str | None = None) -> Validation:
    """Run the full suite at base and head and compare outcome changes with
    the plan's pytest selection."""
    if plan.head.kind == KIND_WORKTREE and plan.base.kind == KIND_WORKTREE:
        raise GitError("validate needs at least one committed snapshot")
    with _Checkout(repo, plan.base.kind, plan.base.commit) as base_dir:
        base_outcomes, base_log = _run_full_pytest(base_dir, command)
    with _Checkout(repo, plan.head.kind, plan.head.commit) as head_dir:
        head_outcomes, head_log = _run_full_pytest(head_dir, command)
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
    lines.append(
        "  note: outcome-based; behaviour changes that keep the same pass/fail outcome are "
        "invisible to this check"
    )
    return "\n".join(lines) + "\n"

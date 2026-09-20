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
from collections.abc import Iterator
from contextlib import contextmanager
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
    known_at_head: bool = True  # a target of the plan (exists in the head snapshot)

    @property
    def changed(self) -> bool:
        return self.base != self.head

    @property
    def removed(self) -> bool:
        """Ran at base, gone at head: nothing to select, so never a miss."""
        return self.head is None and not self.known_at_head


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
        return [o for o in self.outcomes if o.changed and not o.selected and not o.removed]

    @property
    def removed(self) -> list[TargetOutcome]:
        return [o for o in self.outcomes if o.removed]

    @property
    def selected_count(self) -> int:
        return sum(1 for o in self.outcomes if o.selected)

    @property
    def ok(self) -> bool:
        return not self.missed and (self.coverage is None or not self.coverage.missed)


def fold_nodeid(nodeid: str) -> str:
    """Drop the parameter case (``[...]``) so a node id names the test function."""
    return re.sub(r"\[.*\]$", "", nodeid)


def parse_pytest_verbose(output: str) -> dict[str, str]:
    """Map pytest node ids (parameter cases folded into their function) to
    the worst outcome observed for that function."""
    rank = {"PASSED": 0, "SKIPPED": 0, "XFAIL": 0, "XPASS": 1, "FAILED": 2, "ERROR": 3}
    outcomes: dict[str, str] = {}
    for line in output.splitlines():
        m = _PYTEST_LINE.match(line.strip())
        if not m:
            continue
        nodeid = fold_nodeid(m.group("nodeid"))
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


@dataclass
class _SuiteRun:
    outcomes: dict[str, str]
    log: str
    returncode: int
    coverage_db: Path | None = None


def _checkout_env(cwd: Path, source_roots: list[str]) -> dict[str, str]:
    """Environment that makes the checkout's own code win over an installed
    (typically editable) copy of the project: its source roots go first on
    PYTHONPATH. With a ``src`` layout the current directory alone would not
    do it, and the suite would silently test the installed code."""
    env = dict(os.environ)
    roots = []
    for root in source_roots:
        candidate = (cwd / root).resolve() if root not in ("", ".") else cwd.resolve()
        if candidate.is_dir() and str(candidate) not in roots:
            roots.append(str(candidate))
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([*roots, *([existing] if existing else [])])
    return env


@contextmanager
def _run_full_pytest(
    cwd: Path, command: str | None, *, coverage: bool = False, source_roots: list[str] = ()
) -> Iterator[_SuiteRun]:
    """Run the whole suite once with ``-v``; with ``coverage`` the same run
    also records per-test coverage contexts into a temporary database that
    lives for the duration of the context."""
    argv = [
        *shlex.split(command or DEFAULT_COMMANDS["pytest"]),
        "-v",
        "-p",
        "no:cacheprovider",
        "--no-header",
        "-rN",
    ]
    with tempfile.TemporaryDirectory(prefix="diffcone-cov-") as tmp:
        env = _checkout_env(cwd, list(source_roots))
        db: Path | None = None
        if coverage:
            db = Path(tmp) / ".coverage"
            # The project's own addopts stay in force so this run collects the
            # same tests as a plain run; only the coverage options are added.
            # The project's coverage config is replaced: its ``source``/``omit``
            # (typically excluding tests) would blind attribution, and
            # ``parallel``/``branch`` change the database layout.
            rc = Path(tmp) / "coveragerc"
            rc.write_text("[run]\nbranch = false\nparallel = false\nrelative_files = false\n")
            argv += ["--cov=.", "--cov-context=test", "--cov-report=", f"--cov-config={rc}"]
            env["COVERAGE_FILE"] = str(db)
            # coverage.py 7.x defaults to the sys.monitoring core on Python
            # 3.12+, which disables a line after its first hit: with per-test
            # contexts only the *first* test to run a line gets credit for it.
            # coverage avoids that core only for contexts set in its own config,
            # not for pytest-cov's switch_context(), so force the C tracer (it
            # falls back to the pure-Python tracer when unavailable).
            env.setdefault("COVERAGE_CORE", "ctrace")
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env)
        log = proc.stdout + proc.stderr
        if coverage and not (db and db.exists()):
            raise GitError(
                "coverage validation produced no coverage database; is pytest-cov installed in "
                f"the environment that runs {argv[0]!r} (and not disabled by --no-cov)?\n"
                f"exit code {proc.returncode}\n{log[-2000:]}"
            )
        yield _SuiteRun(parse_pytest_verbose(proc.stdout), log, proc.returncode, db)


# --------------------------------------------------------------------------- coverage


def _numbits_to_lines(blob: bytes) -> list[int]:
    """Decode coverage.py's numbits bitmap: bit n set means line n executed."""
    lines: list[int] = []
    for byte_index, byte in enumerate(blob):
        for bit in range(8):
            if byte & (1 << bit):
                lines.append(byte_index * 8 + bit)
    return lines


def read_coverage_contexts(
    db_path: Path, root: Path, outside: set[str] | None = None
) -> dict[str, dict[str, set[int]]]:
    """Per pytest node id (parameter cases and setup/run/teardown phases
    folded), the executed lines per checkout-relative file. Measured files
    that lie outside the checkout are collected into ``outside`` when given.

    Reads both the ``line_bits`` table (line coverage) and the ``arc`` table
    (branch coverage, which coverage.py uses *instead* when ``branch = True``
    is configured). Paths may be stored relative when the project sets
    ``relative_files``; they are resolved against the checkout root.
    """
    result: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    root = root.resolve()

    def rel_path(path: str) -> str | None:
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        try:
            return str(p.resolve().relative_to(root))
        except ValueError:
            if outside is not None:
                outside.add(str(p))
            return None

    con = sqlite3.connect(db_path)
    try:
        line_rows = con.execute(
            "SELECT context.context, file.path, line_bits.numbits FROM line_bits "
            "JOIN context ON context.id = line_bits.context_id "
            "JOIN file ON file.id = line_bits.file_id"
        ).fetchall()
        arc_rows = con.execute(
            "SELECT context.context, file.path, arc.fromno, arc.tono FROM arc "
            "JOIN context ON context.id = arc.context_id "
            "JOIN file ON file.id = arc.file_id"
        ).fetchall()
    finally:
        con.close()
    for context, path, numbits in line_rows:
        rel = rel_path(path) if context else None
        if rel is not None:
            result[fold_nodeid(context.split("|", 1)[0])][rel].update(_numbits_to_lines(numbits))
    for context, path, fromno, tono in arc_rows:
        rel = rel_path(path) if context else None
        if rel is not None:
            lines = result[fold_nodeid(context.split("|", 1)[0])][rel]
            # Negative numbers mark entry/exit arcs; abs() gives the real line.
            lines.update(n for n in (abs(fromno), abs(tono)) if n > 0)
    return result


def _line_owner_index(plan: Plan) -> tuple[dict[str, dict[int, set[str]]], tuple[str, ...]]:
    """``{path: {line: changed symbol ids}}`` for the changed head symbols.

    A container (module or class) owns only the lines outside its members'
    definitions, mirroring how the planner treats body changes; when its
    change is structural (which invalidates every member) all its lines
    count, mirroring the ``defined_in`` propagation rule.
    """
    head = plan.head_index
    members_of: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for symbol in head.symbols.values():
        if symbol.container is not None:
            members_of[symbol.container].extend(symbol.line_ranges)
    index: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    changed_ids: list[str] = []
    for change in plan.changes:
        symbol = change.head
        # Additive-only changes carry no impact for the planner and mean no
        # behaviour change for the code executed, so they are not ground truth.
        if symbol is None or not symbol.line_ranges or not change.carries_impact:
            continue
        changed_ids.append(symbol.id)
        excluded: set[int] = set()
        if not change.structural:
            for start, end in members_of.get(symbol.id, ()):
                excluded.update(range(start, end + 1))
        for start, end in symbol.line_ranges:
            for line in range(start, end + 1):
                if line not in excluded:
                    index[symbol.path][line].add(symbol.id)
    return index, tuple(sorted(changed_ids))


def shadowed_files(plan: Plan, outside: set[str]) -> list[tuple[str, str]]:
    """Measured files outside the checkout that have the same source-root-
    relative path as a file inside it: the suite imported an installed copy
    of the project instead of the checkout."""
    suffixes: dict[str, str] = {}
    for symbol in plan.head_index.symbols.values():
        rel = symbol.path
        suffixes["/" + rel] = rel
        for root in plan.source_roots:
            root = root.strip("/")
            if root and root != "." and rel.startswith(root + "/"):
                suffixes["/" + rel[len(root) + 1 :]] = rel
    found: list[tuple[str, str]] = []
    for path in sorted(outside):
        for suffix, rel in suffixes.items():
            if path.endswith(suffix):
                found.append((path, rel))
                break
    return found


def coverage_validation(
    plan: Plan, run: _SuiteRun, head_dir: Path, selected: set[str]
) -> CoverageValidation:
    assert run.coverage_db is not None
    outside: set[str] = set()
    contexts = read_coverage_contexts(run.coverage_db, head_dir, outside)
    shadowing = shadowed_files(plan, outside)
    if shadowing:
        listing = "\n".join(f"  {path}  (shadows {rel})" for path, rel in shadowing[:5])
        raise GitError(
            "the suite imported project code from outside the checkout, so the validation "
            "would test the wrong revision:\n"
            f"{listing}\nPut the checkout's source roots first on PYTHONPATH (diffcone does "
            "this for --source-root entries; pass the roots that hold the package) or run "
            "against an environment without an installed copy of the project."
        )
    if not contexts:
        raise GitError(
            "coverage validation recorded no per-test contexts: the head suite collected no "
            f"tests under coverage (exit code {run.returncode})\n{run.log[-2000:]}"
        )
    owners, changed_ids = _line_owner_index(plan)
    hits: list[CoverageHit] = []
    for nodeid in sorted(contexts):
        executed: set[str] = set()
        for rel, lines in contexts[nodeid].items():
            by_line = owners.get(rel)
            if by_line:
                for line in lines:
                    executed.update(by_line.get(line, ()))
        hits.append(CoverageHit(nodeid, nodeid in selected, tuple(sorted(executed))))
    return CoverageValidation(hits, changed_ids, run.log)


OutcomeCache = dict[str, dict[str, str]]  # commit sha -> per-test outcomes


def validate_pytest(
    plan: Plan,
    *,
    repo: Path,
    command: str | None = None,
    coverage: bool = False,
    outcome_cache: OutcomeCache | None = None,
) -> Validation:
    """Run the full suite at base and head and compare outcome changes with
    the plan's pytest selection; with ``coverage`` the head run also records
    per-test coverage and every test that executed a changed symbol must be
    selected. ``outcome_cache`` lets consecutive validations (a corpus) reuse
    a committed snapshot's outcomes instead of running its suite again."""
    if plan.head.kind == KIND_WORKTREE and plan.base.kind == KIND_WORKTREE:
        raise GitError("validate needs at least one committed snapshot")
    selected = {
        d.target.runner_id for d in plan.decisions if d.selected and d.target.runner == "pytest"
    }
    cache = outcome_cache if outcome_cache is not None else {}
    base_log = ""
    if plan.base.kind == KIND_COMMIT and plan.base.commit in cache:
        base_outcomes = cache[plan.base.commit]
    else:
        with _Checkout(repo, plan.base.kind, plan.base.commit) as base_dir:
            with _run_full_pytest(base_dir, command, source_roots=plan.source_roots) as base_run:
                base_outcomes, base_log = base_run.outcomes, base_run.log
        if plan.base.kind == KIND_COMMIT:
            cache[plan.base.commit] = base_outcomes
    cov: CoverageValidation | None = None
    with _Checkout(repo, plan.head.kind, plan.head.commit) as head_dir:
        with _run_full_pytest(
            head_dir, command, coverage=coverage, source_roots=plan.source_roots
        ) as head_run:
            head_outcomes, head_log = head_run.outcomes, head_run.log
            if coverage:
                cov = coverage_validation(plan, head_run, head_dir, selected)
    if plan.head.kind == KIND_COMMIT:
        cache[plan.head.commit] = head_outcomes
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
                known_at_head=runner_id in known,
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
        "removed": [o.runner_id for o in v.removed],
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


def _status_label(v: Validation) -> str:
    if v.ok:
        return "OK"
    reasons = []
    if v.missed:
        reasons.append("outcome")
    if v.coverage is not None and v.coverage.missed:
        reasons.append("coverage")
    return f"MISSED ({', '.join(reasons)})"


def validation_to_text(v: Validation) -> str:
    lines = [
        f"validation ({v.runner}): {_status_label(v)}",
        f"  targets: {len(v.outcomes)}, selected: {v.selected_count}, "
        f"outcome changed: {sum(1 for o in v.outcomes if o.changed)} "
        f"(caught {len(v.caught)}, missed {len(v.missed)})",
    ]
    for o in v.missed:
        lines.append(f"  MISSED {o.runner_id}: {o.base} -> {o.head}")
    if v.removed:
        lines.append(f"  removed at head (not misses): {len(v.removed)}")
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


# --------------------------------------------------------------------------- corpus


@dataclass
class CorpusEntry:
    commit: str
    parent: str
    subject: str
    skipped: str | None = None  # reason, when no validation ran
    error: str | None = None
    changed_symbols: int = 0
    targets: int = 0
    selected: int = 0
    degraded: bool = False
    validation: Validation | None = None

    @property
    def savings(self) -> float | None:
        return 1 - self.selected / self.targets if self.targets else None


@dataclass
class CorpusReport:
    repo: str
    revision_range: str
    coverage: bool
    entries: list[CorpusEntry] = field(default_factory=list)

    @property
    def validated(self) -> list[CorpusEntry]:
        return [e for e in self.entries if e.validation is not None]

    def _sum(self, pick) -> int:
        return sum(pick(e.validation) for e in self.validated)

    @property
    def outcome_changed(self) -> int:
        return self._sum(lambda v: sum(1 for o in v.outcomes if o.changed))

    @property
    def outcome_missed(self) -> int:
        return self._sum(lambda v: len(v.missed))

    @property
    def coverage_affected(self) -> int:
        return self._sum(lambda v: len(v.coverage.affected) if v.coverage else 0)

    @property
    def coverage_caught(self) -> int:
        return self._sum(lambda v: len(v.coverage.caught) if v.coverage else 0)

    @property
    def coverage_selected(self) -> int:
        return self._sum(
            lambda v: sum(1 for h in v.coverage.hits if h.selected) if v.coverage else 0
        )

    @property
    def recall(self) -> float | None:
        return self.coverage_caught / self.coverage_affected if self.coverage_affected else None

    @property
    def precision(self) -> float | None:
        return self.coverage_caught / self.coverage_selected if self.coverage_selected else None

    @property
    def mean_savings(self) -> float | None:
        values = [e.savings for e in self.validated if e.savings is not None]
        return sum(values) / len(values) if values else None

    @property
    def ok(self) -> bool:
        return all(e.validation.ok for e in self.validated) and not any(
            e.error for e in self.entries
        )


def _commits_in_range(repo: Path, revision_range: str) -> list[tuple[str, str, str]]:
    """(commit, first parent, subject) for each commit in ``A..B``, oldest first."""
    out = _git(
        repo,
        [
            "rev-list",
            "--reverse",
            "--first-parent",
            "--format=%H %P%x00%s",
            "--no-commit-header",
            revision_range,
        ],
    )
    entries: list[tuple[str, str, str]] = []
    for line in out.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        ids, _, subject = line.partition("\0")
        parts = ids.split()
        if len(parts) < 2:
            continue  # a root commit has no parent to compare against
        entries.append((parts[0], parts[1], subject.strip()))
    return entries


def _touches_python(repo: Path, parent: str, commit: str) -> bool:
    out = _git(repo, ["diff", "--name-only", parent, commit])
    return any(line.endswith(".py") for line in out.decode("utf-8", "replace").splitlines())


def corpus_validation(
    repo: Path,
    revision_range: str,
    make_plan,
    *,
    command: str | None = None,
    coverage: bool = False,
    only_python_changes: bool = True,
    max_commits: int | None = None,
    progress=None,
) -> CorpusReport:
    """Plan and validate every ``parent -> commit`` pair in ``revision_range``.

    ``make_plan(base, head)`` builds the plan (the CLI binds discovery,
    manifest and source roots into it). Suites run once per commit thanks to
    the shared outcome cache; coverage runs are per pair.
    """
    report = CorpusReport(str(repo), revision_range, coverage)
    cache: OutcomeCache = {}
    commits = _commits_in_range(repo, revision_range)
    if max_commits is not None:
        commits = commits[-max_commits:]
    for commit, parent, subject in commits:
        entry = CorpusEntry(commit, parent, subject)
        report.entries.append(entry)
        if progress:
            progress(entry)
        if only_python_changes and not _touches_python(repo, parent, commit):
            entry.skipped = "no .py files changed"
            continue
        try:
            plan = make_plan(parent, commit)
            entry.changed_symbols = len(plan.changes)
            entry.targets = sum(1 for d in plan.decisions if d.target.runner == "pytest")
            entry.selected = sum(
                1 for d in plan.decisions if d.selected and d.target.runner == "pytest"
            )
            entry.degraded = plan.degraded
            entry.validation = validate_pytest(
                plan, repo=repo, command=command, coverage=coverage, outcome_cache=cache
            )
        except GitError as exc:
            entry.error = str(exc)
    return report


def corpus_to_dict(report: CorpusReport) -> dict:
    def entry(e: CorpusEntry) -> dict:
        d: dict = {
            "commit": e.commit,
            "parent": e.parent,
            "subject": e.subject,
            "skipped": e.skipped,
            "error": e.error,
            "changed_symbols": e.changed_symbols,
            "targets": e.targets,
            "selected": e.selected,
            "savings": e.savings,
            "degraded": e.degraded,
        }
        if e.validation is not None:
            v = validation_to_dict(e.validation)
            d["ok"] = v["ok"]
            d["outcome"] = v["counts"]
            d["coverage"] = v["coverage"] and {
                k: v["coverage"][k] for k in ("counts", "recall", "precision", "missed")
            }
            d["missed"] = v["missed"]
        return d

    return {
        "repo": report.repo,
        "range": report.revision_range,
        "coverage": report.coverage,
        "ok": report.ok,
        "totals": {
            "commits": len(report.entries),
            "validated": len(report.validated),
            "skipped": sum(1 for e in report.entries if e.skipped),
            "errors": sum(1 for e in report.entries if e.error),
            "outcome_changed": report.outcome_changed,
            "outcome_missed": report.outcome_missed,
            "coverage_affected": report.coverage_affected,
            "coverage_caught": report.coverage_caught,
            "recall": report.recall,
            "precision": report.precision,
            "mean_savings": report.mean_savings,
        },
        "entries": [entry(e) for e in report.entries],
    }


def corpus_to_text(report: CorpusReport) -> str:
    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.0%}"

    lines = [
        f"corpus {report.revision_range}: {len(report.validated)} validated, "
        f"{sum(1 for e in report.entries if e.skipped)} skipped, "
        f"{sum(1 for e in report.entries if e.error)} error(s); "
        f"{'OK' if report.ok else 'MISSES'}",
        f"  outcome changes: {report.outcome_changed} (missed {report.outcome_missed}); "
        f"mean savings {pct(report.mean_savings)}",
    ]
    if report.coverage:
        lines.append(
            f"  coverage: recall {pct(report.recall)} ({report.coverage_caught} of "
            f"{report.coverage_affected}), precision {pct(report.precision)}"
        )
    for e in report.entries:
        short = e.commit[:10]
        if e.skipped:
            lines.append(f"  {short} skipped: {e.skipped}  {e.subject}")
        elif e.error:
            lines.append(f"  {short} ERROR: {e.error.splitlines()[0]}  {e.subject}")
        else:
            v = e.validation
            assert v is not None
            status = "ok" if v.ok else "MISSED"
            cov = ""
            if v.coverage is not None:
                cov = f", recall {pct(v.coverage.recall)}, precision {pct(v.coverage.precision)}"
            lines.append(
                f"  {short} {status}: {e.selected}/{e.targets} selected "
                f"(savings {pct(e.savings)}), outcome misses {len(v.missed)}{cov}"
                f"{' [degraded]' if e.degraded else ''}  {e.subject}"
            )
            for o in v.missed:
                lines.append(f"      MISSED outcome {o.runner_id}: {o.base} -> {o.head}")
            if v.coverage is not None:
                for h in v.coverage.missed:
                    lines.append(
                        f"      MISSED coverage {h.runner_id}: executed "
                        f"{', '.join(h.executed_changed)}"
                    )
    return "\n".join(lines) + "\n"

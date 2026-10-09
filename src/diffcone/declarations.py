"""Dependencies a project declares that the analysis cannot see.

A registry filled at import time, a plugin resolved through entry points, a
handler named in a YAML file: the dependency is real and no static rule can
find it, so the project states it in ``diffcone.toml`` at the repository
root. The file is read from both snapshots being compared, so a declaration
is versioned with the code it describes, and one the change deletes still
counts.

    # diffcone.toml
    [[edges]]
    from = "pkg.registry.dispatch"   # a symbol or a module, in either revision
    to = "pkg.handlers.json"
    why = "handlers register themselves through entry points"

Targets the project runs on every change, whatever it touches (an
end-to-end suite), are named by a pattern on their runner ids:

    [[always_run]]
    targets = "tests/e2e/*"          # fnmatch; * crosses / and ::
    runner = "pytest"                # optional
    why = "end to end, run on every pull request"

Declarations only *add* edges or selected targets, so they can only widen
selection: a wrong one costs a test that runs anyway, and none of them can
make the plan miss. A declaration whose endpoints resolve to nothing, a
malformed entry, or a file that does not parse, is an analysis error -- a
typo that silently declares nothing is the outcome worth failing on; so is
an ``always_run`` ``runner`` that is neither a runner diffcone discovers nor
one of the plan's targets' (the planner checks it, as only it knows the
targets). An ``always_run`` pattern matching no target is only reported
(with its count, flagged in the text report): one file serves jobs that run
different parts of a suite, so a mistyped pattern cannot be told from one a
job skips.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from diffcone.snapshot import INDEX, WORKTREE, GitError, read_files, resolve_commit

FILENAME = "diffcone.toml"


@dataclass(frozen=True, order=True)
class Declaration:
    source: str
    target: str
    why: str = ""

    @property
    def detail(self) -> str:
        return f"declared in {FILENAME}" + (f": {self.why}" if self.why else "")


@dataclass(frozen=True, order=True)
class AlwaysRun:
    targets: str  # an fnmatch pattern on the runner id
    runner: str = ""  # "" for every runner
    why: str = ""

    def matches(self, runner: str, runner_id: str) -> bool:
        return (not self.runner or runner == self.runner) and fnmatchcase(runner_id, self.targets)

    @property
    def label(self) -> str:
        return self.targets + (f" ({self.runner})" if self.runner else "")

    @property
    def detail(self) -> str:
        return f"always_run {self.label} in {FILENAME}" + (f": {self.why}" if self.why else "")


@dataclass
class Declared:
    """What one snapshot's ``diffcone.toml`` declares, and its problems."""

    edges: list[Declaration] = field(default_factory=list)
    always_run: list[AlwaysRun] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


class Unreadable(Exception):
    """The file is there (or the snapshot cannot be read) but we could not
    get at it. Distinct from absent, which is the normal case: silently
    treating a read failure as "no declarations" narrows the plan."""


def read_file(repo: Path, revision: str) -> bytes | None:
    """``diffcone.toml`` as the given snapshot has it, or None when absent."""
    if revision == WORKTREE:
        path = repo / FILENAME
        if not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError as exc:
            raise Unreadable(f"{FILENAME}: {exc}") from exc
    try:
        commit = "" if revision == INDEX else resolve_commit(repo, revision)
    except GitError as exc:  # an unknown revision is the caller's problem
        raise Unreadable(f"{FILENAME}: {exc}") from exc
    try:
        return read_files(repo, commit, [FILENAME], label=revision).get(FILENAME)
    except GitError:
        return None  # not in this snapshot: the ordinary case


def parse(raw: bytes) -> Declared:
    """Declarations and the problems found; a problem is an analysis error."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return Declared(problems=[f"{FILENAME}: {exc}"])
    out = Declared()
    unknown_tables = sorted(set(data) - {"edges", "always_run"})
    if unknown_tables:
        # A singular ``[[edge]]`` or a capitalisation slip would otherwise
        # declare nothing at all, quietly.
        out.problems.append(f"{FILENAME}: unknown top-level key(s) {', '.join(unknown_tables)}")
    for key, read in (("edges", _edge), ("always_run", _always_run)):
        entries = data.get(key, [])
        if not isinstance(entries, list):
            out.problems.append(f"{FILENAME}: '{key}' must be a list of tables")
            continue
        for i, entry in enumerate(entries, start=1):
            read(out, i, entry)
    return out


def _edge(out: Declared, i: int, entry: object) -> None:
    if not isinstance(entry, dict):
        out.problems.append(f"{FILENAME}: edge {i} is not a table")
        return
    unknown = sorted(set(entry) - {"from", "to", "why"})
    if unknown:
        out.problems.append(f"{FILENAME}: edge {i} has unknown key(s) {', '.join(unknown)}")
        return
    source, target, why = entry.get("from"), entry.get("to"), entry.get("why", "")
    if not isinstance(source, str) or not isinstance(target, str) or not (source and target):
        out.problems.append(f"{FILENAME}: edge {i} needs a 'from' and a 'to'")
        return
    if not isinstance(why, str):
        out.problems.append(f"{FILENAME}: edge {i} has a non-string 'why'")
        return
    out.edges.append(Declaration(source, target, why))


def _always_run(out: Declared, i: int, entry: object) -> None:
    where = f"{FILENAME}: always_run {i}"
    if not isinstance(entry, dict):
        out.problems.append(f"{where} is not a table")
        return
    unknown = sorted(set(entry) - {"targets", "runner", "why"})
    if unknown:
        out.problems.append(f"{where} has unknown key(s) {', '.join(unknown)}")
        return
    targets, runner, why = entry.get("targets"), entry.get("runner", ""), entry.get("why", "")
    if not isinstance(targets, str) or not targets:
        out.problems.append(f"{where} needs 'targets', a pattern on runner ids")
        return
    if not isinstance(runner, str) or not isinstance(why, str):
        out.problems.append(f"{where} has a non-string 'runner' or 'why'")
        return
    out.always_run.append(AlwaysRun(targets, runner, why))


def load(repo: Path, revision: str) -> Declared:
    try:
        raw = read_file(repo, revision)
    except Unreadable as exc:
        return Declared(problems=[str(exc)])
    return parse(raw) if raw is not None else Declared()

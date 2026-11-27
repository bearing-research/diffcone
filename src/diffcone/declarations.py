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

The paths outside the source roots that the tests' build and continuous
integration read are named by patterns; a build input or submodule outside
the roots that no pattern names (another component's CI workflow, another
language binding's native code) then no longer selects every target:

    [build]
    reads = ["cpp/**", "testing", ".github/workflows/python.yml"]

A ``reads`` pattern is an fnmatch pattern on repository paths (``*``
crosses ``/``) in which ``**/`` at the start or after a ``/`` also matches
no directory at all, as in gitignore (``cpp/**/*.h`` covers
``cpp/version.h``, ``**/CMakeLists.txt`` the root one), and a pattern
covers everything under a directory it names (``reads_match``). What is
never set aside, whatever the patterns, is the runners' configuration
(``planner.RunnerConfig``).

Edges and ``always_run`` entries only *add* edges or selected targets, so
they can only widen selection: a wrong one costs a test that runs anyway,
and none of them can make the plan miss. ``[build] reads`` is the one
declaration that narrows, and a stale one can hide a miss (a new workflow,
a directory the build starts reading): the planner reports each path it set
aside, an empty list or a pattern matching no path outside the source roots
is an analysis error, and a table that differs between the two snapshots
selects every target.

A declaration whose endpoints resolve to nothing, a malformed entry, or a
file that does not parse, is an analysis error -- a typo that silently
declares nothing is the outcome worth failing on; so is
an ``always_run`` ``runner`` that is neither a runner diffcone discovers nor
one of the plan's targets' (the planner checks it, as only it knows the
targets). An ``always_run`` pattern matching no target is only reported
(with its count, flagged in the text report): one file serves jobs that run
different parts of a suite, so a mistyped pattern cannot be told from one a
job skips.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase, translate
from functools import cache
from pathlib import Path

from diffcone.snapshot import PROJECT_FILE, GitError, read_root_file

FILENAME = PROJECT_FILE


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
    # ``[build] reads``: None when the file has no ``[build]`` table.
    build_reads: tuple[str, ...] | None = None
    problems: list[str] = field(default_factory=list)


class Unreadable(Exception):
    """The file is there (or the snapshot cannot be read) but we could not
    get at it. Distinct from absent, which is the normal case: silently
    treating a read failure as "no declarations" narrows the plan."""


def read_file(repo: Path, revision: str) -> bytes | None:
    """``diffcone.toml`` as the given snapshot has it, or None when absent."""
    try:
        return read_root_file(repo, revision, FILENAME)
    except (OSError, GitError) as exc:
        # There, so a failure to read it (an unmerged entry, a missing
        # object) is not an absence.
        raise Unreadable(f"{FILENAME}: {exc}") from exc


def parse(raw: bytes) -> Declared:
    """Declarations and the problems found; a problem is an analysis error."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return Declared(problems=[f"{FILENAME}: {exc}"])
    out = Declared()
    # ``[index]`` is the snapshot reader's (which files it analyses).
    unknown_tables = sorted(set(data) - {"edges", "always_run", "index", "build"})
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
    if "build" in data:
        _build(out, data["build"])
    return out


def _build(out: Declared, table: object) -> None:
    where = f"{FILENAME}: [build]"
    if not isinstance(table, dict):
        out.problems.append(f"{where} must be a table")
        return
    unknown = sorted(set(table) - {"reads"})
    if unknown:
        out.problems.append(f"{where} has unknown key(s) {', '.join(unknown)}")
        return
    patterns = table.get("reads")
    if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
        out.problems.append(f"{where} reads must be a list of non-empty patterns")
        return
    if not patterns:
        # It would set aside every build input and submodule outside the
        # roots: more likely a list emptied by mistake than a build that
        # reads nothing there (drop the table to set nothing aside).
        out.problems.append(
            f"{where} reads is empty, which would set aside every build file outside the "
            "source roots: name what the tests' build reads, or drop the table"
        )
        return
    out.build_reads = tuple(patterns)


# Stands for ``**/`` while fnmatch translates the rest (no path holds it).
_ANY_DIRS = "\x00"


@cache
def _reads_pattern(pattern: str) -> re.Pattern[str]:
    """``pattern`` as fnmatch translates it (``*`` crosses ``/``), with each
    ``**/`` at the start or after a ``/`` (outside a ``[...]`` set) also
    matching no directory, as in gitignore."""
    if "**/" not in pattern:
        return re.compile(translate(pattern))
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i) and (i == 0 or pattern[i - 1] == "/"):
            out.append(_ANY_DIRS)
            i += 3
            continue
        if pattern[i] == "[":
            # fnmatch's own scan for the set's end; an unclosed ``[`` is literal.
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j < n:
                out.append(pattern[i : j + 1])
                i = j + 1
                continue
        out.append(pattern[i])
        i += 1
    return re.compile(translate("".join(out)).replace(_ANY_DIRS, "(?:.*/)?"))


def reads_pattern_matches(name: str, pattern: str) -> bool:
    """Whether the ``[build] reads`` pattern matches the path ``name``
    itself (``reads_match`` also takes the directories above it)."""
    if "**/" not in pattern:
        return fnmatchcase(name, pattern)
    return _reads_pattern(pattern).match(name) is not None


def reads_match(path: str, patterns: tuple[str, ...]) -> bool:
    """Whether a ``[build] reads`` pattern names ``path`` or a directory
    above it (``testing`` names the submodule and everything under it).
    Patterns are fnmatch patterns on repository paths (``*`` crosses
    ``/``) in which ``**/`` may also match no directory (_reads_pattern)."""
    parts = path.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return any(reads_pattern_matches(p, pattern) for pattern in patterns for p in prefixes)


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

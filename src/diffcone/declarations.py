"""Dependencies a project declares that the analysis cannot see.

A registry filled at import time, a plugin resolved through entry points, a
handler named in a YAML file: the dependency is real and no static rule can
find it, so the project states it in ``diffcone.toml`` at the repository
root. The file is read from the snapshot being analysed (the head one), so a
declaration is versioned with the code it describes.

    # diffcone.toml
    [[edges]]
    from = "pkg.registry.dispatch"   # a symbol or a module, in either revision
    to = "pkg.handlers.json"
    why = "handlers register themselves through entry points"

Declarations only *add* edges, so they can only widen selection: a wrong one
costs a test that runs anyway, and none of them can make the plan miss. A
declaration whose endpoints resolve to nothing, or a file that does not
parse, is an analysis error -- a typo that silently declares nothing is the
outcome worth failing on.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
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


def read_file(repo: Path, revision: str) -> bytes | None:
    """``diffcone.toml`` as the given snapshot has it, or None when absent."""
    try:
        if revision == WORKTREE:
            path = repo / FILENAME
            return path.read_bytes() if path.is_file() else None
        commit = "" if revision == INDEX else resolve_commit(repo, revision)
        return read_files(repo, commit, [FILENAME], label=revision).get(FILENAME)
    except (GitError, OSError):
        return None


def parse(raw: bytes) -> tuple[list[Declaration], list[str]]:
    """Declarations and the problems found; a problem is an analysis error."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [], [f"{FILENAME}: {exc}"]
    problems: list[str] = []
    edges: list[Declaration] = []
    entries = data.get("edges", [])
    if not isinstance(entries, list):
        return [], [f"{FILENAME}: 'edges' must be a list of tables"]
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"{FILENAME}: edge {i} is not a table")
            continue
        unknown = sorted(set(entry) - {"from", "to", "why"})
        if unknown:
            problems.append(f"{FILENAME}: edge {i} has unknown key(s) {', '.join(unknown)}")
            continue
        source, target, why = entry.get("from"), entry.get("to"), entry.get("why", "")
        if not isinstance(source, str) or not isinstance(target, str) or not (source and target):
            problems.append(f"{FILENAME}: edge {i} needs a 'from' and a 'to'")
            continue
        if not isinstance(why, str):
            problems.append(f"{FILENAME}: edge {i} has a non-string 'why'")
            continue
        edges.append(Declaration(source, target, why))
    return edges, problems


def load(repo: Path, revision: str) -> tuple[list[Declaration], list[str]]:
    raw = read_file(repo, revision)
    return parse(raw) if raw is not None else ([], [])

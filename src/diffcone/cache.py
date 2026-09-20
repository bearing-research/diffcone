"""Per-commit index cache.

A committed snapshot's :class:`SourceIndex` is a pure function of the commit,
the source roots and the indexer version, so it can be stored and reused.
The working tree and the git index are never cached whole (their content is
not identified by a commit). A cache hit must produce a byte-identical plan
to a cache miss; the cache is an optimisation only.

Layout: ``<cache_dir>/index/<key>.json`` where ``cache_dir`` defaults to
``<repo>/.diffcone/cache`` (add ``.diffcone/`` to ``.gitignore``).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from diffcone.model import (
    AnalysisError,
    Edge,
    ExternalReference,
    SnapshotInfo,
    SourceIndex,
    Symbol,
    UnresolvedReference,
)

# Bump whenever the indexer's output for the same input can change.
INDEX_FORMAT = 2  # 2: docstring_hash on symbols


def default_cache_dir(repo: Path) -> Path:
    return repo / ".diffcone" / "cache"


def index_key(commit: str, source_roots: list[str]) -> str:
    material = json.dumps([INDEX_FORMAT, commit, sorted(source_roots)])
    return hashlib.sha256(material.encode()).hexdigest()


def index_to_dict(index: SourceIndex) -> dict:
    return {
        "format": INDEX_FORMAT,
        "snapshot": asdict(index.snapshot),
        "modules": sorted(index.modules),
        "failed_modules": sorted(index.failed_modules),
        "symbols": [asdict(s) for _, s in sorted(index.symbols.items())],
        "edges": [asdict(e) for e in sorted(index.edges)],
        "unresolved": [asdict(u) for u in sorted(index.unresolved)],
        "external": [asdict(x) for x in sorted(index.external)],
        "errors": [asdict(e) for e in sorted(index.errors)],
    }


def index_from_dict(data: dict) -> SourceIndex:
    if data.get("format") != INDEX_FORMAT:
        raise ValueError("unsupported index format")
    symbols = {}
    for s in data["symbols"]:
        s = dict(s)
        s["line_ranges"] = tuple(tuple(r) for r in s["line_ranges"])
        s["imports"] = tuple(s["imports"])
        symbol = Symbol(**s)
        symbols[symbol.id] = symbol
    return SourceIndex(
        snapshot=SnapshotInfo(**data["snapshot"]),
        modules=set(data["modules"]),
        symbols=symbols,
        edges={Edge(**e) for e in data["edges"]},
        unresolved={UnresolvedReference(**u) for u in data["unresolved"]},
        external={ExternalReference(**x) for x in data["external"]},
        errors=[AnalysisError(**e) for e in data["errors"]],
        failed_modules=set(data["failed_modules"]),
    )


class IndexCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0

    def _path(self, commit: str, source_roots: list[str]) -> Path:
        return self.directory / "index" / f"{index_key(commit, source_roots)}.json"

    def load(self, commit: str, source_roots: list[str]) -> SourceIndex | None:
        path = self._path(commit, source_roots)
        try:
            data = json.loads(path.read_text("utf-8"))
            index = index_from_dict(data)
        except (OSError, ValueError, KeyError, TypeError):
            self.misses += 1
            return None
        if index.snapshot.commit != commit:
            self.misses += 1
            return None
        self.hits += 1
        return index

    def store(self, index: SourceIndex, source_roots: list[str]) -> None:
        path = self._path(index.snapshot.commit, source_roots)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write atomically so a concurrent reader never sees a partial file.
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(index_to_dict(index), f)
            os.replace(tmp, path)
        except OSError:
            pass  # a cache write failure is never an error

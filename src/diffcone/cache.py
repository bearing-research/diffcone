"""Per-commit index cache.

A committed snapshot's :class:`SourceIndex` is a pure function of the commit,
the source roots and the indexer version, so it can be stored and reused.
The working tree and the git index are never cached whole (their content is
not identified by a commit). A cache hit must produce a byte-identical plan
to a cache miss; the cache is an optimisation only.

Layout: ``<cache_dir>/index/<key>.json`` where ``cache_dir`` defaults to
``<repo>/.diffcone/cache`` (add ``.diffcone/`` to ``.gitignore``).

Beside it, the :class:`ModuleCache` keeps per-module results for every
snapshot kind, including the working tree: a module's first-pass facts are a
pure function of its file, and its second-pass resolution a pure function of
the file plus a fingerprint of what other modules expose (see
``Indexer.build``). A warm working-tree plan after a one-line edit then
re-parses and re-resolves only the edited module.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
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
INDEX_FORMAT = 3  # 3: module-level variables are symbols


def _indexer_fingerprint() -> str:
    """Hash of the modules that determine an index's content, so any change
    to them invalidates cached indexes without anyone remembering to bump
    INDEX_FORMAT (a stale base index once produced phantom changed symbols)."""
    here = Path(__file__).parent
    h = hashlib.sha256()
    # ``ast.dump`` output (hence every hash) may differ between Python versions.
    h.update(f"python{sys.version_info[0]}.{sys.version_info[1]}:".encode())
    for name in ("model.py", "snapshot.py", "indexer.py"):
        h.update((here / name).read_bytes())
    return h.hexdigest()[:16]


INDEXER_FINGERPRINT = _indexer_fingerprint()


def default_cache_dir(repo: Path) -> Path:
    return repo / ".diffcone" / "cache"


def index_key(commit: str, source_roots: list[str]) -> str:
    material = json.dumps([INDEX_FORMAT, INDEXER_FINGERPRINT, commit, sorted(source_roots)])
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


class ModuleCache:
    """Per-module records in one SQLite file (``<cache_dir>/modules.sqlite``):
    first-pass facts under ``(key, "")`` and second-pass outputs under
    ``(key, fingerprint)``, where ``key`` identifies the module name, path,
    file content and indexer version, and ``fingerprint`` the environment the
    module was resolved against. Records are plain JSON produced by the
    indexer, which treats a malformed one as a miss; a hit must be
    indistinguishable from a miss. One file rather than one per record
    because a warm plan on a large tree loads thousands of records, and
    opening that many files costs more than resolving.

    Facts rows are kept for every distinct file content seen. Resolution
    rows are kept only for the latest fingerprint each file was resolved
    against: a store drops the other fingerprints' rows for the files of
    the snapshot being stored, so the table is bounded by the facts rows.

    Every operation opens its own connection, so one instance may be shared
    by threads (``corpus --jobs``) and by concurrent processes (SQLite locks;
    a writer that cannot get the lock in time gives up silently). Reads open
    the file read-only and work on a directory that cannot be written."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "modules.sqlite"
        self.facts_hits = 0
        self.facts_misses = 0
        self.resolved_hits = 0
        self.resolved_misses = 0

    @staticmethod
    def key(module: str, path: str, content: bytes) -> str:
        h = hashlib.sha256()
        h.update(f"{INDEX_FORMAT}:{INDEXER_FINGERPRINT}:{module}:{path}:".encode())
        h.update(content)
        return h.hexdigest()

    def _connect(self, write: bool) -> sqlite3.Connection | None:
        try:
            if not write:
                if not self.path.exists():
                    return None
                return sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=10)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "key TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY (key, fingerprint))"
            )
            return conn
        except (sqlite3.Error, OSError, ValueError):
            return None

    def _load(self, keys: list[str], fingerprint: str) -> dict[str, dict]:
        found: dict[str, dict] = {}
        if not keys:
            return found
        conn = self._connect(write=False)
        if conn is None:
            return found
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT key, data FROM records WHERE fingerprint = ? AND key IN ({marks})",
                    [fingerprint, *chunk],
                ).fetchall()
                for key, data in rows:
                    try:
                        record = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        found[key] = record
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return found

    def load_facts(self, keys: list[str]) -> dict[str, dict]:
        found = self._load(keys, "")
        self.facts_hits += len(found)
        self.facts_misses += len(keys) - len(found)
        return found

    def load_resolved(self, keys: list[str], fingerprint: str) -> dict[str, dict]:
        found = self._load(keys, fingerprint)
        self.resolved_hits += len(found)
        self.resolved_misses += len(keys) - len(found)
        return found

    def store(
        self,
        facts: dict[str, dict],
        resolved: dict[str, dict],
        fingerprint: str,
        snapshot_keys: list[str],
    ) -> None:
        """Store new records in one transaction and evict the resolution rows
        of ``snapshot_keys`` (every module of the snapshot) that belong to
        another fingerprint."""
        conn = self._connect(write=True)
        if conn is None:
            return
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO records (key, fingerprint, data) VALUES (?, ?, ?)",
                    [(key, "", json.dumps(record)) for key, record in facts.items()]
                    + [(key, fingerprint, json.dumps(record)) for key, record in resolved.items()],
                )
                for i in range(0, len(snapshot_keys), 500):
                    chunk = snapshot_keys[i : i + 500]
                    marks = ",".join("?" * len(chunk))
                    conn.execute(
                        "DELETE FROM records WHERE fingerprint NOT IN ('', ?) "
                        f"AND key IN ({marks})",
                        [fingerprint, *chunk],
                    )
        except sqlite3.Error:
            pass  # a cache write failure is never an error
        finally:
            conn.close()


class IndexCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.modules = ModuleCache(directory)
        # The per-file hash cache this replaced left ``hashes/`` behind.
        shutil.rmtree(directory / "hashes", ignore_errors=True)

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

"""The cache under ``<repo>/.diffcone/cache`` (``cache_dir``; diffcone
gives ``.diffcone/`` a ``.gitignore`` of its own). It is an optimisation
only: a hit must produce a byte-identical plan to a miss.

- ``index/<key>.json`` (:class:`IndexCache`): a committed snapshot's whole
  :class:`SourceIndex`, a pure function of the commit, the source roots and
  the indexer version. The working tree and the git index are never cached
  whole (their content is not identified by a commit), and neither is an
  index whose analysis ran out of stack (``TOO_DEEP``).
- ``discovery/<key>.json`` (:class:`DiscoveryCache`): static discovery
  results per committed snapshot, runner and options; not one that left a
  module out as too deeply nested to walk (``TOO_DEEP``), for the same
  reason.
- ``modules.sqlite`` (:class:`ModuleCache`): per-module results for every
  snapshot kind, the working tree included: a module's first-pass facts are
  a pure function of its file, and its second-pass resolution a pure
  function of the file plus a fingerprint of what other modules expose (see
  ``Indexer.build``). A warm working-tree plan after a one-line edit then
  re-parses and re-resolves only the edited module.

:func:`prune` (``diffcone prune``) keeps only what planning at given commits
reads.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from diffcone.cython import CythonFunction, CythonModule, CythonStatement
from diffcone.discovery import DiscoveryNote, DiscoveryOptions, DiscoveryResult
from diffcone.indexer.facts import symbol_from_dict
from diffcone.indexer.syntax import TOO_DEEP
from diffcone.manifest import Target
from diffcone.model import (
    AnalysisError,
    Edge,
    EdgeKind,
    ExternalReference,
    SnapshotInfo,
    SnapshotKind,
    SourceIndex,
    UnresolvedKind,
    UnresolvedReference,
)

# Bump whenever the indexer's output for the same input can change.
# 62: a class reached by another name is written through it, one record of
#     what each class is given (writes.ClassRecord), shared objects bound in
#     methods, nested classes' state, library methods on a class and
#     ``type()`` of three arguments (audit round 7, U2, U3, U11, U12);
# 63: SourceIndex.decorator_symbols goes through the whole-index cache
#     (audit round 7, U27); the state rules of round 7's state batch;
# 64: typed_calls and shared_instances; receiver attribute stores keep
#     their name (``@self.x``), re-initialisation is ``@init``; memoisers
#     applied by a call (audit round 7, U4, U5, U14, U15, U16, U17, U18);
# 59: a class attribute bound once to a value that cannot change in place
#     is no root of its class (audit round 6, T24); an instance holds the
#     arguments its constructor was handed, and only a write that may reach
#     them writes them (T23);
# 58: an instance an in-scope class makes (``Store()``, ``cls.__new__(cls)``)
#     is no root of the class; a write in place into it writes the class
#     when the class holds state, and ``__new__``/``__init_subclass__`` take
#     the class (audit round 6, T22);
# 57: a write in place into a module variable holding an instance of an
#     in-scope class with class-level state writes the class and its
#     in-scope bases too (audit round 6, T21);
# 56: SourceIndex.stores without what the value derives from, through
#     ``__dict__``/``vars()`` and explicit re-initialisation (audit T19);
# 55: no mutated_by edge from a module variable whose one binding is an
#     immutable value nothing binds anew (audit round 6, T20);
# 54: writes to state held on a class, and through another name for a module
#     variable (a local alias, a getter's result, globals()), give mutated_by
#     edges (audit round 6, T6 and T7);
# 53: imports and runs of files [index] exclude keeps out are unknown
#     dependencies; pytest.importorskip, find_spec, pkgutil imports (T2, T9);
# 52: SourceIndex.write_values, call_values removed again (audit T1);
# 51: SourceIndex.write_values, call_values (what in-place writes store and calls
#     hand on; E32, E35, E36);
# 50: SnapshotInfo.excluded (diffcone.toml [index] exclude; E29);
# 49: SourceIndex.lazy_constants (indexer/lazy.py; roadmap item 19, rule 2);
# 48: Symbol.annotation_names (what a function's annotations can read; E7);
# 47: a name bound to modules reads off them (indexer/aliases.py), and a
# module followed through a name is one a value of unknown type may be;
# 46: batch B of round 5 (text run as code, test runs, handed instances,
# match_args); 45: batches A, C and D of round 5 merged (each had numbered its index 44):
# SourceIndex.file_modes (git modes of the entries under the roots), the
# evidence stores/memoized/decorations facts;
# 44: Symbol.statement_layout (the order of a module's or class's statements,
# audit round 5 R6), Symbol.reads_any_docstring (R7), classes with Enum-family
# bases or in-scope metaclasses depend on their ``__init__``/``__new__`` (R20),
# stores through a wider-scoped fixture's ``monkeypatch`` are not undone (R27);
# 43: Symbol.fixture_scope (the isolation assumption: planner.isolated_test_code);
# 42: writes only (not handing on) give use-scan ``mutated_by`` edges, modules
# handed to written parameters, stores put back before the test ends, lookups
# on the whole chain; 44: stores, memoized, decorations (audit round 5, R22);
# 41: ``mutated_by`` edges from the use scan (Indexer._mutations), star-imported
# names in the use scan, UseRecord.direct;
# 40: no escaped_values; decorated symbols (``decorated`` edges, passive decorators), the known
# callers of a parameter, deeply nested modules as analysis errors;
# 39: stores undone after the test (indexer/patches.py: SCOPED, SWAP,
# external writes, restored process state); 38: attribute_modules,
# any_module_readers, module_writers, module-attribute references,
# unpicklers and file loaders, installs under unbounded names;
# 37: module_reach, sys.modules installs, scoped names in the use scan;
# 36: process writes, `writes`/`rebinds` edges, builtin containers,
# main-guarded code; 35: third-party values of project factories (external_returns); 34:
# writes through parameters and receivers, scripts named in strings;
# 33: bounded run-time module names, table_imports, graph handles, escaped
# modules; 32: namespace reads off any object; 31: class-object reads,
# literal tables of shadowing scopes; 30: quiet_header; 29: uses of tables
# and modules (indexer.uses); 28: __subclasses__ reads; 27: type
# parameters, unnameable build scripts as other files; 26: escaped values;
# 25: external sites; 24: docstring decorators; 23: open classes
INDEX_FORMAT = 64


def _indexer_fingerprint() -> str:
    """Hash of the modules that determine an index's content, so any change
    to them invalidates cached indexes without anyone remembering to bump
    INDEX_FORMAT (a stale base index once produced phantom changed symbols)."""
    here = Path(__file__).parent
    h = hashlib.sha256()
    # ``ast.dump`` output (hence every hash) may differ between Python versions.
    h.update(f"python{sys.version_info[0]}.{sys.version_info[1]}:".encode())
    sources = [here / name for name in ("model.py", "snapshot.py", "cython.py")]
    sources += sorted((here / "indexer").glob("*.py"))
    for path in sources:
        h.update(path.relative_to(here).as_posix().encode() + b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


INDEXER_FINGERPRINT = _indexer_fingerprint()


def make_own_dir(directory: Path) -> None:
    """Create a directory of diffcone's, and give the ``.diffcone`` it sits in
    a ``.gitignore`` of its own (as pytest does for ``.pytest_cache``), so the
    cache and recordings never show up as untracked files of the project."""
    directory.mkdir(parents=True, exist_ok=True)
    for parent in (directory, *directory.parents):
        if parent.name == ".diffcone":
            ignore = parent / ".gitignore"
            if not ignore.exists():
                try:
                    ignore.write_text("# created by diffcone\n*\n", "utf-8")
                except OSError:
                    pass
            break


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` through a temporary file in the
    same directory, so a concurrent reader never sees a partial file. A
    cache write that fails is never an error: the temporary file goes."""
    tmp: str | None = None
    try:
        make_own_dir(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
        "escaped_classes": sorted(index.escaped_classes),
        "escaped_modules": sorted(index.escaped_modules),
        "module_reach": {m: list(r) for m, r in sorted(index.module_reach.items())},
        "attribute_modules": sorted(index.attribute_modules),
        "any_module_readers": sorted(index.any_module_readers),
        "module_writers": {m: list(w) for m, w in sorted(index.module_writers.items())},
        "other_files": dict(sorted(index.other_files.items())),
        "file_modes": dict(sorted(index.file_modes.items())),
        "symbols": [asdict(s) for _, s in sorted(index.symbols.items())],
        "edges": [asdict(e) for e in sorted(index.edges)],
        "unresolved": [asdict(u) for u in sorted(index.unresolved)],
        "external": [asdict(x) for x in sorted(index.external)],
        "errors": [asdict(e) for e in sorted(index.errors)],
        "reflection": sorted(list(r) for r in index.reflection),
        "external_sites": [[s, d, list(w)] for (s, d), w in sorted(index.external_sites.items())],
        "table_imports": [
            [s, d, list(c), list(w)] for (s, d), (c, w) in sorted(index.table_imports.items())
        ],
        "class_attributes": {
            c: dict(sorted(a.items())) for c, a in sorted(index.class_attributes.items())
        },
        "class_bases": {c: list(b) for c, b in sorted(index.class_bases.items())},
        "open_classes": sorted(index.open_classes),
        "doc_decorated": sorted(index.doc_decorated),
        "decorator_symbols": sorted(index.decorator_symbols),
        "scripts": dict(sorted(index.scripts.items())),
        "script_refs": sorted(list(r) for r in index.script_refs),
        "process_writes": sorted(list(w) for w in index.process_writes),
        "main_guarded": sorted(list(m) for m in index.main_guarded),
        "stores": sorted(list(s) for s in index.stores),
        "handed": sorted(list(h) for h in index.handed),
        "memoized": sorted(index.memoized),
        "decorations": sorted(list(d) for d in index.decorations),
        "typed_calls": sorted(list(t) for t in index.typed_calls),
        "shared_instances": sorted(index.shared_instances),
        "lazy_constants": dict(sorted(index.lazy_constants.items())),
        "cython": {
            path: {
                "functions": [{**asdict(f), "names": sorted(f.names)} for f in module.functions],
                "outside_hash": module.outside_hash,
                "statements": None
                if module.statements is None
                else [asdict(st) for st in module.statements],
            }
            for path, module in sorted(index.cython.items())
        },
    }


def index_from_dict(data: dict) -> SourceIndex:
    if data.get("format") != INDEX_FORMAT:
        raise ValueError("unsupported index format")
    symbols = {s.id: s for s in map(symbol_from_dict, data["symbols"])}
    return SourceIndex(
        snapshot=SnapshotInfo(
            **data["snapshot"]
            | {
                "kind": SnapshotKind(data["snapshot"]["kind"]),
                "excluded": tuple(data["snapshot"]["excluded"]),
            }
        ),
        modules=set(data["modules"]),
        symbols=symbols,
        edges={
            Edge(e["source"], e["target"], EdgeKind(e["kind"]), e["detail"]) for e in data["edges"]
        },
        unresolved={
            UnresolvedReference(u["symbol"], UnresolvedKind(u["kind"]), u["name"], u["detail"])
            for u in data["unresolved"]
        },
        external={ExternalReference(**x) for x in data["external"]},
        errors=[AnalysisError(**e) for e in data["errors"]],
        failed_modules=set(data["failed_modules"]),
        escaped_classes=set(data["escaped_classes"]),
        escaped_modules=set(data["escaped_modules"]),
        module_reach={m: tuple(r) for m, r in data["module_reach"].items()},
        attribute_modules=set(data["attribute_modules"]),
        any_module_readers=set(data["any_module_readers"]),
        module_writers={m: tuple(w) for m, w in data["module_writers"].items()},
        other_files=dict(data["other_files"]),
        file_modes=dict(data["file_modes"]),
        reflection={(s, d) for s, d in data["reflection"]},
        external_sites={(s, d): tuple(w) for s, d, w in data["external_sites"]},
        table_imports={(s, d): (tuple(c), tuple(w)) for s, d, c, w in data["table_imports"]},
        class_attributes={c: dict(a) for c, a in data["class_attributes"].items()},
        class_bases={c: tuple(b) for c, b in data["class_bases"].items()},
        open_classes=set(data["open_classes"]),
        doc_decorated=set(data["doc_decorated"]),
        decorator_symbols=set(data["decorator_symbols"]),
        scripts=dict(data["scripts"]),
        script_refs={(s, p) for s, p in data["script_refs"]},
        process_writes={(s, w) for s, w in data["process_writes"]},
        main_guarded={(a, b, c) for a, b, c in data["main_guarded"]},
        stores={(a, b) for a, b in data["stores"]},
        handed={(a, b, c) for a, b, c in data["handed"]},
        memoized=set(data["memoized"]),
        decorations={(a, b) for a, b in data["decorations"]},
        typed_calls={(a, b) for a, b in data["typed_calls"]},
        shared_instances=set(data["shared_instances"]),
        lazy_constants=dict(data["lazy_constants"]),
        cython={
            path: CythonModule(
                path,
                tuple(_cython_function(f) for f in module["functions"]),
                module["outside_hash"],
                None
                if module["statements"] is None
                else tuple(_cython_statement(st) for st in module["statements"]),
            )
            for path, module in data["cython"].items()
        },
    )


def _cython_function(data: dict[str, Any]) -> CythonFunction:
    fields = dict(data)
    fields["names"] = frozenset(data["names"])
    return CythonFunction(**fields)


def _cython_statement(data: dict[str, Any]) -> CythonStatement:
    fields = dict(data)
    fields["names"] = tuple(data["names"])
    fields["bases"] = tuple(data["bases"])
    return CythonStatement(**fields)


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
        # Absolute, so that the read-only URI below can be formed: a relative
        # directory (the default ``--repo .``) once made every read a miss
        # (audit round 7, U24).
        self.path = (directory / "modules.sqlite").absolute()
        # Hit and miss counters: test instrumentation, nothing reads them else.
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
            make_own_dir(self.path.parent)
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


# 2: lifecycle dependencies every target lists are written once (audit E37)
DISCOVERY_FORMAT = 2


def _discovery_fingerprint() -> str:
    """The indexer's fingerprint (discovery reads the index) and the
    discovery package's source: any change invalidates cached results."""
    here = Path(__file__).parent
    h = hashlib.sha256(INDEXER_FINGERPRINT.encode())
    for path in sorted((here / "discovery").glob("*.py")) + [here / "manifest.py"]:
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


DISCOVERY_FINGERPRINT = _discovery_fingerprint()


def _sorted_unique(deps: tuple[str, ...]) -> bool:
    return all(a < b for a, b in zip(deps, deps[1:], strict=False))


def _shared_lifecycle(targets: list[Target]) -> frozenset[str]:
    """The lifecycle dependencies every target lists, where each lists them
    sorted and once (as discovery does), so a target's own rest rebuilds it."""
    sets = [frozenset(t.lifecycle_dependencies) for t in targets]
    if not sets or not all(_sorted_unique(t.lifecycle_dependencies) for t in targets):
        return frozenset()
    return frozenset.intersection(*sets)


def _target_entry(target: Target, shared: frozenset[str]) -> dict[str, Any]:
    entry = asdict(target)
    if shared:
        del entry["lifecycle_dependencies"]
        entry["own"] = [d for d in target.lifecycle_dependencies if d not in shared]
    return entry


class DiscoveryCache:
    """Static discovery results per committed snapshot. Discovery reads only
    the snapshot and its index, so a commit, source roots, runner and
    options determine the result; ``WORKTREE`` and ``INDEX`` are never
    cached. On pandas discovering both sides was the largest part of a warm
    plan, and a cached head also lets the head index come from the cache."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory / "discovery"

    def _path(self, commit: str, roots: list[str], runner: str, options: DiscoveryOptions) -> Path:
        settings = {
            k: sorted(v) if isinstance(v, (set, frozenset)) else v
            for k, v in sorted(asdict(options).items())
        }
        material = json.dumps(
            [DISCOVERY_FORMAT, DISCOVERY_FINGERPRINT, commit, sorted(roots), runner, settings]
        )
        return self.directory / f"{hashlib.sha256(material.encode()).hexdigest()}.json"

    def load(
        self, commit: str, roots: list[str], runner: str, options: DiscoveryOptions
    ) -> DiscoveryResult | None:
        try:
            data = json.loads(self._path(commit, roots, runner, options).read_text("utf-8"))
            if data.get("format") != DISCOVERY_FORMAT or data.get("commit") != commit:
                return None
            shared = data["shared"]
            pool: dict[str, str] = {d: d for d in shared}
            return DiscoveryResult(
                runner=data["runner"],
                targets=[
                    Target(
                        t["runner"],
                        t["runner_id"],
                        t["entry_symbol"],
                        tuple(sorted([*shared, *(pool.setdefault(d, d) for d in t["own"])]))
                        if "own" in t
                        else tuple(t["lifecycle_dependencies"]),
                    )
                    for t in data["targets"]
                ],
                notes=[DiscoveryNote(**n) for n in data["notes"]],
                config=data["config"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def store(
        self,
        result: DiscoveryResult,
        commit: str,
        roots: list[str],
        options: DiscoveryOptions,
    ) -> None:
        if any(TOO_DEEP in n.detail for n in result.notes):
            return  # depends on the caller's stack depth, not only on the commit
        path = self._path(commit, roots, result.runner, options)
        shared = _shared_lifecycle(result.targets)
        data = {
            "format": DISCOVERY_FORMAT,
            "commit": commit,
            "fingerprint": DISCOVERY_FINGERPRINT,
            "runner": result.runner,
            # What every target depends on, written once (audit E37: pandas'
            # 25 620 targets each carry the same 654, a 680 MB file that took
            # 1.4 GB to load); a target lists the rest (``own``).
            "shared": sorted(shared),
            "targets": [_target_entry(t, shared) for t in result.targets],
            "notes": [asdict(n) for n in result.notes],
            "config": result.config,
        }
        _write_json_atomic(path, data)


class IndexCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0  # hit and miss counters: test instrumentation
        self.misses = 0
        self.modules = ModuleCache(directory)
        self.discovery = DiscoveryCache(directory)

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
        if any(e.message.startswith(TOO_DEEP) for e in index.errors):
            return  # depends on the caller's stack depth, not only on the commit
        _write_json_atomic(self._path(index.snapshot.commit, source_roots), index_to_dict(index))


# --------------------------------------------------------------------------- pruning

_COMMIT = re.compile(rb'"commit": "([0-9a-f]{40})"')
_FINGERPRINT = re.compile(rb'"fingerprint": "([0-9a-f]+)"')


def _head(path: Path) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(4096)
    except OSError:
        return b""


def _recorded_commit(path: Path) -> str | None:
    """The commit an index or discovery file was stored for: the first
    ``"commit"`` key, which both write near the start."""
    match = _COMMIT.search(_head(path))
    return match.group(1).decode() if match else None


@dataclass
class PruneResult:
    files_removed: int = 0
    files_kept: int = 0
    rows_removed: int = 0
    rows_kept: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


def _size(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def prune(
    directory: Path, commits: set[str], source_roots: list[str], module_keys: set[str]
) -> PruneResult:
    """Keep only what planning at ``commits`` with ``source_roots`` reads:
    their whole indexes and discovery results as this version of diffcone
    names them, and the per-module rows for their files' ``module_keys``
    (``ModuleCache.key``), which also serve a later commit or a working tree
    sharing those files. Everything else goes: other commits, entries an
    older diffcone wrote, partial writes. The cache stays an optimisation: a
    pruned entry is a miss, never a different plan."""
    result = PruneResult(bytes_before=_size(directory) if directory.exists() else 0)
    indexes = {f"{index_key(c, source_roots)}.json" for c in commits}

    def wanted(sub: str, path: Path) -> bool:
        if sub == "index":
            return path.name in indexes
        found = _FINGERPRINT.search(_head(path))
        return (
            path.suffix == ".json"
            and _recorded_commit(path) in commits
            and found is not None
            and found.group(1).decode() == DISCOVERY_FINGERPRINT
        )

    for sub in ("index", "discovery"):
        for path in sorted((directory / sub).glob("*")):
            if wanted(sub, path):
                result.files_kept += 1
                continue
            try:
                path.unlink()
                result.files_removed += 1
            except OSError:
                pass
    modules = directory / "modules.sqlite"
    if modules.exists():
        try:
            conn = sqlite3.connect(modules, timeout=30)
            try:
                with conn:
                    conn.execute("CREATE TEMP TABLE keep (key TEXT PRIMARY KEY)")
                    conn.executemany(
                        "INSERT OR IGNORE INTO keep VALUES (?)", [(k,) for k in module_keys]
                    )
                    result.rows_removed = conn.execute(
                        "DELETE FROM records WHERE key NOT IN (SELECT key FROM keep)"
                    ).rowcount
                result.rows_kept = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            finally:
                conn.close()
        except sqlite3.Error:
            pass  # a cache that cannot be pruned is still a valid cache
    result.bytes_after = _size(directory) if directory.exists() else 0
    return result

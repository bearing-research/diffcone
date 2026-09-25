"""Execution evidence: what each test executed at one commit, as planning data.

The recorder (``diffcone/collect.py``, a pytest plugin) writes raw per-process
records of code objects and repository paths. :func:`fold` maps those code
objects to symbols of diffcone's own index of the same commit and checks the
collection is usable; :func:`write_store` and :func:`load_store` keep the
result in ``.diffcone/evidence/<commit>-<environment>.sqlite``.

Nothing here runs project code, and the planner treats an :class:`Evidence`
the way it treats a manifest: data a runner integration produced. See
docs/evidence_design.md.
"""

from __future__ import annotations

import array
import glob
import json
import os
import sqlite3
import struct
import tempfile
import time
import zlib
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from diffcone.model import MODULE, SourceIndex

STORE_FORMAT = 1
FLAG_SUBPROCESS = 1  # the test started a subprocess, whose execution is not seen
FLAG_UNSTABLE = 2  # the test's record differed between two collections
EVIDENCE_DIR = Path(".diffcone") / "evidence"
# An import run by a module outside the source roots: nothing static can say
# who observes what it built.
UNINDEXED_MODULE = "path:"


class EvidenceError(Exception):
    """A collection that cannot be trusted, or a store that cannot be read."""


@dataclass(frozen=True)
class TestRecord:
    __test__ = False  # not a pytest class

    symbols: frozenset[int]
    paths: frozenset[int]
    flags: int = 0


@dataclass
class Evidence:
    commit: str
    source_roots: list[str]
    environment: dict
    environment_hash: str
    command: str
    created: float
    symbols: list[str]
    paths: list[str]
    tests: dict[str, TestRecord]
    # Symbols executed outside every test window (imports, collection, hooks).
    import_phase: frozenset[str] = frozenset()
    # Symbol -> the modules whose import ran it (``path:<file>`` for a module
    # outside the index).
    import_by: dict[str, frozenset[str]] = field(default_factory=dict)
    # Paths opened, stat'ed or listed outside every test window.
    import_paths: frozenset[str] = frozenset()
    # Something outside every test window started a subprocess.
    import_subprocess: bool = False
    reverse_checked: bool = False
    location: Path | None = None

    def __post_init__(self) -> None:
        self._symbol_ids = {s: i for i, s in enumerate(self.symbols)}
        self._path_ids = {p: i for i, p in enumerate(self.paths)}

    def symbol_ids(self, symbols) -> set[int]:
        return {self._symbol_ids[s] for s in symbols if s in self._symbol_ids}

    def path_ids(self, paths) -> set[int]:
        return {self._path_ids[p] for p in paths if p in self._path_ids}

    def executed(self, record: TestRecord) -> set[str]:
        return {self.symbols[i] for i in record.symbols}

    def touched(self, record: TestRecord) -> set[str]:
        return {self.paths[i] for i in record.paths}


# --------------------------------------------------------------------------- folding


class _Owners:
    """Code object (file, first line, qualname) -> the innermost symbol whose
    definition contains that line; a module's own code maps to the module.
    Nested functions, lambdas and comprehensions land in their enclosing
    symbol, as coverage validation maps lines."""

    def __init__(self, index: SourceIndex) -> None:
        self.module_of_path: dict[str, str] = {}
        spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        for symbol in index.symbols.values():
            if symbol.kind == MODULE:
                self.module_of_path[symbol.path] = symbol.id
            for start, end in symbol.line_ranges:
                spans[symbol.path].append((start, end, symbol.id))
        self.spans = {path: sorted(s) for path, s in spans.items()}
        self.starts = {path: [s[0] for s in spans_] for path, spans_ in self.spans.items()}
        self.memo: dict[tuple[str, int, str], str | None] = {}

    def __call__(self, path: str, line: int, qualname: str) -> str | None:
        key = (path, line, qualname)
        if key in self.memo:
            return self.memo[key]
        found = self.module_of_path.get(path)
        if qualname != "<module>" and path in self.spans:
            best: tuple[int, str] | None = None
            spans = self.spans[path]
            for i in range(bisect_right(self.starts[path], line) - 1, -1, -1):
                start, end, sid = spans[i]
                if end >= line and (best is None or end - start < best[0]):
                    best = (end - start, sid)
            if best is not None:
                found = best[1]
        self.memo[key] = found
        return found


@dataclass
class _Raw:
    tests: dict[str, tuple[set[str], set[str], int]]
    import_phase: set[str]
    import_by: dict[str, set[str]]
    import_paths: set[str]
    import_subprocess: bool
    environment: dict
    environment_hash: str


def _read_raw(directory: Path, owners: _Owners, project_modules: set[str]) -> _Raw:
    processes = sorted(glob.glob(str(directory / "process-*.json")))
    if not processes:
        raise EvidenceError(
            "the suite wrote no evidence: was the plugin loaded (-p diffcone_collect), "
            "and did pytest run at all?"
        )
    raw = _Raw({}, set(), defaultdict(set), set(), False, {}, "")
    environments: list[dict] = []
    seen_pids = set()
    for process_file in processes:
        with open(process_file, encoding="utf-8") as f:
            data = json.load(f)
        pid = str(data["pid"])
        seen_pids.add(pid)
        if data["errors"]:
            raise EvidenceError(
                "the recorder failed inside the suite, so the record may be incomplete: "
                + "; ".join(data["errors"][:5])
            )
        shadowed = [
            (name, file) for name, file in data["outside_modules"] if name in project_modules
        ]
        if shadowed:
            name, file = shadowed[0]
            raise EvidenceError(
                f"the suite imported {name} from {file}, outside the checkout: it ran an "
                f"installed copy of the project, not the code at this commit "
                f"({len(shadowed)} module(s))"
            )
        environments.append(data["environment"])
        raw.environment_hash = data["environment_hash"]
        symbol_of: list[str | None] = []
        file_of: list[str | None] = []
        for path, line, qualname in data["table"]:
            symbol = owners(path, line, qualname)
            symbol_of.append(symbol)
            # Code from a repository file the index does not read (outside the
            # source roots): recorded as a path the test touched.
            file_of.append(None if symbol is not None else path)
        for i in data["import_phase"]:
            if symbol_of[i] is not None:
                raw.import_phase.add(symbol_of[i])
            elif file_of[i] is not None:
                raw.import_paths.add(file_of[i])
        raw.import_paths.update(data["import_paths"])
        raw.import_subprocess |= bool(data["import_flags"] & FLAG_SUBPROCESS)
        for key, modules in data["import_by"].items():
            symbol = symbol_of[int(key)]
            if symbol is None:
                continue
            for module_path in modules:
                module = owners.module_of_path.get(module_path)
                raw.import_by[symbol].add(module or UNINDEXED_MODULE + module_path)
        tests_file = directory / f"tests-{pid}.bin"
        if data["wrote_tests"]:
            if not tests_file.exists():
                raise EvidenceError(f"process {pid} recorded no test file")
            blob = tests_file.read_bytes()
            i = 0
            while i < len(blob):
                n, m = struct.unpack_from("<II", blob, i)
                i += 8
                name = blob[i : i + n].decode()
                i += n
                record = json.loads(zlib.decompress(blob[i : i + m]))
                i += m
                symbols, paths, flags = raw.tests.setdefault(name, (set(), set(), 0))
                for c in record["codes"]:
                    if symbol_of[c] is not None:
                        symbols.add(symbol_of[c])
                    elif file_of[c] is not None:
                        paths.add(file_of[c])
                paths.update(record["paths"])
                raw.tests[name] = (symbols, paths, flags | record["flags"])
    orphans = [
        p
        for p in glob.glob(str(directory / "tests-*.bin"))
        if Path(p).stem.split("-", 1)[1] not in seen_pids
    ]
    if orphans:
        raise EvidenceError(
            f"{len(orphans)} test process(es) ended without finishing their record "
            "(a crashed worker?); the evidence would be incomplete"
        )
    if any(env != environments[0] for env in environments):
        raise EvidenceError("the test processes ran in different environments")
    raw.environment = environments[0]
    return raw


def fold(
    directories: list[Path],
    index: SourceIndex,
    *,
    commit: str,
    source_roots: list[str],
    command: str,
    project_modules: set[str],
) -> Evidence:
    """Turn one or two raw collections (the second in reverse order) into
    evidence. A test whose record differs between the two is unstable: its
    path depends on what ran before it, so it is always selected.
    ``project_modules`` are the importable names of the indexed modules: one
    of them loaded from outside the checkout means an installed copy ran."""
    owners = _Owners(index)
    raws = [_read_raw(d, owners, project_modules) for d in directories]
    if len(raws) > 1 and raws[1].environment != raws[0].environment:
        raise EvidenceError("the two collections ran in different environments")
    unstable: set[str] = set()
    tests: dict[str, tuple[set[str], set[str], int]] = {}
    # Stability is judged on symbols and tracked data files. pytest stats
    # package directories and ``__init__`` files lazily, during whichever
    # test runs first, and that is not the test's own behaviour.
    data_files = set(index.other_files)
    for raw in raws:
        for name, (symbols, paths, flags) in raw.tests.items():
            if name in tests:
                before = tests[name]
                if before[0] != symbols or (before[1] ^ paths) & data_files:
                    unstable.add(name)
                tests[name] = (before[0] | symbols, before[1] | paths, before[2] | flags)
            else:
                if len(raws) > 1 and raw is not raws[0]:
                    unstable.add(name)  # ran in one order only
                tests[name] = (set(symbols), set(paths), flags)
    if len(raws) > 1:
        unstable |= set(raws[0].tests) - set(raws[1].tests)
    symbol_table = sorted({s for symbols, _, _ in tests.values() for s in symbols})
    path_table = sorted({p for _, paths, _ in tests.values() for p in paths})
    sid = {s: i for i, s in enumerate(symbol_table)}
    pid = {p: i for i, p in enumerate(path_table)}
    shared: dict[frozenset[int], frozenset[int]] = {}

    def intern(ids: frozenset[int]) -> frozenset[int]:
        return shared.setdefault(ids, ids)

    records = {
        name: TestRecord(
            intern(frozenset(sid[s] for s in symbols)),
            intern(frozenset(pid[p] for p in paths)),
            flags | (FLAG_UNSTABLE if name in unstable else 0),
        )
        for name, (symbols, paths, flags) in sorted(tests.items())
    }
    import_by: dict[str, set[str]] = defaultdict(set)
    for raw in raws:
        for symbol, modules in raw.import_by.items():
            import_by[symbol] |= modules
    return Evidence(
        commit=commit,
        source_roots=list(source_roots),
        environment=raws[0].environment,
        environment_hash=raws[0].environment_hash,
        command=command,
        created=time.time(),
        symbols=symbol_table,
        paths=path_table,
        tests=records,
        import_phase=frozenset().union(*(r.import_phase for r in raws)),
        import_by={s: frozenset(m) for s, m in sorted(import_by.items())},
        import_paths=frozenset().union(*(r.import_paths for r in raws)),
        import_subprocess=any(r.import_subprocess for r in raws),
        reverse_checked=len(raws) > 1,
    )


# --------------------------------------------------------------------------- store


def _pack(ids) -> bytes:
    return zlib.compress(array.array("I", sorted(ids)).tobytes(), 6)


def _unpack(blob: bytes) -> frozenset[int]:
    values = array.array("I")
    values.frombytes(zlib.decompress(blob))
    return frozenset(values)


def store_name(evidence: Evidence) -> str:
    return f"{evidence.commit}-{evidence.environment_hash}.sqlite"


def write_store(evidence: Evidence, directory: Path) -> Path:
    """Write atomically: a reader never sees half a store."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / store_name(evidence)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    os.close(fd)
    try:
        db = sqlite3.connect(tmp)
        with db:
            db.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE symbols (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE paths (id INTEGER PRIMARY KEY, path TEXT NOT NULL);
                CREATE TABLE sets (id INTEGER PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE tests (
                    target TEXT PRIMARY KEY, symbols INTEGER NOT NULL,
                    paths INTEGER NOT NULL, flags INTEGER NOT NULL);
                CREATE TABLE import_by (symbol TEXT NOT NULL, module TEXT NOT NULL);
                """
            )
            meta = {
                "format": STORE_FORMAT,
                "commit": evidence.commit,
                "source_roots": evidence.source_roots,
                "environment": evidence.environment,
                "environment_hash": evidence.environment_hash,
                "command": evidence.command,
                "created": evidence.created,
                "import_phase": sorted(evidence.import_phase),
                "import_paths": sorted(evidence.import_paths),
                "import_subprocess": evidence.import_subprocess,
                "reverse_checked": evidence.reverse_checked,
            }
            db.executemany(
                "INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()]
            )
            db.executemany("INSERT INTO symbols VALUES (?, ?)", enumerate(evidence.symbols))
            db.executemany("INSERT INTO paths VALUES (?, ?)", enumerate(evidence.paths))
            set_ids: dict[int, int] = {}
            rows = []
            for name, record in evidence.tests.items():
                ids = []
                for s in (record.symbols, record.paths):
                    if id(s) not in set_ids:
                        set_ids[id(s)] = len(set_ids)
                        db.execute("INSERT INTO sets VALUES (?, ?)", (set_ids[id(s)], _pack(s)))
                    ids.append(set_ids[id(s)])
                rows.append((name, ids[0], ids[1], record.flags))
            db.executemany("INSERT INTO tests VALUES (?, ?, ?, ?)", rows)
            db.executemany(
                "INSERT INTO import_by VALUES (?, ?)",
                [(s, m) for s, modules in evidence.import_by.items() for m in sorted(modules)],
            )
        db.close()
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    evidence.location = target
    return target


def _meta(db: sqlite3.Connection) -> dict:
    return {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}


def load_store(path: Path) -> Evidence:
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise EvidenceError(f"cannot open evidence store {path}: {exc}") from exc
    try:
        meta = _meta(db)
        if meta.get("format") != STORE_FORMAT:
            raise EvidenceError(
                f"evidence store {path} has format {meta.get('format')}, this diffcone "
                f"reads {STORE_FORMAT}; collect it again"
            )
        symbols = [n for _, n in db.execute("SELECT id, name FROM symbols ORDER BY id")]
        paths = [p for _, p in db.execute("SELECT id, path FROM paths ORDER BY id")]
        sets = {i: _unpack(blob) for i, blob in db.execute("SELECT id, data FROM sets")}
        tests = {
            name: TestRecord(sets[s], sets[p], flags)
            for name, s, p, flags in db.execute("SELECT target, symbols, paths, flags FROM tests")
        }
        import_by: dict[str, set[str]] = defaultdict(set)
        for symbol, module in db.execute("SELECT symbol, module FROM import_by"):
            import_by[symbol].add(module)
    except (sqlite3.Error, KeyError, ValueError, zlib.error) as exc:
        raise EvidenceError(f"cannot read evidence store {path}: {exc}") from exc
    finally:
        db.close()
    return Evidence(
        commit=meta["commit"],
        source_roots=meta["source_roots"],
        environment=meta["environment"],
        environment_hash=meta["environment_hash"],
        command=meta["command"],
        created=meta["created"],
        symbols=symbols,
        paths=paths,
        tests=tests,
        import_phase=frozenset(meta["import_phase"]),
        import_by={s: frozenset(m) for s, m in import_by.items()},
        import_paths=frozenset(meta["import_paths"]),
        import_subprocess=meta["import_subprocess"],
        reverse_checked=meta["reverse_checked"],
        location=path,
    )


@dataclass(frozen=True)
class StoreInfo:
    path: Path
    commit: str
    environment_hash: str
    source_roots: tuple[str, ...]
    created: float
    tests: int
    python: str


def list_stores(repo: Path) -> list[StoreInfo]:
    """Every readable store of the repository, newest first."""
    found = []
    for path in sorted((repo / EVIDENCE_DIR).glob("*.sqlite")):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                meta = _meta(db)
                (count,) = db.execute("SELECT COUNT(*) FROM tests").fetchone()
            finally:
                db.close()
        except (sqlite3.Error, ValueError):
            continue
        if meta.get("format") != STORE_FORMAT:
            continue
        found.append(
            StoreInfo(
                path,
                meta["commit"],
                meta["environment_hash"],
                tuple(meta["source_roots"]),
                meta["created"],
                count,
                meta["environment"]["python"].split()[0],
            )
        )
    return sorted(found, key=lambda s: -s.created)

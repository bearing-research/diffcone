"""Spike analysis: execution evidence on pandas, following docs/evidence_design.md.

Usage: python analyse2.py REPO EVIDENCE_DIR CACHE_DIR OUT_JSON COMMIT...

Evidence comes from one collection at the repository's HEAD and stands in
for evidence at each commit's parent: an estimate of size, not a recall claim.

Per change (c~1 -> c), E and escalation as in the design:
  function body/docstring edit        {f}; if f ran during an import: the
                                      importing test module's tests, or static
                                      planning of f for a library/conftest import
  function definition edit            {f} + static readers; static planning if
                                      library decorators changed
  function/class added or deleted     static readers of its name + tests that
                                      executed an unbounded dynamic lookup site
                                      of the matching kind (+ f itself)
  variable edit                       static readers of it by name; static
                                      planning if a library reader ran at import
  class edit (not docstring-only)     members of its bases, itself and its
                                      subclasses + readers of those classes;
                                      static planning if library class decorators
                                      changed
  module-level statements/imports     static planning of that change
  non-Python file                     tests that opened it; everything if it was
                                      opened outside any test, is a compiled
                                      source, or is configuration/dependencies
"Static planning" is diffcone's planner restricted to the escalated changes.
"""

from __future__ import annotations

import ast
import collections
import glob
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import diffcone.planner as planner_mod
from diffcone.cache import IndexCache
from diffcone.classify import classify
from diffcone.model import CLASS, FUNCTION, METHOD, MODULE, REFERENCES, UNRESOLVED_DYNAMIC, VARIABLE
from diffcone.planner import _changed_unanalysed_files, _ImportReach, _index_snapshot
from diffcone.snapshot import resolve_commit

repo, evidence, cache_dir, out_json, *commits = sys.argv[1:]
REPO = Path(repo)
cache = IndexCache(Path(cache_dir))
head_index, _ = _index_snapshot(
    REPO, resolve_commit(REPO, "HEAD"), ["."], with_config=False, cache=cache
)

COMPILED = (".pyx", ".pxd", ".pxi", ".c", ".h", ".cpp", ".tpl")
CONFIG = (
    "pyproject.toml",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "meson.build",
    "environment.yml",
    "requirements-dev.txt",
    "pixi.lock",
    "pixi.toml",
    "meson.options",
)


def is_test_module(path: str) -> bool:
    return path.startswith("pandas/tests/") and not path.endswith("conftest.py")


def is_test_code(path: str) -> bool:
    """Test modules and the conftests beside them."""
    return path.startswith("pandas/tests/")


# ---- evidence ---------------------------------------------------------------------
by_path = collections.defaultdict(list)
for sym in head_index.symbols.values():
    for start, end in sym.line_ranges:
        by_path[sym.path].append((start, end, sym.id))
module_of_path = {s.path: s.id for s in head_index.symbols.values() if s.kind == MODULE}


def owner(path: str, line: int, qualname: str) -> str | None:
    if qualname == "<module>":
        return module_of_path.get(path)
    best = None
    for start, end, sid in by_path.get(path, ()):
        if start <= line <= end and (best is None or end - start < best[0]):
            best = (end - start, sid)
    return best[1] if best else module_of_path.get(path)


X: dict[str, set[str]] = collections.defaultdict(set)
F: dict[str, set[str]] = collections.defaultdict(set)
import_by: dict[str, set[str]] = collections.defaultdict(set)
import_phase: set[str] = set()
import_files: set[str] = set()
for cf in glob.glob(f"{evidence}/codes-*.json"):
    pid = cf.rsplit("-", 1)[1].split(".")[0]
    data = json.load(open(cf))
    sym_of = [owner(p, line, q) for p, line, q in data["table"]]
    import_phase |= {sym_of[i] for i in data["import_phase"] if sym_of[i]}
    import_files |= set(data["import_files"])
    for k, mods in data["import_by"].items():
        if sym_of[int(k)]:
            import_by[sym_of[int(k)]] |= set(mods)
    b = open(f"{evidence}/tests-{pid}.bin", "rb").read()
    i = 0
    while i < len(b):
        n, m = struct.unpack_from("<II", b, i)
        i += 8
        name = b[i : i + n].decode()
        i += n
        rec = json.loads(zlib.decompress(b[i : i + m]))
        i += m
        X[name] |= {sym_of[c] for c in rec["codes"] if sym_of[c]}
        F[name] |= set(rec["files"])
ALL = set(X)
executed_by = collections.defaultdict(set)
for t, syms in X.items():
    for s in syms:
        executed_by[s].add(t)
opened_by = collections.defaultdict(set)
for t, files in F.items():
    for f in files:
        opened_by[f].add(t)
tests_of_module = collections.defaultdict(set)  # tests that executed any code of a module path
sym_path = {s.id: s.path for s in head_index.symbols.values()}
for t, syms in X.items():
    for s in syms:
        if s in sym_path:
            tests_of_module[sym_path[s]].add(t)
print(
    f"tests {len(ALL)}; import-phase symbols {len(import_phase)}; "
    f"files opened at import {len(import_files)}",
    flush=True,
)


# ---- static helpers -------------------------------------------------------------
def reverse_refs(indexes):
    refs = collections.defaultdict(set)
    by_name = collections.defaultdict(set)
    dyn_method, dyn_module = set(), set()
    for index in indexes:
        for e in index.edges:
            if e.kind == REFERENCES:
                refs[e.target].add(e.source)
        for u in index.unresolved:
            if u.kind == UNRESOLVED_DYNAMIC:
                # A read off an object of unknown type can find a method; one
                # off a module (or globals()/vars()) can find a module-level name.
                if "receiver from elsewhere" in u.detail or u.detail.startswith("vars"):
                    dyn_method.add(u.symbol)
                elif "import" not in u.detail:
                    dyn_module.add(u.symbol)
            elif u.name:
                by_name[u.name].add(u.symbol)
    return refs, by_name, dyn_method, dyn_module


def tests_executing(symbols) -> set[str]:
    out = set()
    for s in symbols:
        out |= executed_by.get(s, set())
    return out


_src_cache: dict[tuple[str, str], ast.Module | None] = {}


def parsed(commit: str, path: str):
    key = (commit, path)
    if key not in _src_cache:
        r = subprocess.run(["git", "-C", repo, "show", f"{commit}:{path}"], capture_output=True)
        try:
            _src_cache[key] = ast.parse(r.stdout) if r.returncode == 0 else None
        except SyntaxError:
            _src_cache[key] = None
    return _src_cache[key]


def _defs_named(node, name: str) -> list:
    """Every def/class called ``name`` directly in ``node``'s body, looking
    through ``if``/``try``/``with``/loops (``if TYPE_CHECKING:``) but not into
    other definitions, in source order (``@overload`` stubs included)."""
    found = []
    stack = list(reversed(getattr(node, "body", [])))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n.name == name:
                found.append(n)
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            stack.extend(reversed(getattr(n, field, []) or []))
    return found


def decorators(commit: str, sym) -> list[list[str]] | None:
    """The decorator lists of every definition of ``sym`` (all overloads and
    conditional variants), or None when the source cannot be read."""
    tree = parsed(commit, sym.path)
    if tree is None:
        return None
    nodes = [tree]
    for name in sym.id[len(sym.module) + 1 :].split("."):
        nodes = [d for n in nodes for d in _defs_named(n, name)]
        if not nodes:
            return None
    return [[ast.unparse(d) for d in n.decorator_list] for n in nodes]


def is_fixture(decs) -> bool:
    return any("fixture" in d for variant in decs or () for d in variant)


def tests_in_scope(path: str) -> set[str]:
    """Tests that can see a fixture defined in ``path``: a conftest's directory
    and below, or the test module itself."""
    if path.endswith("conftest.py"):
        prefix = path[: -len("conftest.py")]
        return {t for t in ALL if t.startswith(prefix)}
    return {t for t in ALL if t.split("::", 1)[0] == path}


def static_select(c: str, ids: set[str]) -> set[str]:
    """diffcone's static plan with the change list restricted to ``ids``."""
    real, real_files, real_index = (
        planner_mod.classify,
        planner_mod._changed_unanalysed_files,
        planner_mod._index_snapshot,
    )
    planner_mod.classify = lambda b, h: [ch for ch in real(b, h) if ch.id in ids]
    # Evidence handles files through what each test opened.
    planner_mod._changed_unanalysed_files = lambda b, h: []

    def no_dynamic(*a, **k):
        # Dynamic sites are evidence's business: a test that ran one is in the
        # record through the code it reached (see analyse()).
        index, snap = real_index(*a, **k)
        index.unresolved = {u for u in index.unresolved if u.kind != UNRESOLVED_DYNAMIC}
        return index, snap

    planner_mod._index_snapshot = no_dynamic
    try:
        p = planner_mod.plan(
            repo, c + "~1", c, source_roots=["."], discover_runners=["pytest"], cache=cache
        )
    finally:
        planner_mod.classify, planner_mod._changed_unanalysed_files, planner_mod._index_snapshot = (
            real,
            real_files,
            real_index,
        )
    return {d.target.runner_id for d in p.selected} & ALL


# ---- per commit -----------------------------------------------------------------------
def analyse(c: str) -> dict:
    base_c, head_c = resolve_commit(REPO, c + "~1"), resolve_commit(REPO, c)
    b, _ = _index_snapshot(REPO, base_c, ["."], with_config=False, cache=cache)
    h, _ = _index_snapshot(REPO, head_c, ["."], with_config=False, cache=cache)
    refs, by_name, dyn_method, dyn_module = reverse_refs((b, h))
    E: set[str] = set()
    direct: set[str] = set()  # tests selected directly (opened files, test modules)
    escalate: set[str] = set()
    select_all: list[str] = []
    notes: list[str] = []

    def readers(sym_id: str, name: str) -> set[str]:
        return refs.get(sym_id, set()) | by_name.get(name, set())

    def import_effect(sym_id: str) -> None:
        for mod_path in import_by.get(sym_id, ()):
            if is_test_module(mod_path):
                direct.update(tests_of_module.get(mod_path, set()))
            else:
                escalate.add(sym_id)
                notes.append(f"{sym_id} ran importing {mod_path}")
        if sym_id in import_phase and sym_id not in import_by:
            escalate.add(sym_id)
            notes.append(f"{sym_id} ran outside any test")

    def import_time_readers(rs: set[str], sym_id: str) -> None:
        """Readers that run at import: module or class top-level code (the
        recorder never credits ``<module>`` code to an importer), or code an
        import ran. Their results are baked into objects."""
        for r in rs:
            rsym = h.symbols.get(r) or b.symbols.get(r)
            if rsym is None:
                continue
            if rsym.kind in (MODULE, CLASS):
                if is_test_module(rsym.path):
                    direct.update(tests_of_module.get(rsym.path, set()))
                else:
                    escalate.add(sym_id)
                    notes.append(f"{sym_id} read at import by {r}")
            for mod_path in import_by.get(r, ()):
                if is_test_module(mod_path):
                    direct.update(tests_of_module.get(mod_path, set()))
                else:
                    escalate.add(sym_id)
                    notes.append(f"{sym_id} read by {r}, run importing {mod_path}")

    def name_sites(sym, method: bool) -> set[str]:
        if is_test_code(sym.path):
            # Found by the runner and by test code, not by library lookups.
            return {x for x in (dyn_method | dyn_module) if x.startswith("pandas.tests.")}
        return dyn_method if method else dyn_module

    for ch in classify(b, h):
        sym = ch.head or ch.base
        kinds = set(ch.changes)
        if not ch.carries_impact and kinds != {"docstring_changed"}:
            continue
        if sym.kind in (FUNCTION, METHOD):
            if sym.name.startswith("pytest_") and sym.path.endswith("conftest.py"):
                select_all.append(f"{sym.id} (pytest hook)")
                continue
            base_decs = decorators(base_c, ch.base) if ch.base else None
            head_decs = decorators(head_c, ch.head) if ch.head else None
            fixture = is_test_code(sym.path) and is_fixture((base_decs or []) + (head_decs or []))
            if kinds & {"added", "deleted"}:
                E |= readers(sym.id, sym.name) | {sym.id} | name_sites(sym, sym.kind == METHOD)
                if fixture:  # a new fixture can shadow a same-named one in its scope
                    direct |= tests_in_scope(sym.path)
                continue
            E.add(sym.id)
            if kinds != {"docstring_changed"}:
                # Whatever else changed with it (a body edit usually adds or
                # redirects calls too): code an import ran is import-time.
                import_effect(sym.id)
            if kinds & {"definition_changed", "annotations_changed"}:
                E |= readers(sym.id, sym.name)
            if base_decs != head_decs:
                if fixture:  # autouse, scope, params, name: its whole scope
                    direct |= tests_in_scope(sym.path)
                elif not is_test_module(sym.path):
                    escalate.add(sym.id)
                    notes.append(f"{sym.id} decorators changed")
        elif sym.kind == VARIABLE:
            rs = readers(sym.id, sym.name)
            E |= rs | {sym.id}
            if kinds & {"added", "deleted"}:
                E |= name_sites(
                    sym,
                    method=bool(
                        sym.container
                        and (h.symbols.get(sym.container) or b.symbols.get(sym.container)).kind
                        == CLASS
                    ),
                )
            import_time_readers(rs, sym.id)
        elif sym.kind == CLASS:
            if kinds == {"docstring_changed"}:
                continue
            if kinds & {"added", "deleted"}:
                E |= readers(sym.id, sym.name) | {sym.id} | name_sites(sym, method=False)
                continue
            # Spike approximation: the class's own members and readers; the
            # design also takes its bases' and subclasses' members.
            members = {s for index in (b, h) for s in index.symbols if s.startswith(sym.id + ".")}
            rs = readers(sym.id, sym.name)
            E |= members | rs | {sym.id}
            import_time_readers(rs, sym.id)
            base_decs = decorators(base_c, ch.base) if ch.base else None
            head_decs = decorators(head_c, ch.head) if ch.head else None
            if base_decs != head_decs and not is_test_module(sym.path):
                escalate.add(sym.id)
                notes.append(f"{sym.id} class decorators changed")
        elif kinds & {"added", "deleted"}:
            # Only code that names a module reaches it, and that code changed too.
            E |= readers(sym.id, sym.name) | {sym.id}
        else:  # module-level statements, imports
            escalate.add(sym.id)
            notes.append(f"{sym.id} module-level")
    for f in _changed_unanalysed_files(b, h):
        if f.endswith(COMPILED) or f.rsplit("/", 1)[-1] in CONFIG or f.endswith(".lock"):
            select_all.append(f)
        elif f in import_files:
            select_all.append(f + " (opened outside a test)")
        else:
            direct |= opened_by.get(f, set())
    if escalate:
        # Dynamic sites that can see an escalated module: closure-bounded ones
        # whose module's import closure holds it, reads off objects from
        # elsewhere, and, for a library module, imports named at runtime.
        reach = _ImportReach(b, h)
        esc_modules = {(h.symbols.get(x) or b.symbols.get(x)).module for x in escalate}
        esc_paths = {(h.symbols.get(m) or b.symbols.get(m)).path for m in esc_modules}
        # conftests count as library here: their objects reach every test in scope
        library = any(not is_test_module(path) for path in esc_paths)
        for index in (b, h):
            for u in index.unresolved:
                if u.kind != UNRESOLVED_DYNAMIC:
                    continue
                if "import" in u.detail:
                    if library:
                        E.add(u.symbol)
                elif ("receiver from elsewhere" in u.detail and library) or reach.closure_of(
                    u.symbol
                ) & esc_modules:
                    # A test module's import-time objects reach library code only
                    # while its own tests run (selected below) or through its
                    # importers (static planning).
                    E.add(u.symbol)
        for path in esc_paths:
            if is_test_module(path):
                direct.update(tests_of_module.get(path, set()))
    if select_all:
        selected = set(ALL)
    else:
        selected = tests_executing(E) | direct
        if escalate:
            selected |= static_select(c, escalate)
    return {
        "commit": c,
        "selected": len(selected),
        "of": len(ALL),
        "E": len(E),
        "escalated": sorted(escalate)[:5],
        "n_escalated": len(escalate),
        "select_all": select_all[:3],
        "notes": notes[:4],
        "evidence_only": len(tests_executing(E) | direct),
    }


rows = []
for c in commits:
    try:
        r = analyse(c)
    except Exception as exc:
        # Counted as selecting everything, so a failure cannot flatter the numbers.
        print(f"{c[:10]} FAILED, counted as select-all: {type(exc).__name__}: {exc}", flush=True)
        r = {
            "commit": c,
            "selected": len(ALL),
            "of": len(ALL),
            "E": 0,
            "escalated": [],
            "n_escalated": 0,
            "select_all": [f"analysis failed: {type(exc).__name__}"],
            "notes": [],
            "evidence_only": len(ALL),
            "failed": True,
        }
        rows.append(r)
        continue
    rows.append(r)
    tag = (
        "ALL: " + ", ".join(r["select_all"])
        if r["select_all"]
        else (
            f"escalated {r['n_escalated']}: {'; '.join(r['notes'][:2])}" if r["n_escalated"] else ""
        )
    )
    print(
        f"{c[:10]} select {r['selected']:6d} ({100 * r['selected'] / r['of']:5.1f}%) "
        f"evidence-only {100 * r['evidence_only'] / r['of']:5.1f}%  |E|={r['E']:5d}  {tag}",
        flush=True,
    )
json.dump(rows, open(out_json, "w"), indent=1)
frac = sorted(r["selected"] / r["of"] for r in rows)
print(
    f"\ncommits {len(rows)}; median {100 * frac[len(frac) // 2]:.1f}%; "
    f"under 25%: {sum(1 for x in frac if x < 0.25)}; "
    f"select-all: {sum(1 for r in rows if r['select_all'])}; "
    f"with static escalation: {sum(1 for r in rows if r['n_escalated'])}"
)

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


def decorators(commit: str, sym) -> list[str] | None:
    tree = parsed(commit, sym.path)
    if tree is None:
        return None
    qual = sym.id[len(sym.module) + 1 :].split(".")
    node = tree
    for name in qual:
        node = next(
            (
                n
                for n in ast.iter_child_nodes(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == name
            ),
            None,
        )
        if node is None:
            return None
    return [ast.unparse(d) for d in node.decorator_list]


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

    for ch in classify(b, h):
        sym = ch.head or ch.base
        kinds = set(ch.changes)
        if not ch.carries_impact and kinds != {"docstring_changed"}:
            continue
        in_tests = sym.path.startswith("pandas/tests/")
        if sym.kind in (FUNCTION, METHOD):
            if kinds & {"added", "deleted"}:
                E |= readers(sym.id, sym.name) | {sym.id}
                if in_tests:
                    # Test functions and test-class methods are found by the
                    # runner and by test code, not by library lookups.
                    E |= {x for x in (dyn_method | dyn_module) if x.startswith("pandas.tests.")}
                else:
                    E |= dyn_method if sym.kind == METHOD else dyn_module
                continue
            E.add(sym.id)
            if kinds <= {"body_changed", "docstring_changed"}:
                if kinds != {"docstring_changed"}:
                    import_effect(sym.id)
                continue
            E |= readers(sym.id, sym.name)
            if not in_tests and decorators(base_c, ch.base or sym) != decorators(
                head_c, ch.head or sym
            ):
                escalate.add(sym.id)
                notes.append(f"{sym.id} decorators changed")
        elif sym.kind == VARIABLE:
            rs = readers(sym.id, sym.name)
            E |= rs | {sym.id}
            if not in_tests and any(
                any(not is_test_module(m) for m in import_by.get(r, ())) for r in rs
            ):
                escalate.add(sym.id)
                notes.append(f"{sym.id} read at import")
        elif sym.kind == CLASS:
            if kinds == {"docstring_changed"}:
                continue
            if kinds & {"added", "deleted"}:
                E |= readers(sym.id, sym.name) | {sym.id}
                E |= (
                    {x for x in dyn_module if x.startswith("pandas.tests.")}
                    if in_tests
                    else dyn_module
                )
                continue
            # Spike approximation: the class's own members and readers; the
            # design also takes its bases' and subclasses' members.
            members = {s for index in (b, h) for s in index.symbols if s.startswith(sym.id + ".")}
            E |= members | readers(sym.id, sym.name) | {sym.id}
            if not in_tests and decorators(base_c, ch.base or sym) != decorators(
                head_c, ch.head or sym
            ):
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
        library = any(not m.startswith("pandas.tests.") for m in esc_modules)
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
        for m in esc_modules:
            if m.startswith("pandas.tests."):
                path = (h.symbols.get(m) or b.symbols.get(m)).path
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
        print(f"{c[:10]} skipped: {type(exc).__name__}: {exc}", flush=True)
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

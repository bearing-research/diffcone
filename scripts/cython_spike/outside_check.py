"""Post-check of outside_replay.py's rows (roadmap item 8): for each changed
name in a planned row, the oracle tests of the functions at X mentioning it
must be covered by the evidence (their own records, or a recorded caller
for nogil/cpdef) in the files that can see the name; in any other file the
name must have its own binding (a cimport is local to its module), or the
row is flagged for review.

usage: outside_check.py REPO X STORE RUN_TRACE replay.jsonl
"""

import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

from diffcone.cython import IMPORT, names_module, pxd_stem, symbol_id
from diffcone.evidence import load_store
from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot

repo, x, store, trace, rows = (
    Path(sys.argv[1]),
    sys.argv[2],
    sys.argv[3],
    Path(sys.argv[4]),
    sys.argv[5],
)
ev = load_store(Path(store))
ix = build_index(read_snapshot(repo, x, ["."]))
mods = ix.cython
oracle = defaultdict(set)
for f in glob.glob(str(trace / "*.jsonl")):
    for line in open(f):
        r = json.loads(line)
        for path, lineno in r["x"]:
            m = mods.get(path)
            fn = m.function_at(lineno) if m else None
            if fn:
                oracle[(path, fn.name)].add(r["t"])
ran = defaultdict(set)
for t, rec in ev.tests.items():
    for i in rec.symbols:
        ran[ev.symbols[i]].add(t)
mentions = defaultdict(list)
for p, m in mods.items():
    for fn in m.functions:
        for n in fn.names:
            mentions[n].append((p, fn))


def covered(p, fn, seen=None):
    """Evidence tests for fn, through recorded callers for nogil/cpdef."""
    seen = seen or set()
    out = set(ran.get(symbol_id(p, fn.name), ()))
    if fn.nogil or fn.cpdef:
        for cp, cf in mentions.get(fn.simple_name, ()):
            key = (cp, cf.name)
            if key not in seen and key != (p, fn.name):
                seen.add(key)
                out |= covered(cp, cf, seen)
    return out


def seers(path, visible, name=""):
    if visible or path.endswith(".pxi"):
        return None
    seen = {path} | {p for p in mods if p.endswith(".pxi")}
    if path.endswith(".pyx"):
        twin = path[:-4] + ".pxd"
        if twin not in mods or not binds(twin, name):
            return seen
        path = twin
        seen.add(path)
    seen.add(path[:-4] + ".pyx")
    frontier = [path]
    while frontier:
        stem = pxd_stem(frontier.pop())
        for p, m in mods.items():
            if p not in seen and (
                m.statements is None
                or any(
                    s.kind == IMPORT and not s.visible and names_module(s, stem)
                    for s in m.statements
                )
            ):
                seen.add(p)
                if p.endswith(".pxd"):
                    frontier.append(p)
    return seen


def binds(p, name):
    m = mods[p]
    return any(name in s.names for s in m.statements or ()) or any(
        f.simple_name == name for f in m.functions
    )


for line in open(rows):
    r = json.loads(line)
    if "selected" not in r or r["fallbacks"] or r["selected"] == r["targets"]:
        continue
    problems, scoped = [], 0
    for item in r["names"]:
        fname, name, change = item.split(":")
        name = name.split(".")[-1]
        path = (
            next(p for p in mods if p.endswith("/" + fname))
            if any(p.endswith("/" + fname) for p in mods)
            else None
        )
        if path is None:
            continue
        visible = False  # the strict case; visible names reach every file anyway
        s = seers(path, visible, name)
        for p, fn in mentions.get(name, ()):
            o = oracle.get((p, fn.name), set())
            if not o:
                continue
            if s is None or p in s:
                miss = o - covered(p, fn)
                if miss:
                    problems.append(
                        f"{p}::{fn.name} names {name}: {len(miss)} oracle tests not in evidence"
                    )
            elif not binds(p, name):
                problems.append(
                    f"{p}::{fn.name} names {name} without its own binding ({len(o)} tests)"
                )
            else:
                scoped += len(o)
    print(
        r["commit"],
        "OK" if not problems else "CHECK",
        f"scoped-out oracle tests {scoped}",
        f"flagged ids {r['flagged']}",
    )
    for pr in problems[:10]:
        print("   ", pr)

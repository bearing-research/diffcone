"""Spike analysis (roadmap item 7): are function-level Cython records from a
profile=True build sound against a line-traced oracle, and how much would the
rule select on recent compiled edits?

usage: analyse.py ROOT(pandas worktree at C) RUN_PROF RUN_TRACE PANDAS_GIT
"""

import glob
import hashlib
import json
import subprocess
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cyblocks import read, read_tree  # noqa: E402

# ruff: noqa: E741 (E and O are the evidence and the oracle, as in the roadmap)

root, run_prof, run_trace, repo = (Path(a) for a in sys.argv[1:5])
funcs = read_tree(root)
by_path = defaultdict(list)
for f in funcs:
    by_path[f.path].append(f)
starts = {p: [f.start for f in sorted(fs, key=lambda f: f.start)] for p, fs in by_path.items()}
sorted_funcs = {p: sorted(fs, key=lambda f: f.start) for p, fs in by_path.items()}


def owner(path, line):
    """The outermost function whose span holds the line: a nested function
    belongs to its parent, as in diffcone's Python index (methods are not
    nested in a function, so they stay their own)."""
    fs = sorted_funcs.get(path)
    if not fs:
        return None
    best = None
    for i in range(bisect_right(starts[path], line) - 1, -1, -1):
        f = fs[i]
        if f.end >= line and (best is None or f.end - f.start > best.end - best.start):
            best = f
        if f.start < line - 5000:
            break
    return best


def load(run):
    tests = defaultdict(set)
    for name in glob.glob(str(run / "*.jsonl")):
        for line in open(name):
            d = json.loads(line)
            seen = tests[d["t"]]  # a test with no Cython code still counts as recorded
            for path, lineno in d["x"]:
                if not path.startswith("pandas/_libs/"):
                    continue
                f = owner(path, lineno)
                if f is not None:
                    seen.add((f.path, f.qualname))
    return tests


E = load(run_prof)
O = load(run_trace)
# Only tests both runs recorded can be compared.
common = set(E) & set(O)
E = {t: E[t] for t in common}
O = {t: O[t] for t in common}
print(f"{len(funcs)} Cython functions; tests: evidence {len(E)}, oracle {len(O)}")
E_by_f, O_by_f = defaultdict(set), defaultdict(set)
for t, fs in E.items():
    for f in fs:
        E_by_f[f].add(t)
for t, fs in O.items():
    for f in fs:
        O_by_f[f].add(t)
key = {(f.path, f.qualname): f for f in funcs}

# Static callers by simple name, for nogil functions (no start events).
callers = defaultdict(set)
for f in funcs:
    for name in f.calls:
        callers[name].add((f.path, f.qualname))


def caller_closure(fk):
    seen, stack = set(), [fk]
    while stack:
        x = stack.pop()
        for c in callers.get(x[1].rsplit(".", 1)[-1], ()):
            if c not in seen and c != fk:
                seen.add(c)
                stack.append(c)
    return seen


def selected_for(fk):
    """Tests the rule selects when function fk changes."""
    out = set(E_by_f.get(fk, ()))
    f = key.get(fk)
    # nogil functions raise no start; a cpdef's C body raises none when called
    # with skip_dispatch (an explicit `Base.method(self, ...)` call): both are
    # covered through the Cython functions that name them.
    cpdef = f is not None and f.header.lstrip().startswith("cpdef")
    if f is not None and (f.nogil or cpdef or fk not in E_by_f):
        for c in caller_closure(fk):
            out |= E_by_f.get(c, set())
    return out


missing = Counter()
cats = Counter()
examples = defaultdict(list)
for fk, otests in O_by_f.items():
    f = key[fk]
    direct = otests - E_by_f.get(fk, set())
    if not direct:
        cats["direct"] += 1
        continue
    rest = otests - selected_for(fk)
    cat = ("nogil" if f.nogil else "other") + ("+callers ok" if not rest else " MISS")
    cats[cat] += 1
    if rest:
        missing[fk] = len(rest)
        if len(examples[cat]) < 15:
            examples[cat].append((fk, len(rest), len(otests), f.header[:90], sorted(rest)[:2]))
print("functions the oracle saw executed:", len(O_by_f))
for c, n in cats.most_common():
    print(f"  {c}: {n}")
for cat, ex in examples.items():
    print(f"== {cat}")
    for e in ex:
        print("  ", e)
print("tests missed in total (function, test) pairs:", sum(missing.values()))


# ---------------------------------------------------------------- recent edits
def git(*a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout


def bodies(rev, path):
    text = git("show", f"{rev}:{path}")
    if not text:
        return {}, ""
    tmp = Path("/tmp/cyspike_src" + Path(path).suffix)
    tmp.write_text(text)
    lines = text.split("\n")
    fs = read(tmp, path)
    out = {}
    covered = set()
    for f in fs:
        out[f.qualname] = hashlib.sha1("\n".join(lines[f.start - 1 : f.end]).encode()).hexdigest()
        covered.update(range(f.start, f.end + 1))
    rest = "\n".join(
        l
        for i, l in enumerate(lines, 1)
        if i not in covered and l.strip() and not l.strip().startswith("#")
    )
    return out, hashlib.sha1(rest.encode()).hexdigest()


commits = git("log", "--first-parent", "--format=%H", "-n", "500", "3f57341").split()
rows = []
total = len(set(E) | set(O))
for c in commits:
    changed = git("diff-tree", "--no-commit-id", "--name-only", "-r", c).split()
    cy = [
        p for p in changed if p.startswith("pandas/_libs/") and p.endswith((".pyx", ".pxd", ".pxi"))
    ]
    other = [
        p
        for p in changed
        if p.endswith((".pxi.in", ".c", ".h", ".cpp")) or p.endswith("meson.build")
    ]
    if not cy and not other:
        continue
    funcs_changed, module_level = [], []
    for p in cy:
        a, ra = bodies(f"{c}^", p)
        b, rb = bodies(c, p)
        for q in set(a) | set(b):
            if a.get(q) != b.get(q):
                funcs_changed.append((p, q))
        if ra != rb:
            module_level.append(p)
    sel = set()
    unknown = 0
    oracle = set()
    for fk in funcs_changed:
        if fk in key:
            sel |= selected_for(fk)
            oracle |= O_by_f.get(fk, set())
        else:
            unknown += 1  # added/removed since, or renamed by C
    status = "all" if other or module_level else f"{len(sel)}"
    rows.append(
        (
            c[:10],
            status,
            len(funcs_changed),
            unknown,
            len(oracle - sel),
            other[:2],
            module_level[:2],
        )
    )
print(f"\n{len(rows)} commits touch compiled sources or build files (tests: {total})")
for r in rows:
    print("  ", r)
fn_only = [r for r in rows if r[1] != "all"]
print(
    "function-level only:",
    len(fn_only),
    "median selected",
    sorted(int(r[1]) for r in fn_only)[len(fn_only) // 2] if fn_only else None,
    "oracle misses",
    sum(r[4] for r in fn_only),
)

# ---------------------------------------------------------------- miss shape
per_test = Counter()
per_func = {}
for fk, otests in O_by_f.items():
    rest = otests - selected_for(fk)
    if rest:
        per_func[fk] = len(rest)
        per_test.update(rest)
print("\nmissed pairs by test: tests", len(per_test), "top", per_test.most_common(12))
print("missed pairs by function, largest:", sorted(per_func.items(), key=lambda x: -x[1])[:12])

# ---------------------------------------------------------------- symmetry
rev = Counter()
rev_tests = Counter()
for fk, etests in E_by_f.items():
    extra = etests - O_by_f.get(fk, set())
    if extra:
        rev[fk] = len(extra)
        rev_tests.update(extra)
print(
    "\nreverse (started in evidence, no oracle line): pairs",
    sum(rev.values()),
    "functions",
    len(rev),
    "tests",
    len(rev_tests),
)
print("  largest:", rev.most_common(10))
both = set(per_test) & set(rev_tests)
print(
    "  tests with misses in both directions:", len(both), "of", len(per_test), "forward-miss tests"
)
Path(Path(__file__).parent / "forward_miss_tests.txt").write_text(
    "\n".join(sorted(per_test)) + "\n"
)
Path(Path(__file__).parent / "forward_miss_pairs.json").write_text(
    json.dumps(
        sorted([f"{fk[0]}::{fk[1]}", t] for fk, ot in O_by_f.items() for t in ot - selected_for(fk))
    )
)

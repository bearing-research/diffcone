"""Roadmap item 8 check: real Cython edits outside function bodies, replayed
at the evidence commit X by reverse-applying their hunks, planned with the
store, and compared with the line-traced oracle.

usage: outside_replay.py REPO X STORE RUN_TRACE [--last 500] [--out replay.jsonl]
"""

from __future__ import annotations

import argparse
import glob
import json
import keyword
import re
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from diffcone.cache import IndexCache, default_cache_dir
from diffcone.cython import cython_changes, is_cython, read
from diffcone.evidence import load_store
from diffcone.indexer import build_index
from diffcone.planner import plan
from diffcone.snapshot import read_snapshot

WORD = re.compile(r"\b[A-Za-z_]\w*\b")


def git(repo, *args, check=True, input=None):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, input=input)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r


def main():
    p = argparse.ArgumentParser()
    p.add_argument("repo", type=Path)
    p.add_argument("x")
    p.add_argument("store", type=Path)
    p.add_argument("run_trace", type=Path)
    p.add_argument("--last", type=int, default=500)
    p.add_argument("--out", type=Path, default=Path("replay.jsonl"))
    a = p.parse_args()
    repo = a.repo.resolve()
    x = git(repo, "rev-parse", a.x).stdout.strip()
    evidence = load_store(a.store)
    index_x = build_index(read_snapshot(repo, x, ["."]))
    oracle = defaultdict(set)  # (path, function) -> tests
    for name in glob.glob(str(a.run_trace / "*.jsonl")):
        for line in open(name):
            r = json.loads(line)
            for path, lineno in r["x"]:
                m = index_x.cython.get(path)
                f = m.function_at(lineno) if m else None
                if f is not None:
                    oracle[(path, f.name)].add(r["t"])
    mentions = defaultdict(set)  # identifier -> oracle tests of functions at X naming it
    for path, m in index_x.cython.items():
        for f in m.functions:
            for n in f.names:
                mentions[n] |= oracle.get((path, f.name), set())
    base = "3f57341"
    commits = git(
        repo, "log", "--first-parent", "--format=%H", "-n", str(a.last), base
    ).stdout.split()
    cache = IndexCache(default_cache_dir(repo))
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "replay"
        git(repo, "worktree", "add", "--detach", "-q", str(work), x)
        try:
            out = a.out.open("w")
            for c in commits:
                paths = [
                    q
                    for q in git(
                        repo, "diff-tree", "--no-commit-id", "--name-only", "-r", c
                    ).stdout.split()
                    if is_cython(q)
                ]
                if not paths:
                    continue

                def mod(rev, q):
                    r = git(repo, "show", f"{rev}:{q}", check=False)
                    return read(q, r.stdout) if r.returncode == 0 else None

                ch = cython_changes(
                    {q: m for q in paths if (m := mod(c + "^", q))},
                    {q: m for q in paths if (m := mod(c, q))},
                )
                if not ch.names and not ch.files:
                    continue  # body-only: stage 4's replay
                git(work, "checkout", "-q", "--detach", "-f", x)
                applied = []
                for q in paths:
                    diff = git(repo, "diff", f"{c}^", c, "--", q).stdout
                    if git(work, "apply", "-R", "-", input=diff, check=False).returncode == 0:
                        applied.append(q)
                if not applied:
                    print(f"{c[:10]}: no hunk applies at X", flush=True)
                    out.write(json.dumps({"commit": c[:10], "applied": []}) + "\n")
                    continue
                git(
                    work,
                    "-c",
                    "user.email=s@l",
                    "-c",
                    "user.name=s",
                    "commit",
                    "-qam",
                    f"revert cython of {c[:10]}",
                )
                head = git(work, "rev-parse", "HEAD").stdout.strip()
                index_h = build_index(read_snapshot(repo, head, ["."]))
                at_x = cython_changes(index_x.cython, index_h.cython)
                names = {n.name for n in at_x.names}
                # Identifiers on changed lines outside every function span (at
                # X for removed lines, at X' for added ones): an independent
                # over-approximation of what the edit bound.
                ids = set()
                for q in applied:
                    spans = {}
                    for side, mod_ in (("-", index_x.cython.get(q)), ("+", index_h.cython.get(q))):
                        spans[side] = [(f.start, f.end) for f in mod_.functions] if mod_ else []
                    old_no = new_no = 0
                    for line in git(work, "diff", "-U0", x, head, "--", q).stdout.splitlines():
                        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
                        if m:
                            old_no, new_no = int(m.group(1)), int(m.group(2))
                            continue
                        if line.startswith(("+++", "---")) or line[:1] not in "+-":
                            continue
                        side = line[0]
                        no = old_no if side == "-" else new_no
                        if side == "-":
                            old_no += 1
                        else:
                            new_no += 1
                        if any(a <= no <= b for a, b in spans[side]):
                            continue
                        code = line[1:].split("#", 1)[0]
                        ids |= {w for w in WORD.findall(code) if not keyword.iskeyword(w)}
                if at_x.files:
                    row = {
                        "commit": c[:10],
                        "applied": applied,
                        "file_level": [f"{q}: {w}" for q, w in at_x.files],
                        "names": sorted(
                            f"{n.path.rsplit('/', 1)[-1]}:{n.name}:{n.change}" for n in at_x.names
                        ),
                    }
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(
                        f"{c[:10]}: file-level: {at_x.files[0][0]}: {at_x.files[0][1][:120]}",
                        flush=True,
                    )
                    continue
                t = time.time()
                planned = plan(
                    repo,
                    x,
                    head,
                    source_roots=["."],
                    discover_runners=["pytest"],
                    cache=cache,
                    evidence=evidence,
                )
                seconds = time.time() - t
                selected = {d.target.runner_id for d in planned.selected}
                fallbacks = [f"{f.rule}: {f.detail[:160]}" for f in planned.fallbacks]
                expected = set().union(*(mentions.get(n, set()) for n in names)) if names else set()
                missed_names = sorted(expected - selected) if not planned.fallbacks else []
                flagged = {}
                if not planned.fallbacks:
                    for w in sorted(ids - names):
                        extra = mentions.get(w, set()) - selected
                        if extra:
                            flagged[w] = len(extra)
                row = {
                    "commit": c[:10],
                    "applied": applied,
                    "selected": len(selected),
                    "targets": len(planned.decisions),
                    "fallbacks": fallbacks,
                    "names": sorted(
                        f"{n.path.rsplit('/', 1)[-1]}:"
                        f"{n.scope + '.' if n.scope else ''}{n.name}:{n.change}"
                        for n in at_x.names
                    ),
                    "files": [f"{q}: {w}" for q, w in at_x.files],
                    "functions": len(at_x.functions),
                    "oracle": len(expected),
                    "missed": missed_names[:10],
                    "n_missed": len(missed_names),
                    "flagged": flagged,
                    "seconds": round(seconds, 1),
                }
                out.write(json.dumps(row) + "\n")
                out.flush()
                print(
                    f"{c[:10]}: {len(applied)}/{len(paths)} files, "
                    f"select {len(selected)}/{len(planned.decisions)}, "
                    f"oracle {len(expected)}, missed {len(missed_names)}, flagged {len(flagged)}"
                    f"{' FALLBACK ' + fallbacks[0][:140] if fallbacks else ''}",
                    flush=True,
                )
        finally:
            git(repo, "worktree", "remove", "--force", str(work), check=False)


if __name__ == "__main__":
    main()

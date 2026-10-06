"""Recall of diffcone's Cython evidence planning on pandas (roadmap item 7,
stage 4).

For every recent commit that changes only Cython function bodies, the same
functions are edited at X (the commit evidence was recorded at, from a
profile=True build) by inserting a no-op statement, X -> X' is planned with
the evidence store, and the plan is checked against the line-traced oracle:
every test whose traced lines fall in an edited function must be selected.

usage: recall.py REPO X STORE RUN_TRACE [--last 500] [--out results.jsonl]

REPO is a pandas checkout whose object database holds X (a worktree is
created in it for the synthetic commits), STORE the evidence store recorded
at X, RUN_TRACE the oracle's per-test records (cyspike.py's monlines mode).
"""

from __future__ import annotations

import argparse
import glob
import json
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


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def cython_at(repo: Path, rev: str, path: str):
    try:
        return read(path, git(repo, "show", f"{rev}:{path}"))
    except subprocess.CalledProcessError:
        return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("repo", type=Path)
    p.add_argument("x")
    p.add_argument("store", type=Path)
    p.add_argument("run_trace", type=Path)
    p.add_argument("--last", type=int, default=500)
    p.add_argument("--out", type=Path, default=Path("cython_recall.jsonl"))
    args = p.parse_args()
    repo = args.repo.resolve()
    x = git(repo, "rev-parse", args.x).strip()
    evidence = load_store(args.store)
    index_x = build_index(read_snapshot(repo, x, ["."]))

    # The oracle: per test, the functions at X its traced lines fall in.
    oracle: dict[tuple[str, str], set[str]] = defaultdict(set)
    for name in glob.glob(str(args.run_trace / "*.jsonl")):
        for line in open(name):
            record = json.loads(line)
            for path, lineno in record["x"]:
                module = index_x.cython.get(path)
                function = module.function_at(lineno) if module else None
                if function is not None:
                    oracle[(path, function.name)].add(record["t"])

    commits = git(repo, "log", "--first-parent", "--format=%H", "-n", str(args.last), x).split()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "synthetic"
        git(repo, "worktree", "add", "--detach", "-q", str(work), x)
        try:
            with args.out.open("w") as out:
                for commit in commits[1:]:  # X itself is not an edit
                    changed = [
                        p
                        for p in git(
                            repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit
                        ).split()
                        if is_cython(p)
                    ]
                    if not changed:
                        continue
                    before = {p: m for p in changed if (m := cython_at(repo, f"{commit}^", p))}
                    after = {p: m for p in changed if (m := cython_at(repo, commit, p))}
                    changes = cython_changes(before, after)
                    if changes.files or not changes.functions:
                        continue  # select-all, or no function-level change: nothing to check
                    at_x = [
                        (p, n)
                        for p, n in changes.functions
                        if p in index_x.cython and n in index_x.cython[p].by_name()
                    ]
                    if not at_x:
                        continue
                    # Edit those functions at X: a no-op as the body's first statement.
                    git(work, "checkout", "-q", "--detach", x)
                    by_path = defaultdict(list)
                    for path, name in at_x:
                        by_path[path].append(index_x.cython[path].by_name()[name])
                    for path, functions in by_path.items():
                        lines = (work / path).read_text().split("\n")
                        for f in sorted(functions, key=lambda f: -f.start):
                            body = next(
                                k
                                for k in range(f.start, f.end)
                                if lines[k].strip()
                                and not lines[k].lstrip().startswith(("#", "@"))
                                and len(lines[k]) - len(lines[k].lstrip())
                                > len(lines[f.start - 1]) - len(lines[f.start - 1].lstrip())
                            )
                            indent = lines[body][: len(lines[body]) - len(lines[body].lstrip())]
                            lines.insert(body, f"{indent}pass  # synthetic edit")
                        (work / path).write_text("\n".join(lines))
                    git(
                        work,
                        "-c",
                        "user.email=s@l",
                        "-c",
                        "user.name=s",
                        "commit",
                        "-qam",
                        f"edit {commit[:10]}",
                    )
                    head = git(work, "rev-parse", "HEAD").strip()
                    t = time.time()
                    planned = plan(
                        repo,
                        x,
                        head,
                        source_roots=["."],
                        discover_runners=["pytest"],
                        cache=IndexCache(default_cache_dir(repo)),
                        evidence=evidence,
                    )
                    selected = {d.target.runner_id for d in planned.selected}
                    expected = set().union(*(oracle.get(fk, set()) for fk in at_x))
                    missed = sorted(expected - selected)
                    row = {
                        "commit": commit[:10],
                        "functions": [f"{p}::{n}" for p, n in at_x],
                        "selected": len(selected),
                        "targets": len(planned.decisions),
                        "oracle": len(expected),
                        "missed": missed[:20],
                        "n_missed": len(missed),
                        "fallbacks": [f.rule for f in planned.fallbacks],
                        "seconds": round(time.time() - t, 1),
                    }
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(
                        f"{commit[:10]}: {len(at_x)} function(s), select {len(selected)}/"
                        f"{len(planned.decisions)}, oracle {len(expected)}, missed {len(missed)}"
                        f"{' ' + str(row['fallbacks']) if row['fallbacks'] else ''}",
                        flush=True,
                    )
        finally:
            git(repo, "worktree", "remove", "--force", str(work))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

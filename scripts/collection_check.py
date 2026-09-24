"""Measure static discovery against what pytest really collects.

Discovery reproduces pytest's documented collection rules without importing
anything (docs/design.md). This script checks that claim the only way it can
be checked: it runs ``pytest --collect-only`` in a repository and diffs the
node ids against the targets ``diffcone discover`` produces for the same
snapshot. It executes project code, so it is a script, not part of planning.

    uv run python scripts/collection_check.py --repo DIR --command CMD \
        [--source-root src --source-root .] [--limit 20]

Exit code 1 when pytest collects a test that is not a target (a recall gap),
0 otherwise. Parameter cases are collapsed: diffcone plans whole test
functions, so ``test_x[1]`` and ``test_x[2]`` are both ``test_x``.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path

from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import Indexer
from diffcone.snapshot import read_snapshot

# ``tests/test_x.py::TestC::test_m[case]`` -> node id without the case.
PARAM = re.compile(r"\[.*\]$")
# The file part of a node id: a path with an extension pytest can collect.
NODE_FILE = re.compile(r"^[\w./-]+\.(py|txt|rst|md)$")


def collected(repo: Path, command: str) -> tuple[set[str], str]:
    """Node ids pytest collects, with parameter cases collapsed."""
    argv = shlex.split(command) + ["--collect-only", "-q", "--no-header", "-p", "no:cacheprovider"]
    proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
    ids = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        # A node id has no whitespace once its parameter case is stripped, and
        # starts with a file path; plugins print other lines containing "::".
        node = PARAM.sub("", line)
        if "::" not in node or any(c.isspace() for c in node):
            continue
        if not NODE_FILE.match(node.split("::")[0]):
            continue
        ids.add(node)
    return ids, proc.stdout + proc.stderr


def targets(repo: Path, source_roots: list[str], rev: str) -> tuple[set[str], list]:
    # with_config: discovery needs the project's pytest configuration, which
    # the planner also reads only for the snapshot it discovers in.
    snapshot = read_snapshot(repo, rev, source_roots=source_roots, with_config=True)
    index = Indexer(snapshot).build()
    result = discover("pytest", snapshot, index, DiscoveryOptions())
    return {PARAM.sub("", t.runner_id) for t in result.targets}, result.notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--command", required=True, help="the pytest command to collect with")
    parser.add_argument("--source-root", action="append", dest="source_roots", default=[])
    parser.add_argument("--rev", default="WORKTREE")
    parser.add_argument("--limit", type=int, default=15, help="example node ids to print")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    roots = args.source_roots or (["src", "."] if (repo / "src").is_dir() else ["."])
    real, log = collected(repo, args.command)
    if not real:
        print(f"collected nothing; is the command right?\n{log[-2000:]}", file=sys.stderr)
        return 2
    planned, notes = targets(repo, roots, args.rev)

    missing = sorted(real - planned)
    extra = sorted(planned - real)
    print(f"collected {len(real)}  targets {len(planned)}")
    print(f"missing {len(missing)} (collected, not a target)   extra {len(extra)}")
    kinds = Counter(n.kind for n in notes)
    print(f"notes: {dict(kinds)}" if kinds else "notes: none")
    for label, ids in (("MISSING", missing), ("EXTRA", extra)):
        for node in ids[: args.limit]:
            print(f"  {label} {node}")
        if len(ids) > args.limit:
            print(f"  ... and {len(ids) - args.limit} more {label}")
    # Files are what a reader can act on: which ones the misses live in.
    if missing:
        files = Counter(node.split("::")[0] for node in missing)
        print("missing by file:")
        for path, n in files.most_common(args.limit):
            print(f"  {n:5d}  {path}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

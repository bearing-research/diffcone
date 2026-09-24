"""Measure static discovery against what pytest really collects.

Discovery reproduces each runner's documented collection rules without
importing anything (docs/design.md). This script checks that claim the only
way it can be checked: it asks the runner what it collects and diffs the
answer against the targets discovery produces for the same snapshot. It
executes project code, so it is a script, not part of planning.

    uv run python scripts/collection_check.py --repo DIR --command CMD \
        [--runner pytest|asv] [--source-root src --source-root .] [--limit 20]

pytest is asked with ``--collect-only``; ASV with ``python -m asv.benchmark
discover``, which is what ASV itself runs to enumerate benchmarks (and needs
``asv`` and the project importable in the environment ``--command`` names).

For ASV it also checks the other half: whether the ``--bench`` pattern
``diffcone run`` builds selects exactly those targets, by replaying ASV's own
filter (asv/benchmarks.py) over the discovered benchmarks. Naming a benchmark
correctly is no use if the pattern that runs it does not match -- an anchored
pattern once matched no parameterised benchmark at all. ``--execute`` goes
one step further and hands the pattern to ASV itself (in the existing
environment, results thrown away), checking that it runs that many
benchmarks: the replay is only as good as its reading of ASV's source.

Exit code 1 when pytest collects a test that is not a target (a recall gap),
0 otherwise. Parameter cases are collapsed: diffcone plans whole test
functions, so ``test_x[1]`` and ``test_x[2]`` are both ``test_x``.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import shlex
import subprocess
import sys
import tempfile
import warnings
from collections import Counter
from pathlib import Path, PurePosixPath

from diffcone.discovery import DiscoveryOptions, discover
from diffcone.discovery.asv_static import read_asv_config
from diffcone.execution import build_command
from diffcone.indexer import Indexer
from diffcone.manifest import Target
from diffcone.snapshot import read_snapshot

# ``tests/test_x.py::TestC::test_m[case]`` -> node id without the case.
# Parsing a project's test *data* can warn (black keeps deliberately invalid
# escapes under tests/data); that is the project's business, not ours.
warnings.filterwarnings("ignore", category=SyntaxWarning)

PARAM = re.compile(r"\[.*\]$")
# The file part of a node id: a path with an extension pytest can collect.
NODE_FILE = re.compile(r"^[\w./-]+\.(py|txt|rst|md)$")


def asv_selection(pattern: str, records: list[dict]) -> set[str]:
    """The benchmarks ASV's ``--bench`` filter selects with ``pattern``, by
    its own rule: a parameterised benchmark is matched as ``name(p0, p1)``,
    one combination at a time, and a plain one by its name."""
    chosen: set[str] = set()
    for record in records:
        name = record.get("name")
        if not name:
            continue
        if record.get("params"):
            for combo in itertools.product(*record["params"]):
                if re.search(pattern, f"{name}({', '.join(combo)})"):
                    chosen.add(name)
                    break
        elif re.search(pattern, name):
            chosen.add(name)
    return chosen


def asv_run_count(repo: Path, command: str, snapshot, pattern: str) -> tuple[int | None, str]:
    """How many benchmarks ASV actually runs with ``pattern``. It runs them in
    the existing environment and throws the results away: this asks whether
    its real filter agrees with the replay above, not how fast anything is.
    ASV elides long names in that output, so the count is what it reports."""
    conf_dir = PurePosixPath(read_asv_config(snapshot)["source"] or ".").parent
    argv = shlex.split(command) + [
        "-m",
        "asv",
        "run",
        "--python=same",
        "--quick",
        "--dry-run",
        "--bench",
        pattern,
    ]
    proc = subprocess.run(argv, cwd=repo / conf_dir, capture_output=True, text=True)
    log = proc.stdout + proc.stderr
    found = re.search(r"Running (\d+) total benchmarks", log)
    return (int(found.group(1)) if found else None), log


def asv_collected(repo: Path, command: str, snapshot) -> tuple[set[str], str, list[dict]]:
    """Benchmark names ASV itself discovers, through its own discover step,
    with the raw records: their parameters are needed to replay its filter."""
    config = read_asv_config(snapshot)
    # ASV discovers from the directory holding asv.conf.json, which is what
    # puts the benchmark package's parent on sys.path (networkx's benchmark
    # modules import ``benchmarks.utils``).
    conf_dir = PurePosixPath(config["source"] or ".").parent
    bench_dir = PurePosixPath(config["benchmark_dir"])
    rel = bench_dir.relative_to(conf_dir) if conf_dir != PurePosixPath(".") else bench_dir
    with tempfile.TemporaryDirectory(prefix="diffcone-asv-") as tmp:
        out = Path(tmp) / "benchmarks.json"
        argv = shlex.split(command) + ["-m", "asv.benchmark", "discover", str(rel), str(out)]
        proc = subprocess.run(argv, cwd=repo / conf_dir, capture_output=True, text=True)
        log = proc.stdout + proc.stderr
        if not out.exists():
            return set(), log, []
        try:
            found = json.loads(out.read_text())
        except json.JSONDecodeError as exc:
            # ASV writes the file even when a benchmark module fails to
            # import; its log says which, so hand that back.
            return set(), f"{exc}\n{log}", []
    records = [b for b in found if isinstance(b, dict) and "name" in b]
    return {PARAM.sub("", b["name"]) for b in records}, log, records


def collected(repo: Path, command: str, clean_addopts: bool = False) -> tuple[set[str], str]:
    """Node ids pytest collects, with parameter cases collapsed. Verbosity is
    the fiddly part: ``--collect-only -q`` lists node ids, but a project whose
    own ``addopts`` carry ``-v`` gets the tree format instead, and there one
    more ``-q`` is what lists them (while on a quiet project that same second
    ``-q`` prints per-file counts). So: try one, and if nothing that looks
    like a node id comes back, try two."""
    extra = ["-o", "addopts="] if clean_addopts else []
    log = ""
    for quiet in (["-q"], ["-q", "-q"]):
        argv = shlex.split(command) + ["--collect-only", "--no-header", *quiet, *extra]
        proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
        log = proc.stdout + proc.stderr
        ids = set()
        for line in proc.stdout.splitlines():
            # A node id has no whitespace once its parameter case is stripped,
            # and starts with a file path; plugins print other lines with "::".
            node = PARAM.sub("", line.strip())
            if "::" not in node or any(c.isspace() for c in node):
                continue
            if not NODE_FILE.match(node.split("::")[0]):
                continue
            ids.add(node)
        if ids:
            return ids, log
    return set(), log


def targets(repo: Path, source_roots: list[str], rev: str, runner: str):
    # with_config: discovery needs the project's runner configuration, which
    # the planner also reads only for the snapshot it discovers in.
    snapshot = read_snapshot(repo, rev, source_roots=source_roots, with_config=True)
    index = Indexer(snapshot).build()
    result = discover(runner, snapshot, index, DiscoveryOptions())
    return {PARAM.sub("", t.runner_id) for t in result.targets}, result.notes, snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--command",
        required=True,
        help="pytest: the command to collect with; asv: the python to discover with",
    )
    parser.add_argument("--runner", choices=("pytest", "asv"), default="pytest")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="asv only: also run ASV with the pattern and check it runs that many benchmarks",
    )
    parser.add_argument(
        "--clean-addopts",
        action="store_true",
        help="collect with -o addopts= (for a project whose addopts print over the listing)",
    )
    parser.add_argument("--source-root", action="append", dest="source_roots", default=[])
    parser.add_argument("--rev", default="WORKTREE")
    parser.add_argument("--limit", type=int, default=15, help="example node ids to print")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    roots = args.source_roots or (["src", "."] if (repo / "src").is_dir() else ["."])
    planned, notes, snapshot = targets(repo, roots, args.rev, args.runner)
    records: list[dict] = []
    if args.runner == "pytest":
        real, log = collected(repo, args.command, args.clean_addopts)
    else:
        real, log, records = asv_collected(repo, args.command, snapshot)
    if not real:
        print(f"collected nothing; is the command right?\n{log[-2000:]}", file=sys.stderr)
        return 2

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
    if args.runner == "asv" and planned:
        # The other half: does the pattern ``run`` would pass select them?
        argv = build_command("asv", [Target("asv", t, t, ()) for t in sorted(planned)], None, [])
        chosen = asv_selection(argv[-1], records)
        known = planned & real
        print(f"\n--bench pattern selects {len(chosen)} of the {len(known)} target(s) ASV knows")
        for label, ids in (
            ("PATTERN MISSES", sorted(known - chosen)),
            ("PATTERN OVER-SELECTS", sorted(chosen - planned)),
        ):
            for node in ids[: args.limit]:
                print(f"  {label} {node}")
            if len(ids) > args.limit:
                print(f"  ... and {len(ids) - args.limit} more {label}")
        if known - chosen:
            return 1
        if args.execute:
            ran, log = asv_run_count(repo, args.command, snapshot, argv[-1])
            print(f"asv itself runs {ran if ran is not None else '?'} benchmark(s) with it")
            if ran != len(chosen):
                print(f"  ASV RAN A DIFFERENT NUMBER\n{log[-1500:]}", file=sys.stderr)
                return 1
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Which selection causes are worth their cost?

The census attributes every selected target to one cause (a resolved
dependency, a dynamic reference, a name match, or either of the last two).
This script adds the missing half: whether the target was worth selecting,
from the same per-test coverage `validate --coverage` uses. For each cause it
reports how many selected tests actually executed a changed symbol.

    uv run python scripts/cause_precision.py --repo DIR --command CMD \
        --commit SHA [--commit SHA ...] [--source-root src --source-root .]

It runs the project's whole suite under coverage, so it is a script, not part
of planning. A cause whose tests almost never execute a changed symbol is
paying for nothing; one whose tests usually do is earning its selections.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import Counter
from pathlib import Path

from diffcone.execution import validate_pytest
from diffcone.planner import plan as build_plan

SCRIPTS = Path(__file__).resolve().parent


def _census():
    """The census's cause attribution, without running the census."""
    spec = importlib.util.spec_from_file_location("census", SCRIPTS / "census.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["census"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--commit", action="append", dest="commits", required=True)
    parser.add_argument("--source-root", action="append", dest="source_roots", default=[])
    parser.add_argument("--setup-command")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    roots = args.source_roots or (["src", "."] if (repo / "src").is_dir() else ["."])
    causes_of = _census()._causes

    totals: Counter = Counter()
    worth: Counter = Counter()
    for sha in args.commits:
        p = build_plan(repo, f"{sha}~1", sha, source_roots=roots, discover_runners=["pytest"])
        causes = {rid: cause for rid, (cause, _) in causes_of(p).items()}
        validation = validate_pytest(
            p,
            repo=repo,
            command=args.command,
            coverage=True,
            setup_command=args.setup_command,
        )
        if validation.coverage is None:
            print(f"{sha[:10]}: no coverage", file=sys.stderr)
            continue
        executed = {h.runner_id for h in validation.coverage.hits if h.executed_changed}
        seen = {h.runner_id for h in validation.coverage.hits}
        for rid, cause in causes.items():
            if rid not in seen:  # never ran (skipped, or not collected here)
                continue
            totals[cause] += 1
            if rid in executed:
                worth[cause] += 1
        print(
            f"{sha[:10]}: {len(causes)} selected, {len(executed)} executed a changed symbol",
            flush=True,
        )

    print("\ncause                     selected  executed a change  share")
    for cause, n in totals.most_common():
        good = worth[cause]
        print(f"{cause:24s}  {n:8d}  {good:17d}  {100 * good / n:4.0f} %")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

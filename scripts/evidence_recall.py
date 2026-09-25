"""Recall of evidence plans on a repository that must be built in place (pandas).

``diffcone validate`` checks each snapshot out into a fresh worktree, which a
compiled project would have to build from scratch every time. This walks a
first-parent range *in place* instead: each commit is checked out in the
repository, the project's editable install rebuilds what changed on import,
and the whole suite runs once under per-test coverage. Every ``--every``
commits (the first included) evidence is also recorded (``diffcone
collect``). Each later commit c is planned from the nearest recorded commit
A before it (``plan A -> c`` with that evidence) and checked the way
``validate --coverage`` checks a plan:

* every test whose outcome differs between A and c must be selected;
* every test that executed a changed symbol, at c (coverage of c) or at A
  (coverage of A, for deleted symbols), must be selected.

Each commit's coverage run is reused as the base run of the pairs after it.

Usage:
  uv run python scripts/evidence_recall.py --repo DIR --command CMD --out DIR \\
      --range A..B [--every 6] [--setup CMD]

``--command`` is the full pytest command line including its arguments
(``python -m pytest -n 8 -m 'not slow' pandas``). The repository must be
clean; it is left at B. Results: one JSON line per commit in
``OUT/recall.jsonl``, coverage databases and logs beside it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from diffcone.cache import IndexCache, default_cache_dir
from diffcone.evidence import load_store
from diffcone.execution import (
    _run_full_pytest,
    _SuiteRun,
    collect_evidence,
    coverage_validation,
    fold_nodeid,
    merge_coverage,
)
from diffcone.planner import plan


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True, type=Path)
    p.add_argument("--command", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--range", required=True, dest="revision_range")
    p.add_argument("--every", type=int, default=6)
    p.add_argument("--setup", help="shell command run after each checkout (a build step)")
    p.add_argument("--source-root", action="append", dest="roots")
    args = p.parse_args()
    repo, out = args.repo.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = args.roots or ["."]
    cache = IndexCache(default_cache_dir(repo))
    start, end = args.revision_range.split("..")
    commits = git(repo, "rev-list", "--first-parent", "--reverse", f"{start}..{end}").split()
    commits = [git(repo, "rev-parse", start)] + commits
    # Coverage measures only the Python files the range changes: the oracle
    # asks which tests executed a changed symbol, and per-test contexts over
    # all of pandas fill a disk.
    changed_files = sorted(
        set(git(repo, "log", "--format=", "--name-only", f"{start}..{end}", "--", "*.py").split())
    )
    print(f"{len(commits)} commits, {len(changed_files)} changed Python files", flush=True)
    results = out / "recall.jsonl"
    done = set()
    if results.exists():
        done = {json.loads(line)["commit"] for line in results.read_text().splitlines()}
    runs: dict[str, tuple[dict[str, str], Path]] = {}  # commit -> (outcomes, coverage db)
    stores: dict[str, Path] = {}
    anchor = None
    for i, commit in enumerate(commits):
        short = commit[:10]
        is_anchor = i % args.every == 0
        cov_db = out / f"cov-{short}.db"
        outcomes_file = out / f"outcomes-{short}.json"
        if commit in done and not is_anchor:
            print(f"{short} done", flush=True)
            continue
        git(repo, "checkout", "-q", "--detach", commit)
        if args.setup:
            t = time.time()
            subprocess.run(args.setup, shell=True, cwd=repo, check=True, capture_output=True)
            print(f"{short} setup {time.time() - t:.0f}s", flush=True)
        if outcomes_file.exists() and cov_db.exists():
            outcomes = json.loads(outcomes_file.read_text())
        else:
            t = time.time()
            with _run_full_pytest(
                repo,
                args.command,
                coverage=True,
                source_roots=roots,
                coverage_include=changed_files,
            ) as run:
                outcomes = run.outcomes
                shutil.copy(run.coverage_db, cov_db)
                (out / f"log-{short}.txt").write_text(run.log[-200_000:])
            outcomes_file.write_text(json.dumps(outcomes))
            print(f"{short} coverage run {time.time() - t:.0f}s, {len(outcomes)} tests", flush=True)
        runs[commit] = (outcomes, cov_db)
        if is_anchor:
            store = sorted((repo / ".diffcone" / "evidence").glob(f"{commit}-*.sqlite"))
            if store:
                stores[commit] = store[-1]
            else:
                t = time.time()
                argv = args.command.split(" -m pytest", 1)
                collected = collect_evidence(
                    repo,
                    command=argv[0] + " -m pytest",
                    source_roots=roots,
                    extra=_split(argv[1]) if len(argv) > 1 else [],
                    cache=cache,
                )
                stores[commit] = collected.store
                print(
                    f"{short} collected {len(collected.evidence.tests)} tests in "
                    f"{time.time() - t:.0f}s",
                    flush=True,
                )
            anchor = commit
            if i == 0:
                continue
        if anchor == commit or anchor is None:
            continue
        if commit in done:
            continue
        t = time.time()
        evidence = load_store(stores[anchor])
        planned = plan(
            repo,
            anchor,
            commit,
            source_roots=roots,
            discover_runners=["pytest"],
            cache=cache,
            evidence=evidence,
        )
        plan_seconds = time.time() - t
        static = plan(
            repo, anchor, commit, source_roots=roots, discover_runners=["pytest"], cache=cache
        )
        selected = {d.target.runner_id for d in planned.selected}
        base_outcomes, base_db = runs[anchor]
        head_outcomes, head_db = runs[commit]
        known = {d.target.runner_id for d in planned.decisions}
        changed = sorted(
            n
            for n in set(base_outcomes) | set(head_outcomes)
            if base_outcomes.get(n) != head_outcomes.get(n)
        )
        outcome_missed = [n for n in changed if n in known and n not in selected]
        unknown_changed = [n for n in changed if n not in known]
        head_cov = coverage_validation(
            planned, _SuiteRun(head_outcomes, "", 0, head_db), repo, selected
        )
        base_cov = coverage_validation(
            planned, _SuiteRun(base_outcomes, "", 0, base_db), repo, selected, side="base"
        )
        cov = merge_coverage(head_cov, base_cov, {fold_nodeid(n) for n in head_outcomes})
        row = {
            "commit": commit,
            "anchor": anchor,
            "distance": i - commits.index(anchor),
            "subject": git(repo, "log", "-1", "--format=%s", commit),
            "targets": len(planned.decisions),
            "selected": len(selected),
            "static_selected": len(static.selected),
            "changes": len(planned.changes),
            "outcome_changed": len(changed),
            "outcome_missed": outcome_missed,
            "outcome_changed_unknown_target": unknown_changed[:20],
            "coverage_affected": len(cov.affected) if cov else None,
            "coverage_missed": sorted(h.runner_id for h in cov.missed)[:50] if cov else None,
            "rules": _rule_counts(planned),
            "escalated_modules": planned.evidence["escalated_modules"][:20],
            "fallbacks": [f"{f.rule}: {f.detail[:160]}" for f in planned.fallbacks][:5],
            "plan_seconds": round(plan_seconds, 1),
        }
        with results.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(
            f"{short} vs {anchor[:10]} (+{row['distance']}): select {row['selected']}/"
            f"{row['targets']} (static {row['static_selected']}), outcome changes "
            f"{len(changed)} missed {len(outcome_missed)}, coverage affected "
            f"{row['coverage_affected']} missed {len(cov.missed) if cov else '-'}",
            flush=True,
        )
    return 0


def _split(text: str) -> list[str]:
    import shlex

    return shlex.split(text)


def _rule_counts(planned) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in planned.selected:
        for rule in {r.rule for r in d.reasons}:
            counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    sys.exit(main())

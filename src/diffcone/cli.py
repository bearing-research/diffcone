"""Command-line interface.

Exit codes (plan / discover):
  0  plan produced, analysis complete
  1  plan produced, but analysis errors forced a conservative fallback
  2  no plan (bad arguments, unreadable manifest, unknown revision)

``run`` exits with the runner's exit code (0 when nothing was selected or
with --dry-run); ``validate`` exits 0 when every outcome change was selected,
1 when some were missed, 2 on errors.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from diffcone.cache import IndexCache, default_cache_dir
from diffcone.discovery import RUNNERS, DiscoveryOptions, discover
from diffcone.execution import (
    corpus_to_dict,
    corpus_to_text,
    corpus_validation,
    run_selected,
    validate_pytest,
    validation_to_dict,
    validation_to_text,
)
from diffcone.indexer import build_index
from diffcone.manifest import ManifestError, load_manifest, manifest_to_dict
from diffcone.planner import plan
from diffcone.report import snapshot_to_dict, to_json, to_text
from diffcone.snapshot import GitError, read_snapshot


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument(
        "--source-root",
        action="append",
        dest="source_roots",
        metavar="DIR",
        help="repo-relative directory whose .py files are analyzed as a module tree "
        "(repeatable; overrides the manifest's source_roots; default: .)",
    )
    p.add_argument(
        "--discover",
        action="append",
        dest="discover",
        choices=RUNNERS,
        metavar="RUNNER",
        help=f"statically discover targets for a runner ({', '.join(RUNNERS)}); repeatable",
    )
    p.add_argument(
        "--assume-external-fixture",
        action="append",
        dest="external_fixtures",
        metavar="NAME",
        default=[],
        help="pytest fixture provided by an installed plugin; not reported as unresolved "
        "(fixtures of well-known plugins such as pytest-mock's mocker are assumed by default)",
    )
    p.add_argument(
        "--no-well-known-fixtures",
        action="store_true",
        help="do not assume fixtures of well-known pytest plugins; report them as unresolved",
    )
    p.add_argument("--output", "-o", help="write the result to this file instead of stdout")
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the cache (<repo>/.diffcone/cache): whole indexes of "
        "committed snapshots and per-module results, which also serve WORKTREE and INDEX",
    )
    p.add_argument("--cache-dir", help="where to keep the cache (default: <repo>/.diffcone/cache)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffcone",
        description="Static-first, function-level change-impact planning for Python.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "plan",
        help="produce a selection plan between two snapshots (revisions, INDEX or WORKTREE)",
        description=(
            "Compare two snapshots and report which targets are affected. A snapshot is a "
            "git revision, INDEX (staged content) or WORKTREE (files on disk). The report "
            "states exactly which kind was read. Targets come from --targets, from "
            "--discover, or both. Project code is never executed."
        ),
    )
    p.add_argument("--base", required=True, help="base snapshot: a git revision, INDEX or WORKTREE")
    p.add_argument(
        "--head",
        required=True,
        help="head snapshot: a git revision, INDEX (staged content) or WORKTREE (files on "
        "disk, ignored files excluded)",
    )
    p.add_argument("--targets", help="path to a JSON target manifest")
    _add_common(p)
    p.add_argument("--format", choices=("json", "text"), default="json")

    r = sub.add_parser(
        "run",
        help="plan, then execute only the selected targets with the runner's CLI",
        description=(
            "Build a plan exactly like `plan`, then invoke the runner on the selected "
            "targets (pytest node ids, or an asv --bench pattern). Nothing is executed "
            "during analysis. Arguments after `--` are passed to the runner."
        ),
    )
    r.add_argument("--base", required=True, help="base snapshot: a git revision, INDEX or WORKTREE")
    r.add_argument("--head", required=True, help="head snapshot: a git revision, INDEX or WORKTREE")
    r.add_argument("--targets", help="path to a JSON target manifest")
    r.add_argument("--runner", choices=RUNNERS, default="pytest", help="which runner to execute")
    r.add_argument(
        "--command",
        dest="runner_command",
        help='runner command line (default: "python -m pytest" or "asv run"); run in --repo',
    )
    r.add_argument("--dry-run", action="store_true", help="print the command instead of running")
    _add_common(r)
    r.add_argument("runner_args", nargs="*", help="extra runner arguments (after --)")

    v = sub.add_parser(
        "validate",
        help="run the full pytest suite at both snapshots and check the plan against it",
        description=(
            "Outcome-based validation: runs the whole pytest suite at base and head "
            "(commits in temporary git worktrees, WORKTREE in place), then reports every "
            "test whose pass/fail outcome changed but was not selected. Behaviour changes "
            "that keep the same outcome are invisible to this check."
        ),
    )
    v.add_argument("--base", required=True, help="base snapshot: a git revision or WORKTREE")
    v.add_argument("--head", required=True, help="head snapshot: a git revision or WORKTREE")
    v.add_argument("--targets", help="path to a JSON target manifest")
    v.add_argument(
        "--command",
        dest="runner_command",
        help='pytest command line (default: "python -m pytest")',
    )
    v.add_argument(
        "--coverage",
        action="store_true",
        help="also run the head suite under pytest-cov with per-test contexts and require "
        "every test that executed a changed symbol to be selected (reports recall/precision)",
    )
    v.add_argument(
        "--setup-command",
        help="shell command run inside each temporary checkout before its suite (recreate "
        "build-generated files such as a setuptools-scm _version.py)",
    )
    _add_common(v)
    v.add_argument("--format", choices=("json", "text"), default="text")

    c = sub.add_parser(
        "corpus",
        help="validate the plan for every commit in a range and aggregate recall/precision",
        description=(
            "Replay history: for each commit in A..B (first-parent order) plan parent -> "
            "commit and validate it like `validate`, then aggregate outcome misses, "
            "coverage recall/precision and selection savings. Each commit's suite runs "
            "once; coverage runs are per pair. Commits touching no .py file are skipped "
            "unless --all-commits is given."
        ),
    )
    c.add_argument(
        "--range", required=True, dest="revision_range", help="git range, e.g. main~20..main"
    )
    c.add_argument("--targets", help="path to a JSON target manifest")
    c.add_argument(
        "--command",
        dest="runner_command",
        help='pytest command line (default: "python -m pytest")',
    )
    c.add_argument("--coverage", action="store_true", help="also measure coverage recall/precision")
    c.add_argument(
        "--setup-command",
        help="shell command run inside each temporary checkout before its suite",
    )
    c.add_argument(
        "--all-commits", action="store_true", help="validate commits without .py changes too"
    )
    c.add_argument(
        "--max", type=int, dest="max_commits", help="only the last N commits of the range"
    )
    c.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="validate this many pairs in parallel, each in its own temporary worktrees "
        "(default 1; suites that write to shared locations can interfere)",
    )
    _add_common(c)
    c.add_argument("--format", choices=("json", "text"), default="text")

    d = sub.add_parser(
        "discover",
        help="statically discover targets in a snapshot and emit a manifest",
        description=(
            "Discover pytest tests and/or ASV benchmarks in a snapshot (a git revision, INDEX "
            "or WORKTREE) without importing them, and print a target manifest (JSON) for "
            "`diffcone plan --targets`. The output states which snapshot kind was read."
        ),
    )
    d.add_argument(
        "--rev", default="HEAD", help="snapshot to discover in: revision, INDEX or WORKTREE"
    )
    _add_common(d)
    return parser


def _write(text: str, output: str | None) -> int:
    if output:
        try:
            Path(output).write_text(text, "utf-8")
        except OSError as exc:
            print(f"diffcone: error: cannot write {output}: {exc}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    options = DiscoveryOptions(
        external_fixtures=frozenset(args.external_fixtures),
        well_known_fixtures=not args.no_well_known_fixtures,
    )

    cache = None
    if not args.no_cache:
        cache = IndexCache(
            Path(args.cache_dir) if args.cache_dir else default_cache_dir(Path(args.repo))
        )

    def build_plan():
        if not args.targets and not args.discover:
            parser.error(f"{args.command} requires --targets and/or --discover")
        manifest = load_manifest(args.targets) if args.targets else None
        return plan(
            Path(args.repo),
            args.base,
            args.head,
            manifest,
            source_roots=args.source_roots,
            discover_runners=args.discover or (),
            discovery_options=options,
            cache=cache,
        )

    try:
        if args.command == "plan":
            result = build_plan()
            text = to_json(result) if args.format == "json" else to_text(result)
            code = _write(text, args.output)
            return code if code else (1 if result.degraded else 0)
        if args.command == "run":
            result = build_plan()
            outcome = run_selected(
                result,
                args.runner,
                cwd=Path(args.repo),
                command=args.runner_command,
                extra=args.runner_args,
                dry_run=args.dry_run,
            )
            status = "degraded" if result.degraded else "complete"
            print(
                f"diffcone: {len(outcome.selected)} of {outcome.total} {args.runner} target(s) "
                f"selected (plan {status})",
                file=sys.stderr,
            )
            if not outcome.selected:
                print("diffcone: nothing selected; not running", file=sys.stderr)
                return 0
            if args.dry_run:
                return _write(" ".join(shlex.quote(a) for a in outcome.command) + "\n", args.output)
            return outcome.returncode or 0
        if args.command == "validate":
            result = build_plan()
            validation = validate_pytest(
                result,
                repo=Path(args.repo),
                command=args.runner_command,
                coverage=args.coverage,
                setup_command=args.setup_command,
            )
            text = (
                json.dumps(validation_to_dict(validation), indent=2) + "\n"
                if args.format == "json"
                else validation_to_text(validation)
            )
            code = _write(text, args.output)
            return code if code else (0 if validation.ok else 1)
        if args.command == "corpus":
            if not args.targets and not args.discover:
                parser.error("corpus requires --targets and/or --discover")
            manifest = load_manifest(args.targets) if args.targets else None

            def make_plan(base: str, head: str):
                return plan(
                    Path(args.repo),
                    base,
                    head,
                    manifest,
                    source_roots=args.source_roots,
                    discover_runners=args.discover or (),
                    discovery_options=options,
                    cache=cache,
                )

            def progress(entry) -> None:
                verb = "skipping" if entry.skipped else "validating"
                print(f"diffcone: {verb} {entry.commit[:10]} {entry.subject}", file=sys.stderr)

            report = corpus_validation(
                Path(args.repo),
                args.revision_range,
                make_plan,
                command=args.runner_command,
                coverage=args.coverage,
                only_python_changes=not args.all_commits,
                max_commits=args.max_commits,
                progress=progress,
                setup_command=args.setup_command,
                jobs=max(1, args.jobs),
            )
            text = (
                json.dumps(corpus_to_dict(report), indent=2) + "\n"
                if args.format == "json"
                else corpus_to_text(report)
            )
            code = _write(text, args.output)
            return code if code else (0 if report.ok else 1)
        if args.command == "discover":
            runners = args.discover or list(RUNNERS)
            roots = args.source_roots or ["."]
            snapshot = read_snapshot(Path(args.repo), args.rev, roots, with_config=True)
            index = build_index(snapshot)
            results = [discover(r, snapshot, index, options) for r in runners]
            data = manifest_to_dict([t for r in results for t in r.targets], roots)
            data["discovery"] = {
                "snapshot": snapshot_to_dict(snapshot.info),
                "revision": snapshot.revision,
                "commit": snapshot.commit,
                "runners": [
                    {
                        "runner": r.runner,
                        "targets": len(r.targets),
                        "config": r.config,
                        "notes": [{"kind": n.kind, "detail": n.detail} for n in r.notes],
                    }
                    for r in results
                ],
            }
            return _write(json.dumps(data, indent=2) + "\n", args.output)
    except (ManifestError, GitError) as exc:
        print(f"diffcone: error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

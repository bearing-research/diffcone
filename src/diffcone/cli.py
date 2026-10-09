"""Command-line interface.

Exit codes (plan / discover):
  0  plan produced, analysis complete
  1  plan produced, but analysis errors forced a conservative fallback
  2  no plan (bad arguments, unreadable manifest, unknown revision, a crash)
  3  plan produced, but discovery may be short of what the runner collects

A manifest written by ``discover`` keeps its notes, so a plan made from it
exits 3 as a plan that discovered the targets itself would.

1 and 3 are opposite failures: 1 means too much was selected (the analysis
gave up safely), 3 means the target list itself may be incomplete, so running
only the selected targets would skip tests. 3 wins when both apply.

``run`` refuses to execute an incomplete plan (exit 3) unless
--allow-incomplete-discovery is given, and refuses (exit 2) when the working
tree it would run differs, under the source roots, from the snapshot the plan
analysed, unless --allow-mismatched-worktree is given; otherwise it exits with
the runner's exit code (0 when nothing was selected or with --dry-run; pytest's
5 counts as 0), or 3 when a selected target was not collected. ``validate`` exits
0 when every outcome change was selected, 1 when some were missed, 2 on
errors. ``check`` exits 0 when every new failure of the full run was
selected, 1 when the plan missed one, 2 when an input cannot be read.
``report`` exits 0 when the CI runs it reads have no miss and no failed check,
1 otherwise, 2 when the directory cannot be read.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import traceback
from pathlib import Path

from diffcone import check as checking
from diffcone import ci_report
from diffcone.cache import IndexCache, default_cache_dir
from diffcone.discovery import RUNNERS, DiscoveryOptions, discover
from diffcone.evidence import (
    FLAG_SUBPROCESS,
    FLAG_UNSTABLE,
    EvidenceError,
    environment_differences,
    find_store,
    list_stores,
    load_store,
)
from diffcone.execution import (
    advance_refusal,
    collect_evidence,
    corpus_to_dict,
    corpus_to_text,
    corpus_validation,
    run_selected,
    run_with_evidence,
    validate_pytest,
    validation_to_dict,
    validation_to_text,
    worktree_mismatch,
)
from diffcone.indexer import build_index
from diffcone.manifest import ManifestError, load_manifest, manifest_to_dict
from diffcone.planner import plan
from diffcone.report import snapshot_to_dict, to_json, to_text
from diffcone.snapshot import GitError, read_snapshot, resolve_commit


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument(
        "--source-root",
        action="append",
        dest="source_roots",
        metavar="DIR[=PREFIX]",
        help="repo-relative directory whose .py files are analyzed as a module tree "
        "(repeatable; overrides the manifest's source_roots; default: .). DIR=PREFIX names "
        "its modules PREFIX.<path>, for per-package test trees whose files share names",
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
    p.add_argument(
        "--output",
        "-o",
        help="write the result to this file instead of stdout (run: the command with "
        "--dry-run, otherwise the plan that ran, as plan writes it in JSON)",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the cache (<repo>/.diffcone/cache): whole indexes of "
        "committed snapshots and per-module results, which also serve WORKTREE and INDEX",
    )
    p.add_argument("--cache-dir", help="where to keep the cache (default: <repo>/.diffcone/cache)")


def _add_evidence(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--evidence",
        metavar="auto|PATH",
        help="opt-in execution evidence (see `diffcone collect`): select pytest targets on "
        "what each test executed when recorded. auto picks the recording at the nearest "
        "ancestor commit; other runners' targets are planned statically",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffcone",
        description="Static-first, function-level change-impact planning for Python.",
    )
    from diffcone import __version__

    parser.add_argument("--version", action="version", version=f"diffcone {__version__}")
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
    _add_evidence(p)
    p.add_argument("--format", choices=("json", "text"), default="json")
    p.add_argument(
        "runner_args",
        nargs="*",
        help="pytest arguments the run will use (after --): options that change what pytest "
        "collects, such as --doctest-modules, are read by discovery",
    )

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
        metavar="COMMAND",
        help=(
            'runner command line (default: "python -m pytest", or "asv run --python=same", '
            "which benchmarks the checkout in the current environment); run in --repo"
        ),
    )
    r.add_argument("--dry-run", action="store_true", help="print the command instead of running")
    r.add_argument(
        "--allow-mismatched-worktree",
        action="store_true",
        help="run even when the checkout is not the snapshot the plan analysed",
    )
    r.add_argument(
        "--allow-incomplete-discovery",
        action="store_true",
        help="run even when discovery reports tests the runner may collect that are not targets",
    )
    _add_common(r)
    _add_evidence(r)
    r.add_argument(
        "--collect",
        action="store_true",
        help="with --evidence: record the selected tests too and advance the recording to head "
        "(their new records, the old ones for every other test); needs a clean checkout of "
        "head and the pytest arguments the recording was made with",
    )
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
        metavar="COMMAND",
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
    _add_evidence(v)
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
        metavar="COMMAND",
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
        "--evidence",
        metavar="auto|PATH",
        help="plan every pair with execution evidence: a fixed recording, or auto for the "
        "recording at the nearest ancestor of each commit",
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

    e = sub.add_parser(
        "collect",
        help="run the whole pytest suite under the evidence recorder and store what each "
        "test executed",
        description=(
            "Execution evidence (opt-in): run the whole suite once with diffcone's recorder "
            "(Python 3.12+) and write .diffcone/evidence/<commit>-<environment>.sqlite: the "
            "symbols each test executed and the repository files it touched, at a commit. "
            "Without --rev the repository itself runs and must be clean. Arguments after "
            "`--` are passed to pytest."
        ),
    )
    e.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    e.add_argument(
        "--source-root",
        action="append",
        dest="source_roots",
        metavar="DIR[=PREFIX]",
        help="as for plan (repeatable; default: .)",
    )
    e.add_argument(
        "--rev", help="collect at this commit, in a temporary worktree (default: the clean HEAD)"
    )
    e.add_argument(
        "--command",
        dest="runner_command",
        metavar="COMMAND",
        help='pytest command line (default: "python -m pytest")',
    )
    e.add_argument(
        "--setup-command", help="shell command run inside the temporary checkout (with --rev)"
    )
    e.add_argument(
        "--reverse-check",
        action="store_true",
        help="run the suite a second time in reverse order; tests whose records differ are "
        "marked unstable and always selected",
    )
    e.add_argument(
        "--env-var",
        action="append",
        dest="env_variables",
        default=[],
        metavar="NAME",
        help="an environment variable that changes what tests do (a feature flag, say): "
        "recorded with the environment, and a run where it differs uses no evidence "
        "(repeatable)",
    )
    e.add_argument("--no-cache", action="store_true", help="do not use the index cache")
    e.add_argument("--cache-dir", help="where to keep the cache (default: <repo>/.diffcone/cache)")
    e.add_argument("runner_args", nargs="*", help="extra pytest arguments (after --)")

    k = sub.add_parser(
        "check",
        help="check a plan against a full run's JUnit XML: was every failing test selected?",
        description=(
            "Read a plan (diffcone plan --format json) and the JUnit XML of a full pytest "
            "run, and report every test that failed or errored there but was not selected. "
            "A failure the --baseline run also had is reported as already failing. Each "
            "--run is a selective run (diffcone's own, or another selector's such as "
            "pytest-testmon) compared on the same failures. Runs nothing."
        ),
    )
    k.add_argument("--plan", required=True, help="the plan, as JSON")
    k.add_argument(
        "--full",
        required=True,
        action="append",
        metavar="JUNIT",
        help="JUnit XML of the full run (repeatable: several files are one run)",
    )
    k.add_argument(
        "--baseline",
        action="append",
        metavar="JUNIT",
        help="JUnit XML of a run without the change (e.g. the nightly run at the evidence "
        "commit): its failures are not misses",
    )
    k.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="NAME=JUNIT",
        help="JUnit XML of a selective run to compare (repeatable)",
    )
    k.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    k.add_argument("--output", "-o", help="write the result to this file instead of stdout")

    rp = sub.add_parser(
        "report",
        help="report on many CI runs of the diffcone actions: selection, checks, misses",
        description=(
            "Read the results the run and record actions uploaded, downloaded into one "
            "directory per workflow run (DIR/<run id>/run.json and DIR/<run id>/<artifact>/), "
            "and report per job: the share pull requests selected and why some were planned "
            "from the code, and for pushes whether each was checked, its new failures, flaky "
            "re-runs and confirmed misses. The report action downloads the directory. Runs "
            "nothing."
        ),
    )
    rp.add_argument("--dir", required=True, help="the directory of downloaded runs")
    rp.add_argument("--format", choices=("markdown", "json"), default="markdown")
    rp.add_argument("--output", "-o", help="write the report to this file instead of stdout")

    pr = sub.add_parser(
        "prune",
        help="shrink the cache to what planning at given commits reads",
        description=(
            "Delete every cached index and discovery result except those of the --keep "
            "commits, and every per-module record except those of their files (which also "
            "serve later commits and the working tree sharing them). For a cache shipped "
            "between CI runs: keep the commit the evidence was recorded at."
        ),
    )
    pr.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    pr.add_argument(
        "--keep", action="append", required=True, metavar="REV", help="commit to keep (repeatable)"
    )
    pr.add_argument(
        "--source-root",
        action="append",
        dest="source_roots",
        metavar="DIR[=PREFIX]",
        help="as for plan (repeatable; default: .)",
    )
    pr.add_argument("--cache-dir", help="the cache (default: <repo>/.diffcone/cache)")

    ls = sub.add_parser("evidence", help="list the evidence recordings of a repository")
    ls.add_argument("--repo", default=".", help="path to the git repository (default: .)")

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


def _prune(args: argparse.Namespace) -> int:
    from diffcone.cache import ModuleCache, prune
    from diffcone.snapshot import module_name_for

    repo = Path(args.repo)
    roots = args.source_roots or ["."]
    try:
        commits, keys = set(), set()
        for revision in args.keep:
            commit = resolve_commit(repo, revision)
            commits.add(commit)
            snapshot = read_snapshot(repo, commit, roots)
            for path, content in snapshot.files.items():
                module = module_name_for(path, snapshot.source_roots)
                if module is not None:
                    keys.add(ModuleCache.key(module, path, content))
    except GitError as exc:
        print(f"diffcone: error: {exc}", file=sys.stderr)
        return 2
    directory = Path(args.cache_dir) if args.cache_dir else default_cache_dir(repo)
    result = prune(directory, commits, roots, keys)
    mb = 1024 * 1024
    print(
        f"diffcone: kept {result.files_kept} cached file(s) and {result.rows_kept} module "
        f"record(s), removed {result.files_removed} and {result.rows_removed}; "
        f"{result.bytes_before / mb:.1f} MB -> {result.bytes_after / mb:.1f} MB",
        file=sys.stderr,
    )
    return 0


def _check(args: argparse.Namespace) -> int:
    def cases(paths: list[str]) -> list[checking.Case]:
        return [case for path in paths for case in checking.read_junit(path)]

    try:
        runs = {}
        for spec in args.run:
            name, sep, path = spec.partition("=")
            if not sep or not name or not path:
                raise checking.CheckError(f"--run takes NAME=JUNIT, not {spec!r}")
            runs[name] = cases([path])
        report = checking.check(
            checking.load_plan(args.plan),
            cases(args.full),
            baseline=cases(args.baseline) if args.baseline else None,
            runs=runs,
        )
    except checking.CheckError as exc:
        print(f"diffcone: error: {exc}", file=sys.stderr)
        return 2
    render = {
        "text": checking.to_text,
        "markdown": checking.to_markdown,
        "json": lambda r: json.dumps(checking.to_dict(r), indent=2) + "\n",
    }[args.format]
    code = _write(render(report), args.output)
    return code if code else (0 if report.ok else 1)


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


def _list_evidence(repo: Path) -> int:
    stores = list_stores(repo)
    if not stores:
        print("no evidence recordings (diffcone collect writes one)")
        return 0
    for store in stores:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(store.created))
        print(
            f"{store.commit[:12]}  env {store.environment_hash}  python {store.python}  "
            f"{store.tests} tests  roots {','.join(store.source_roots)}  {when}  {store.path}"
        )
        if store.advanced_from:
            print(
                f"    advanced from {store.advanced_from[:12]}; last full collection "
                f"{(store.full_commit or '')[:12]}"
            )
    return 0


def _collect(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    cache = None
    if not args.no_cache:
        cache = IndexCache(Path(args.cache_dir) if args.cache_dir else default_cache_dir(repo))
    try:
        result = collect_evidence(
            repo,
            command=args.runner_command,
            source_roots=args.source_roots or ["."],
            rev=args.rev,
            setup_command=args.setup_command,
            reverse_check=args.reverse_check,
            extra=args.runner_args,
            cache=cache,
            env_variables=args.env_variables,
        )
    except (GitError, EvidenceError) as exc:
        print(f"diffcone: error: {exc}", file=sys.stderr)
        return 2
    ev = result.evidence
    flags = [r.flags for r in ev.tests.values()]
    print(
        f"diffcone: recorded {len(ev.tests)} tests at {ev.commit[:12]} "
        f"(environment {ev.environment_hash}): {len(ev.symbols)} symbols executed, "
        f"{sum(1 for f in flags if f & FLAG_SUBPROCESS)} started a subprocess"
        + (
            f", {sum(1 for f in flags if f & FLAG_UNSTABLE)} unstable" if ev.reverse_checked else ""
        ),
        file=sys.stderr,
    )
    if ev.environment_at_start is not None:
        print(
            "diffcone: warning: the test run changed its own environment, and the "
            "recording is keyed by the environment at the end; a run in the environment "
            "it started from (a fresh install) will not use it:",
            file=sys.stderr,
        )
        start, end = ev.environment_at_start, ev.environment
        before, after = set(start.get("distributions", ())), set(end.get("distributions", ()))
        lines = [f"removed during the run: {d}" for d in sorted(before - after)]
        lines += [f"installed during the run: {d}" for d in sorted(after - before)]
        lines += [
            f"{key}: {start.get(key)!r} at the start, {end.get(key)!r} at the end"
            for key in sorted(set(start) | set(end))
            if key != "distributions" and start.get(key) != end.get(key)
        ]
        for line in lines[:8]:
            print(f"  {line}", file=sys.stderr)
    print(str(result.store))
    return 0


def _report(args: argparse.Namespace) -> int:
    try:
        report = ci_report.build(ci_report.load(args.dir))
    except ci_report.ReportError as exc:
        print(f"diffcone: error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        text = json.dumps(ci_report.to_dict(report), indent=2) + "\n"
    else:
        text = ci_report.to_markdown(report)
    code = _write(text, args.output)
    return code if code else (0 if report.ok else 1)


def _run_exit_code(returncode: int, missing: bool) -> int:
    """``run``'s exit code from the runner's. Selected targets that did not run
    make it 3 (the run may have skipped tests) unless the runner failed on its
    own; pytest's 5 (no tests ran) is 0 when every selected test was collected
    and skipped or deselected by the project's own options."""
    if missing:
        return returncode if returncode not in (0, 5) else 3
    return 0 if returncode == 5 else returncode


def _repo_problem(repo: str) -> str | None:
    """Why ``--repo`` cannot be used: it must be the top level of a git
    working tree, since module names and runner ids are relative to it and a
    subdirectory would silently analyse something else."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None  # not a repository: the command reports that itself
    if top and Path(top).resolve() != Path(repo).resolve():
        try:
            sub = Path(repo).resolve().relative_to(Path(top).resolve()).as_posix()
        except ValueError:
            sub = repo
        return (
            f"--repo {repo} is inside the repository at {top}, not its top level; pass "
            f"--repo {top} (and --source-root {sub} to analyse that directory)"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception:
        # Exit 1 means a degraded plan, a miss (check) or a failed check
        # (report): a crash in any command must never look like one.
        traceback.print_exc()
        print(
            "diffcone: internal error (a bug; please report it with the traceback above)",
            file=sys.stderr,
        )
        return 2


def _main(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "repo", None) is not None:
        problem = _repo_problem(args.repo)
        if problem:
            print(f"diffcone: error: {problem}", file=sys.stderr)
            return 2
    if args.command == "evidence":
        return _list_evidence(Path(args.repo))
    if args.command == "collect":
        return _collect(args)
    if args.command == "check":
        return _check(args)
    if args.command == "report":
        return _report(args)
    if args.command == "prune":
        return _prune(args)
    options = DiscoveryOptions(
        external_fixtures=frozenset(args.external_fixtures),
        well_known_fixtures=not args.no_well_known_fixtures,
        runner_args=tuple(getattr(args, "runner_args", None) or ()),
    )

    cache = None
    if not args.no_cache:
        cache = IndexCache(
            Path(args.cache_dir) if args.cache_dir else default_cache_dir(Path(args.repo))
        )

    def build_plan(evidence=None):
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
            evidence=evidence,
        )

    def load_evidence():
        if not getattr(args, "evidence", None):
            return None
        manifest_roots = load_manifest(args.targets).source_roots if args.targets else None
        roots = list(args.source_roots or manifest_roots or ["."])
        head = args.head if args.head not in ("WORKTREE", "INDEX") else "HEAD"
        reference = resolve_commit(Path(args.repo), head)
        return load_store(find_store(Path(args.repo), args.evidence, roots, reference))

    try:
        if args.command == "plan":
            result = build_plan(load_evidence())
            text = to_json(result) if args.format == "json" else to_text(result)
            code = _write(text, args.output)
            if code:
                return code
            if result.incomplete_discovery:
                return 3
            return 1 if result.degraded else 0
        if args.command == "run":
            if args.collect and (not args.evidence or args.runner != "pytest" or args.dry_run):
                parser.error("--collect needs --evidence and the pytest runner, without --dry-run")
            evidence = load_evidence()
            result = build_plan(evidence)
            ran_plan = result
            # Why the recording was not used, for ``-o``.
            unused: dict | None = None
            will_run = any(d.selected and d.target.runner == args.runner for d in result.decisions)
            # Only worth refusing when something would actually execute.
            mismatch = worktree_mismatch(Path(args.repo), result) if will_run else None
            if mismatch and not args.allow_mismatched_worktree:
                print(
                    f"diffcone: {mismatch}; running it would execute code the plan never "
                    "analysed. Plan with --head WORKTREE, check the snapshot out, or pass "
                    "--allow-mismatched-worktree.",
                    file=sys.stderr,
                )
                return 2
            incomplete = result.incomplete_discovery
            if incomplete and not args.allow_incomplete_discovery:
                print(
                    f"diffcone: discovery reports {len(incomplete)} place(s) where "
                    f"{args.runner} may collect tests that are not targets; running the "
                    "selected targets would skip them. Pass --allow-incomplete-discovery "
                    "to run anyway, or declare those tests in a manifest.",
                    file=sys.stderr,
                )
                for note in incomplete[:5]:
                    print(f"  {note.kind}: {note.detail}", file=sys.stderr)
                if len(incomplete) > 5:
                    print(f"  ... and {len(incomplete) - 5} more", file=sys.stderr)
                return 3
            if evidence is not None and args.runner == "pytest":
                if args.collect:
                    refusal = advance_refusal(
                        Path(args.repo), result, evidence, args.runner_command, args.runner_args
                    )
                    if refusal:
                        print(f"diffcone: cannot advance the evidence: {refusal}", file=sys.stderr)
                        return 2
                static_plans: list = []

                def static_plan():
                    static_plans.append(build_plan(None))
                    return static_plans[-1]

                checked = run_with_evidence(
                    result,
                    static_plan,
                    cwd=Path(args.repo),
                    command=args.runner_command,
                    extra=args.runner_args,
                    dry_run=args.dry_run,
                    advance_from=evidence if args.collect else None,
                    allow_incomplete=args.allow_incomplete_discovery,
                )
                if checked.advanced is not None:
                    print(
                        f"diffcone: evidence advanced to {checked.advanced.name} "
                        f"({len(checked.result.selected)} test(s) recorded afresh)",
                        file=sys.stderr,
                    )
                elif args.collect:
                    reason = checked.not_advanced or "the environment differs from the recording's"
                    print(f"diffcone: evidence not advanced: {reason}", file=sys.stderr)
                if checked.mismatch is not None:
                    stored = load_store(Path(result.evidence["store"]))
                    differences = environment_differences(stored.environment, checked.mismatch)
                    # The recording's own test run changed the environment,
                    # and this one is where that run started.
                    changed_itself = stored.environment_at_start == checked.mismatch
                    unused = {
                        "reason": "environment",
                        "differences": differences,
                        "recording_changed_its_environment": changed_itself,
                    }
                    print(
                        "diffcone: the environment differs from the one the evidence was "
                        "recorded in, so it says nothing here; ran "
                        + (
                            "the whole suite instead (the static plan's discovery may be "
                            "incomplete):"
                            if checked.static_whole
                            else "the static plan instead:"
                        ),
                        file=sys.stderr,
                    )
                    for line in differences[:8]:
                        print(f"  {line}", file=sys.stderr)
                    if changed_itself:
                        print(
                            "diffcone: this is the environment the recording's own test run "
                            "started in: the recorded tests changed it (installed or removed "
                            "a distribution), so no run in a fresh environment can use it",
                            file=sys.stderr,
                        )
                outcome = checked.ran
                if checked.static is not None and static_plans:
                    ran_plan = static_plans[-1]
            else:
                outcome = run_selected(
                    result,
                    args.runner,
                    cwd=Path(args.repo),
                    command=args.runner_command,
                    extra=args.runner_args,
                    dry_run=args.dry_run,
                )
            if args.output and not args.dry_run:
                # The plan that ran (the static one when the evidence did not
                # apply), as ``plan -o`` writes it.
                text = to_json(ran_plan)
                if unused is not None:
                    data = json.loads(text)
                    data["evidence_not_used"] = unused
                    text = json.dumps(data, indent=2) + "\n"
                code = _write(text, args.output)
                if code:
                    return code
            if outcome.unknown:
                print(
                    f"diffcone: pytest collected {len(outcome.unknown)} test(s) the plan does not "
                    f"know (e.g. {outcome.unknown[0]}); they were run too, since the plan cannot "
                    "say whether the change reaches them",
                    file=sys.stderr,
                )
            if outcome.missing:
                print(
                    f"diffcone: error: pytest did not collect {len(outcome.missing)} selected "
                    f"target(s), so they did not run (e.g. {outcome.missing[0]}); discovery "
                    "and collection disagree",
                    file=sys.stderr,
                )
            status = "degraded" if result.degraded else "complete"
            print(
                f"diffcone: {len(outcome.selected)} of {outcome.total} {args.runner} target(s) "
                f"selected (plan {status})",
                file=sys.stderr,
            )
            if args.dry_run:
                if not outcome.selected:
                    print("diffcone: nothing selected; not running", file=sys.stderr)
                    return 0
                return _write(" ".join(shlex.quote(a) for a in outcome.command) + "\n", args.output)
            if outcome.returncode is None:
                # Nothing ran: an empty selection (a whole-suite fallback ran
                # something even when no static target was selected).
                print("diffcone: nothing selected; not running", file=sys.stderr)
                return 0
            return _run_exit_code(outcome.returncode, bool(outcome.missing))
        if args.command == "validate":
            result = build_plan(load_evidence())
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
            fixed = load_store(Path(args.evidence)) if args.evidence not in (None, "auto") else None

            def make_plan(base: str, head: str):
                evidence = fixed
                if args.evidence == "auto":
                    roots = list(
                        args.source_roots or (manifest.source_roots if manifest else None) or ["."]
                    )
                    evidence = load_store(find_store(Path(args.repo), "auto", roots, head))
                return plan(
                    Path(args.repo),
                    base,
                    head,
                    manifest,
                    source_roots=args.source_roots,
                    discover_runners=args.discover or (),
                    discovery_options=options,
                    cache=cache,
                    evidence=evidence,
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
                        "notes": [
                            {"kind": n.kind, "detail": n.detail, "path": n.path} for n in r.notes
                        ],
                    }
                    for r in results
                ],
            }
            code = _write(json.dumps(data, indent=2) + "\n", args.output)
            if code:
                return code
            # The plan's codes: 3 when a runner may collect tests that are not
            # targets, 1 when files could not be analysed.
            if any(r.incomplete for r in results):
                return 3
            return 1 if index.errors else 0
    except (ManifestError, GitError, EvidenceError, ValueError, OSError) as exc:
        print(f"diffcone: error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

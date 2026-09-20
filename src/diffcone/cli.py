"""Command-line interface.

Exit codes:
  0  plan produced, analysis complete
  1  plan produced, but analysis errors forced a conservative fallback
  2  no plan (bad arguments, unreadable manifest, unknown revision)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from diffcone.discovery import RUNNERS, DiscoveryOptions, discover
from diffcone.indexer import build_index
from diffcone.manifest import ManifestError, load_manifest, manifest_to_dict
from diffcone.planner import plan
from diffcone.report import to_json, to_text
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
        help="pytest fixture provided by an installed plugin; not reported as unresolved",
    )
    p.add_argument("--output", "-o", help="write the result to this file instead of stdout")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffcone",
        description="Static-first, function-level change-impact planning for Python.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "plan",
        help="produce a selection plan for two committed revisions",
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

    d = sub.add_parser(
        "discover",
        help="statically discover targets at a revision and emit a manifest",
        description=(
            "Discover pytest tests and/or ASV benchmarks in a committed revision without "
            "importing them, and print a target manifest (JSON) for `diffcone plan --targets`."
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
    options = DiscoveryOptions(external_fixtures=frozenset(args.external_fixtures))
    try:
        if args.command == "plan":
            if not args.targets and not args.discover:
                parser.error("plan requires --targets and/or --discover")
            manifest = load_manifest(args.targets) if args.targets else None
            result = plan(
                Path(args.repo),
                args.base,
                args.head,
                manifest,
                source_roots=args.source_roots,
                discover_runners=args.discover or (),
                discovery_options=options,
            )
            text = to_json(result) if args.format == "json" else to_text(result)
            code = _write(text, args.output)
            return code if code else (1 if result.degraded else 0)
        if args.command == "discover":
            runners = args.discover or list(RUNNERS)
            roots = args.source_roots or ["."]
            snapshot = read_snapshot(Path(args.repo), args.rev, roots, with_config=True)
            index = build_index(snapshot)
            results = [discover(r, snapshot, index, options) for r in runners]
            data = manifest_to_dict([t for r in results for t in r.targets], roots)
            data["discovery"] = {
                "revision": args.rev,
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

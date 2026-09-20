"""Command-line interface.

Exit codes:
  0  plan produced, analysis complete
  1  plan produced, but analysis errors forced a conservative fallback
  2  no plan (bad arguments, unreadable manifest, unknown revision)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from diffcone.manifest import ManifestError, load_manifest
from diffcone.planner import plan
from diffcone.report import to_json, to_text
from diffcone.snapshot import GitError


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
            "Compare two committed git revisions and report which manifest targets are "
            "affected. Only committed snapshots are analyzed; uncommitted working-tree "
            "changes are ignored. Project code is never executed."
        ),
    )
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument("--base", required=True, help="base git revision (committed)")
    p.add_argument("--head", required=True, help="head git revision (committed)")
    p.add_argument("--targets", required=True, help="path to the JSON target manifest")
    p.add_argument(
        "--source-root",
        action="append",
        dest="source_roots",
        metavar="DIR",
        help="repo-relative directory whose .py files are analyzed as a module tree "
        "(repeatable; overrides the manifest's source_roots; default: .)",
    )
    p.add_argument("--format", choices=("json", "text"), default="json")
    p.add_argument("--output", "-o", help="write the report to this file instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "plan":  # pragma: no cover - argparse enforces this
        parser.error("unknown command")
    try:
        manifest = load_manifest(args.targets)
        result = plan(
            Path(args.repo), args.base, args.head, manifest, source_roots=args.source_roots
        )
    except (ManifestError, GitError) as exc:
        print(f"diffcone: error: {exc}", file=sys.stderr)
        return 2
    text = to_json(result) if args.format == "json" else to_text(result)
    if args.output:
        Path(args.output).write_text(text, "utf-8")
    else:
        sys.stdout.write(text)
    return 1 if result.degraded else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

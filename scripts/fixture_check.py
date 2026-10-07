"""Measure discovery's fixture dependencies against the fixtures pytest uses.

Discovery resolves each test's fixture closure without importing anything
(internal/design.md). ``collection_check.py`` checks *which* tests pytest
collects; this script checks *what each one depends on*: it asks pytest,
through a small plugin loaded into collection, for every fixture function
each test's closure resolves to, and reports the ones inside the repository
that the test's discovered lifecycle dependencies do not cover. A fixture
that is not covered is a fixture whose change would select nothing: a miss.

    uv run python scripts/fixture_check.py --repo DIR --command CMD \\
        [--source-root src --source-root .] [--limit 20] [-o report.json]

A fixture counts as covered when its symbol is among the test's
dependencies, or when the test carries any ``fixture:<name>`` placeholder
(an unresolved fixture selects the test on every change).
Fixtures defined outside the repository (installed plugins) are skipped;
fixtures in the repository but outside the source roots are counted
separately, since no change there is planned anyway. The pseudo-fixtures
pytest makes for directly parametrized names live in pytest itself, so they
are skipped too. Parameter cases are collapsed, their fixtures unioned.

Exit code 1 when a test uses an in-scope fixture its dependencies miss.
It executes project code (collection imports test modules), so it is a
script, not part of planning.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import warnings
from collections import Counter, defaultdict
from pathlib import Path

from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import Indexer
from diffcone.snapshot import read_snapshot

warnings.filterwarnings("ignore", category=SyntaxWarning)

PARAM = re.compile(r"\[.*\]$")

# Loaded into the project's pytest as ``-p diffcone_fixture_probe``; imports
# nothing of diffcone. For every collected item it walks the fixture closure
# pytest computed (``item.fixturenames``, already pruned of what directly
# parametrized names replace), taking for each name the definition pytest
# uses (the last applicable one) and, when that definition requests its own
# name, the one it overrides.
PROBE = """
import inspect, json, os


def _where(func):
    func = getattr(func, "__wrapped__", func)
    try:
        path = inspect.getsourcefile(func)
    except TypeError:
        path = None
    return (os.path.realpath(path) if path else None), getattr(func, "__qualname__", "")


def pytest_collection_finish(session):
    out = {}
    for item in session.items:
        info = getattr(item, "_fixtureinfo", None)
        if info is None:
            continue
        used = []
        seen = set()

        def walk(name, index):
            defs = info.name2fixturedefs.get(name) or ()
            if (name, index) in seen or -index > len(defs):
                return
            seen.add((name, index))
            fixturedef = defs[index]
            path, qualname = _where(fixturedef.func)
            used.append([name, path, qualname])
            if name in fixturedef.argnames:
                walk(name, index - 1)

        for name in item.fixturenames:
            walk(name, -1)
        out.setdefault(item.nodeid, []).extend(used)
    with open(os.environ["DIFFCONE_FIXTURE_PROBE"], "w") as fh:
        json.dump({"rootdir": str(session.config.rootpath), "items": out}, fh)
"""


def used_fixtures(repo: Path, command: str) -> tuple[dict[str, set[tuple[str, str, str]]], str]:
    """Node id (cases collapsed) -> {(fixture name, repo-relative path or
    absolute when outside, qualname)}."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "diffcone_fixture_probe.py").write_text(PROBE)
        out = Path(tmp, "used.json")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [tmp, env.get("PYTHONPATH")]))
        env["DIFFCONE_FIXTURE_PROBE"] = str(out)
        argv = shlex.split(command) + [
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "diffcone_fixture_probe",
        ]
        proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True, env=env)
        log = proc.stdout[-3000:] + proc.stderr[-3000:]
        if not out.exists():
            return {}, log
        data = json.loads(out.read_text())
    root = Path(data["rootdir"]).resolve()
    repo = repo.resolve()
    prefix = root.relative_to(repo).as_posix() if root != repo else ""
    items: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for nodeid, used in data["items"].items():
        node = PARAM.sub("", f"{prefix}/{nodeid}" if prefix else nodeid)
        for name, path, qualname in used:
            if path is None:
                continue
            p = Path(path)
            rel = p.relative_to(repo).as_posix() if p.is_relative_to(repo) else str(p)
            items[node].add((name, rel, qualname))
    return items, log


def discovered(repo: Path, source_roots: list[str], rev: str):
    snapshot = read_snapshot(repo, rev, source_roots=source_roots, with_config=True)
    index = Indexer(snapshot).build()
    result = discover("pytest", snapshot, index, DiscoveryOptions())
    deps: dict[str, set[str]] = defaultdict(set)
    for target in result.targets:
        deps[PARAM.sub("", target.runner_id)].update(target.lifecycle_dependencies)
    # (path, qualname) -> symbol id, for functions the index knows.
    symbols = {
        (s.path, s.id[len(s.module) + 1 :]): s.id
        for s in index.symbols.values()
        if s.kind in ("function", "method")
    }
    return deps, symbols, snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--command", required=True, help='pytest command, e.g. "uv run pytest"')
    parser.add_argument("--rev", default="WORKTREE")
    parser.add_argument("--source-root", action="append", dest="source_roots")
    parser.add_argument("--limit", type=int, default=20, help="examples to print per kind")
    parser.add_argument("-o", "--output", type=Path, help="write the full report as JSON")
    args = parser.parse_args()
    roots = args.source_roots or ["."]

    used, log = used_fixtures(args.repo, args.command)
    if not used:
        print("pytest reported no items; its output ends:\n" + log, file=sys.stderr)
        return 2
    deps, symbols, snapshot = discovered(args.repo, roots, args.rev)
    in_scope = set(snapshot.files)

    missed: dict[str, list[str]] = {}
    by_fixture: Counter[str] = Counter()
    outside_roots: Counter[str] = Counter()
    unmatched: Counter[str] = Counter()  # in the repo, but no symbol in the index
    not_targets = 0
    checked = 0
    for node, fixtures in sorted(used.items()):
        if node not in deps:
            not_targets += 1  # collection_check.py's business
            continue
        checked += 1
        have = deps[node]
        for _name, path, qualname in sorted(fixtures):
            if path.startswith("/") or "site-packages" in path:
                continue  # an installed plugin (a venv may sit inside the repo)
            symbol = symbols.get((path, qualname))
            if symbol is None:
                if path not in in_scope:
                    outside_roots[f"{path}::{qualname}"] += 1
                else:
                    unmatched[f"{path}::{qualname}"] += 1
                continue
            if symbol in have or any(d.startswith("fixture:") for d in have):
                continue  # an unresolved fixture selects the test on every change
            missed.setdefault(node, []).append(symbol)
            by_fixture[symbol] += 1

    print(f"tests checked: {checked} (collected but not targets: {not_targets})")
    print(f"tests with a missed fixture: {len(missed)}")
    for symbol, count in by_fixture.most_common(args.limit):
        example = next(n for n, s in missed.items() if symbol in s)
        print(f"  {count:6d}  {symbol}   e.g. {example}")
    if unmatched:
        print(f"in-repo fixtures with no indexed symbol: {len(unmatched)}")
        for key, count in unmatched.most_common(args.limit):
            print(f"  {count:6d}  {key}")
    if outside_roots:
        print(f"fixtures outside the source roots: {len(outside_roots)}")
    if args.output:
        args.output.write_text(
            json.dumps(
                {
                    "checked": checked,
                    "not_targets": not_targets,
                    "missed": missed,
                    "unmatched": dict(unmatched),
                    "outside_roots": dict(outside_roots),
                },
                indent=2,
            )
        )
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

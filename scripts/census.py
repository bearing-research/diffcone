"""Selection census: where do plans' selections come from?

Plans (never validates) the last few Python-touching commits of many
public pytest projects and attributes every selected test to one cause,
so selection rules can be ranked by how often they matter across
projects rather than by one corpus (docs/roadmap.md, "Selection
census"). Planning runs no project code, so nothing is installed.

    uv run python scripts/census.py run --work DIR -o census.json [--jobs 4]
    uv run python scripts/census.py report census.json

``run`` shallow-clones each repository in REPOS into DIR (reused when
present). Source roots are ``src`` and ``.`` when ``src/`` exists, ``.``
otherwise; the census does not tune roots per project.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

from diffcone.cache import IndexCache
from diffcone.model import UNRESOLVED_DYNAMIC
from diffcone.planner import (
    RULE_ANALYSIS_ERROR,
    RULE_DYNAMIC_REFERENCE,
    RULE_LIFECYCLE_UNRESOLVED,
    RULE_UNRESOLVED_NAME_MATCH,
    plan,
    plan_from_indexes,
)

REPOS = [
    "pallets/flask",
    "pallets/jinja",
    "pallets/werkzeug",
    "pallets/itsdangerous",
    "encode/httpx",
    "encode/starlette",
    "encode/uvicorn",
    "psf/requests",
    "urllib3/urllib3",
    "Textualize/rich",
    "fastapi/typer",
    "fastapi/fastapi",
    "pydantic/pydantic",
    "marshmallow-code/marshmallow",
    "python-attrs/cattrs",
    "jd/tenacity",
    "theskumar/python-dotenv",
    "pytest-dev/pluggy",
    "pytest-dev/pytest-xdist",
    "pytest-dev/pytest-asyncio",
    "pypa/pip",
    "pypa/packaging",
    "pypa/build",
    "pypa/twine",
    "pypa/virtualenv",
    "tox-dev/tox",
    "pre-commit/pre-commit",
    "PyCQA/isort",
    "PyCQA/flake8",
    "nedbat/coveragepy",
    "arrow-py/arrow",
    "dateutil/dateutil",
    "more-itertools/more-itertools",
    "mahmoud/boltons",
    "python-poetry/poetry",
    "psf/black",
    "sqlalchemy/alembic",
    "networkx/networkx",
    "scrapy/scrapy",
    "agronholm/anyio",
    "python-trio/trio",
    "pygments/pygments",
]

DEPTH = 80


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _clone(slug: str, work: Path) -> Path:
    dest = work / slug.replace("/", "__")
    if not dest.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                str(DEPTH),
                "--no-tags",
                "--single-branch",
                f"https://github.com/{slug}.git",
                str(dest),
            ],
            check=True,
            capture_output=True,
        )
    return dest


def _commits(repo: Path, count: int) -> list[str]:
    """The latest first-parent commits that change a ``.py`` file and whose
    parent is present in the shallow clone."""
    shas = _git(repo, "log", "--first-parent", "--format=%H", f"-n{DEPTH - 2}").split()
    picked: list[str] = []
    for sha in shas:
        try:
            names = _git(repo, "diff", "--name-only", f"{sha}~1", sha).split()
        except subprocess.CalledProcessError:
            break  # shallow boundary
        if any(n.endswith(".py") for n in names):
            picked.append(sha)
            if len(picked) == count:
                break
    return picked


# --------------------------------------------------------------------------- dynamic shapes


def _name_shape(expr: ast.expr, func: ast.AST) -> str:
    """What a non-literal name argument is: how a rule could bound it."""
    args = getattr(func, "args", None)
    params = [a.arg for a in ast.walk(args) if isinstance(a, ast.arg)] if args is not None else []
    if isinstance(expr, ast.Name):
        if expr.id in params:
            return "parameter"
        for node in ast.walk(func):
            if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                if any(isinstance(n, ast.Name) and n.id == expr.id for n in ast.walk(node.target)):
                    return "loop variable"
        return "local variable"
    if isinstance(expr, ast.Attribute):
        base = expr.value
        if isinstance(base, ast.Name) and params and base.id == params[0]:
            return "self attribute"
        return "attribute"
    if isinstance(expr, ast.Subscript):
        return "subscript"
    if isinstance(expr, ast.Call):
        return "call result"
    if isinstance(expr, (ast.JoinedStr, ast.BinOp)):
        return "built string"
    return type(expr).__name__


def _uses(tree: ast.AST) -> list[str]:
    """Labels of the dynamic uses in a function or module body."""
    labels: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "getattr" and len(node.args) >= 2:
            arg = node.args[1]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                labels.append(f"getattr: {_name_shape(arg, tree)}")
        elif name in ("import_module", "__import__") and node.args:
            arg = node.args[0]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                labels.append(f"{name}: {_name_shape(arg, tree)}")
        elif name in ("eval", "exec", "globals", "vars") and isinstance(func, ast.Name):
            labels.append(f"{name}()")
    return labels


def _seed_labels(repo: Path, commit: str, index, symbol_id: str, cache: dict) -> list[str]:
    """Classify each dynamic use in a seed symbol's source at ``commit``."""
    key = (commit, symbol_id)
    if key in cache:
        return cache[key]
    symbol = index.symbols.get(symbol_id)
    labels: list[str] = []
    if symbol is not None:
        try:
            source = _git(repo, "show", f"{commit}:{symbol.path}")
            tree = ast.parse(source)
        except (subprocess.CalledProcessError, SyntaxError, ValueError):
            tree = None
        if tree is not None:
            if symbol.kind in ("function", "method"):
                nodes = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == symbol.name
                    and any(lo <= n.lineno <= hi for lo, hi in symbol.line_ranges)
                ]
                for n in nodes:
                    labels += _uses(n)
            else:
                # Module, class or variable: its own statements, not nested defs.
                for lo, hi in symbol.line_ranges:
                    for stmt in ast.walk(tree):
                        if isinstance(stmt, ast.stmt) and lo <= stmt.lineno <= hi:
                            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                labels += _uses(stmt)
    labels = sorted(set(labels)) or ["unclassified"]
    cache[key] = labels
    return labels


# --------------------------------------------------------------------------- attribution


def _selected(p) -> set[str]:
    return {d.target.runner_id for d in p.decisions if d.selected}


def _without(p, drop) -> set[str]:
    """Selection when the unresolved references ``drop`` accepts are removed
    from both indexes (everything else, targets included, unchanged)."""

    def strip(index):
        return replace(index, unresolved={u for u in index.unresolved if not drop(u)})

    counterfactual = plan_from_indexes(
        strip(p.base_index),
        strip(p.head_index),
        None,
        repo=p.repo,
        source_roots=p.source_roots,
        discovered=p.discovery,
        # Without these, every target selected through a declared edge looks
        # like one that dynamic references or name matches caused.
        declarations=p.declarations,
    )
    # Only targets reached through the graph: fallbacks (unknown fixtures,
    # analysis errors) select regardless of what was removed.
    return {d.target.runner_id for d in counterfactual.decisions if any(r.path for r in d.reasons)}


def _is_dynamic(u) -> bool:
    return u.kind == UNRESOLVED_DYNAMIC


def _is_name(u) -> bool:
    return u.kind != UNRESOLVED_DYNAMIC


def _causes(p) -> dict[str, tuple[str, str]]:
    """(cause, detail) per selected target. A plan explains each target by
    one path only, so causes come from counterfactual plans: a target still
    selected with dynamic references and name matches both removed has a
    resolved dependency; one lost only when dynamic references are removed
    is caused by them (likewise name matches); one lost only when both are
    removed needs either. Fallbacks (analysis errors, unknown fixtures) are
    read from the target's own reasons."""
    full = _selected(p)
    no_dynamic = _without(p, _is_dynamic)
    no_names = _without(p, _is_name)
    neither = _without(p, lambda u: True)
    out: dict[str, tuple[str, str]] = {}
    for d in p.decisions:
        if not d.selected:
            continue
        rid = d.target.runner_id
        rules = {r.rule: r for r in d.reasons}
        explained = next((r for r in d.reasons if r.path), None)
        if RULE_ANALYSIS_ERROR in rules:
            out[rid] = ("analysis error", "")
        elif rid in neither:
            out[rid] = ("dependency", "")
        elif RULE_LIFECYCLE_UNRESOLVED in rules and rid not in (no_dynamic | no_names):
            out[rid] = ("unknown fixture", rules[RULE_LIFECYCLE_UNRESOLVED].detail)
        elif rid not in no_dynamic and rid in no_names:
            seed = ""
            if explained is not None and explained.rule == RULE_DYNAMIC_REFERENCE:
                seed = explained.path[-1].target
            out[rid] = ("dynamic reference", seed)
        elif rid not in no_names and rid in no_dynamic:
            name = ""
            if explained is not None and explained.rule == RULE_UNRESOLVED_NAME_MATCH:
                steps = [s for s in explained.path if s.kind == "unresolved_name_match"]
                name = steps[-1].target.rsplit(".", 1)[-1] if steps else ""
            out[rid] = ("name match", name)
        elif RULE_LIFECYCLE_UNRESOLVED in rules:
            out[rid] = ("unknown fixture", rules[RULE_LIFECYCLE_UNRESOLVED].detail)
        else:
            out[rid] = ("dynamic or name match", "")
    assert set(out) == full
    return out


def census_repo(slug: str, work: Path, count: int) -> dict:
    result: dict = {"repo": slug, "commits": [], "static": {}, "error": None}
    try:
        repo = _clone(slug, work)
        roots = ["src", "."] if (repo / "src").is_dir() else ["."]
        result["roots"] = roots
        cache = IndexCache(work / "_cache" / slug.replace("/", "__"))
        seed_cache: dict = {}
        commits = _commits(repo, count)
        for i, sha in enumerate(commits):
            started = time.perf_counter()
            try:
                p = plan(
                    repo,
                    f"{sha}~1",
                    sha,
                    source_roots=roots,
                    discover_runners=["pytest"],
                    cache=cache,
                )
            except Exception as exc:  # noqa: BLE001 - one bad commit must not end the census
                result["commits"].append({"sha": sha[:10], "error": f"{type(exc).__name__}: {exc}"})
                continue
            causes: Counter = Counter()
            dynamic: Counter = Counter()
            seeds: Counter = Counter()
            names: Counter = Counter()
            fixtures: Counter = Counter()
            for cause, detail in _causes(p).values():
                causes[cause] += 1
                if cause == "dynamic reference" and detail:
                    seeds[detail] += 1
                    labels = _seed_labels(repo, sha, p.head_index, detail, seed_cache)
                    for label in labels:
                        dynamic[label] += 1 / len(labels)
                elif cause == "name match":
                    names[detail] += 1
                elif cause == "unknown fixture":
                    fixtures[detail] += 1
            result["commits"].append(
                {
                    "sha": sha[:10],
                    "targets": len(p.decisions),
                    "selected": sum(d.selected for d in p.decisions),
                    "degraded": p.degraded,
                    "errors": [e.message[:200] for e in p.errors][:5],
                    "seconds": round(time.perf_counter() - started, 2),
                    "causes": dict(causes),
                    "dynamic": {k: round(v, 3) for k, v in dynamic.items()},
                    "seeds": dict(seeds.most_common(10)),
                    "names": dict(names.most_common(10)),
                    "fixtures": dict(fixtures.most_common(10)),
                }
            )
            if i == 0:
                # Prevalence at the newest commit: every dynamic seed in the
                # index, whether or not a commit reached it.
                static: Counter = Counter()
                for u in p.head_index.unresolved:
                    if u.kind == UNRESOLVED_DYNAMIC:
                        static.update(_seed_labels(repo, sha, p.head_index, u.symbol, seed_cache))
                result["static"] = dict(static)
                result["modules"] = len(p.head_index.modules)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def run(args: argparse.Namespace) -> int:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    repos = args.repo or REPOS
    results = []
    with ProcessPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(census_repo, slug, work, args.commits) for slug in repos]
        for future in futures:
            r = future.result()
            ok = [c for c in r["commits"] if "error" not in c]
            print(
                f"{r['repo']}: {len(ok)} planned, {len(r['commits']) - len(ok)} failed"
                + (f", error {r['error']}" if r["error"] else ""),
                file=sys.stderr,
            )
            results.append(r)
    Path(args.output).write_text(json.dumps(results, indent=1) + "\n")
    return 0


# --------------------------------------------------------------------------- report

CAUSES = [
    "dependency",
    "dynamic reference",
    "name match",
    "dynamic or name match",
    "unknown fixture",
    "analysis error",
]


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f} %" if whole else "n/a"


def report(args: argparse.Namespace) -> int:
    results = json.loads(Path(args.results).read_text())
    out: list[str] = []
    total: Counter = Counter()
    dyn_total: Counter = Counter()
    dyn_repos: dict[str, set[str]] = defaultdict(set)
    static_repos: dict[str, set[str]] = defaultdict(set)
    name_total: Counter = Counter()
    dominated: Counter = Counter()
    out.append(
        "| repository | modules | commits | mean selected | dependency | dynamic | name match "
        "| either | unknown fixture | degraded plans |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    fixture_total: Counter = Counter()
    errors: list[str] = []
    for r in results:
        ok = [c for c in r["commits"] if "error" not in c]
        if not ok:
            out.append(f"| {r['repo']} | | 0 | {r.get('error') or 'no commits'} | | | | | | |")
            continue
        causes: Counter = Counter()
        for c in ok:
            causes.update(c["causes"])
            for label, v in c["dynamic"].items():
                dyn_total[label] += v
                dyn_repos[label].add(r["repo"])
            name_total.update(
                {f"{k} ({r['repo'].split('/')[1]})": v for k, v in c["names"].items()}
            )
            fixture_total.update(
                {f"{k} ({r['repo'].split('/')[1]})": v for k, v in c.get("fixtures", {}).items()}
            )
            if c["degraded"]:
                errors.append(f"{r['repo']} {c['sha']}: {c['errors'][0] if c['errors'] else ''}")
        for label in r["static"]:
            static_repos[label].add(r["repo"])
        selected = sum(causes.values())
        total.update(causes)
        mean = sum(c["selected"] / c["targets"] for c in ok if c["targets"]) / max(
            1, sum(1 for c in ok if c["targets"])
        )
        if selected:
            top = max(CAUSES, key=lambda k: causes[k])
            if causes[top] / selected >= 0.5:
                dominated[top] += 1
        out.append(
            f"| {r['repo']} | {r.get('modules', '')} | {len(ok)} | {100 * mean:.0f} % | "
            + " | ".join(_share(causes[k], selected) for k in CAUSES[:5])
            + f" | {sum(c['degraded'] for c in ok)} |"
        )
    grand = sum(total.values())
    out.append("")
    out.append(f"All selections: {grand}.")
    for k in CAUSES:
        out.append(f"* {k}: {total[k]} ({100 * total[k] / max(1, grand):.0f} %)")
    out.append(f"* other: {grand - sum(total[k] for k in CAUSES)}")
    out.append("")
    out.append(
        "Repositories where one cause is at least half of all selections: "
        + ", ".join(f"{k} {v}" for k, v in dominated.most_common())
    )
    out.append("")
    out.append(
        "| dynamic construct | selections it caused | repositories (selections) "
        "| repositories (present) |"
    )
    out.append("|---|---|---|---|")
    for label in sorted(set(dyn_total) | set(static_repos), key=lambda k: -dyn_total.get(k, 0)):
        out.append(
            f"| {label} | {dyn_total.get(label, 0):.0f} | {len(dyn_repos.get(label, ()))} | "
            f"{len(static_repos.get(label, ()))} |"
        )
    out.append("")
    out.append(
        "Top name-match attributes: " + ", ".join(f"{k} {v}" for k, v in name_total.most_common(15))
    )
    out.append("")
    out.append(
        "Top unknown fixtures: " + ", ".join(f"{k} {v}" for k, v in fixture_total.most_common(15))
    )
    out.append("")
    out.append("Degraded plans:")
    out.extend(f"* {e}" for e in errors)
    print("\n".join(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--work", required=True)
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--commits", type=int, default=8)
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--repo", action="append", help="owner/name; repeatable (default: REPOS)")
    p = sub.add_parser("report")
    p.add_argument("results")
    args = parser.parse_args(argv)
    return run(args) if args.command == "run" else report(args)


if __name__ == "__main__":
    raise SystemExit(main())

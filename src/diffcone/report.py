"""Human-readable and JSON reports for a plan."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from typing import Any, TextIO

from diffcone.model import (
    SnapshotInfo,
    UnresolvedKind,
)
from diffcone.planner import (
    Decision,
    Plan,
    Reason,
    Rule,
    without_cyclic_gc,
)

# 2: unresolved_relationships.matched_affected_symbols (was matched_changed_symbols)
# 3: analysis.evidence (null for a static plan) and the evidence reason rules
# 4: target records drop lifecycle_dependencies (audit E37: 634 of a pandas
#    plan's 709 MB; discovery's output keeps them)
# 5: a reason an all-targets fallback gives names it by its index in
#    fallback_decisions instead of repeating its detail (strata's 29 such
#    details, repeated for each of 7694 targets, were 377 MB of a 429 MB plan)
# 6: a target carrying every all-targets fallback of a rule says so in one
#    reason (``all_targets``) instead of naming each (a pandas plan's 1 079,
#    named on each of 25 618 targets, were 27.7 million entries, 3.8 GB)
SCHEMA_VERSION = 6

SCOPE_DESCRIPTION = {
    "granularity": "whole functions, methods, classes and modules",
    "resolved": [
        "module-level functions, methods, classes and simple module-level assignments "
        "(NAME = <expr>, bound once) as symbols",
        "direct name and attribute references resolvable through module-level "
        "definitions, import aliases, star imports within source roots, or self/cls",
        "absolute and relative imports within source roots",
        "class attribute lookup through the in-scope MRO, including super()",
        "importlib.import_module / getattr with literal arguments",
        "transitive dependencies",
        "manifest-declared entry and lifecycle dependencies",
    ],
    "not_resolved": [
        "type inference and dynamic dispatch on receivers of unknown type",
        "instance attributes and arbitrary object.method() calls (name-bounded fallback)",
        "dynamic imports / getattr / eval with non-literal arguments (dynamic fallback)",
        "code outside the source roots and installed packages (assumed unchanged)",
    ],
}


def _global_fallbacks(plan: Plan) -> dict[tuple[str, str], int]:
    """Each all-targets fallback's (rule, detail), to its index in the
    plan's fallbacks: every target carries it as a reason, so the report
    writes it once and names it from each target."""
    found: dict[tuple[str, str], int] = {}
    for i, f in enumerate(plan.fallbacks):
        if f.scope == "all_targets":
            found.setdefault((f.rule, f.detail), i)
    return found


def _fallback_index(reason: Reason, fallbacks: dict[tuple[str, str], int]) -> int | None:
    if reason.path or reason.changed_symbol is not None or reason.changes:
        return None
    return fallbacks.get((reason.rule, reason.detail))


def _per_rule(fallbacks: dict[tuple[str, str], int]) -> Counter[str]:
    """How many all-targets fallbacks (distinct details) each rule has."""
    return Counter(rule for rule, _ in fallbacks)


# A target's reasons as the report writes them: a reason as it is, a
# fallback by its index, or the rule of which it carries every
# all-targets fallback, once.
_Written = tuple[str, Reason, int]


def _written_reasons(
    reasons: list[Reason], fallbacks: dict[tuple[str, str], int], per_rule: Counter[str]
) -> list[_Written]:
    """``reasons`` with the all-targets fallbacks a target carries: each
    rule of which it carries every one is written once, where its first
    falls (``("all", reason, n)``, n the rule's count); any other by its
    index (``("fallback", reason, index)``); the rest as they are."""
    carried: dict[str, set[int]] = {}
    for r in reasons:
        index = _fallback_index(r, fallbacks)
        if index is not None:
            carried.setdefault(r.rule, set()).add(index)
    whole = {rule for rule, found in carried.items() if len(found) == per_rule[rule]}
    out: list[_Written] = []
    written: set[str] = set()
    for r in reasons:
        index = _fallback_index(r, fallbacks)
        if index is None:
            out.append(("reason", r, -1))
        elif r.rule not in whole:
            out.append(("fallback", r, index))
        elif r.rule not in written:
            written.add(r.rule)
            out.append(("all", r, per_rule[r.rule]))
    return out


def _written_dict(item: _Written) -> dict[str, Any]:
    kind, reason, n = item
    if kind == "all":
        return {"rule": reason.rule, "conservative": reason.conservative, "all_targets": True}
    if kind == "fallback":
        return {"rule": reason.rule, "conservative": reason.conservative, "fallback": n}
    return _reason_dict(reason, {})


def _reason_dict(reason: Reason, fallbacks: dict[tuple[str, str], int]) -> dict[str, Any]:
    index = _fallback_index(reason, fallbacks)
    if index is not None:
        return {"rule": reason.rule, "conservative": reason.conservative, "fallback": index}
    return {
        "rule": reason.rule,
        "conservative": reason.conservative,
        "detail": reason.detail,
        "changed_symbol": reason.changed_symbol,
        "changes": list(reason.changes),
        "path": [asdict(step) | {"revisions": list(step.revisions)} for step in reason.path],
    }


def _target_dict(decision: Decision) -> dict[str, Any]:
    t = decision.target
    return {
        "runner": t.runner,
        "runner_id": t.runner_id,
        "entry_symbol": t.entry_symbol,
    }


def snapshot_to_dict(info: SnapshotInfo) -> dict[str, Any]:
    return {
        "revision": info.revision,
        "commit": info.commit,
        "kind": info.kind,
        "uncommitted": not info.committed,
        "description": info.description,
        # ``diffcone.toml``'s ``[index] exclude``: .py files read as other files.
        "excluded": list(info.excluded),
    }


@without_cyclic_gc
def to_dict(plan: Plan) -> dict[str, Any]:
    selected = [d for d in plan.decisions if d.selected]
    unselected = [d for d in plan.decisions if not d.selected]
    fallbacks = _global_fallbacks(plan)
    per_rule = _per_rule(fallbacks)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "degraded" if plan.degraded else "complete",
        # A runner may collect tests that are not targets: the plan is not a
        # judgement on those, so running only its targets would skip them.
        "discovery_incomplete": bool(plan.incomplete_discovery),
        # Dependencies the project stated that the analysis cannot see.
        "declarations": [
            {"from": d.source, "to": d.target, "why": d.why} for d in plan.declarations
        ],
        # Targets the project runs on every change, whatever the plan found.
        "always_run": [
            {
                "targets": a.targets,
                "runner": a.runner or None,
                "why": a.why,
                "matched": plan.always_run_matched.get(a, 0),
            }
            for a in plan.always_run
        ],
        # ``[build] reads``: the paths outside the source roots the tests'
        # build reads, and the changed build inputs it set aside.
        "build_reads": {
            "patterns": None if plan.build_reads is None else list(plan.build_reads),
            "set_aside": list(plan.build_set_aside),
        },
        "analysis": {
            "repo": plan.repo,
            "base": snapshot_to_dict(plan.base),
            "head": snapshot_to_dict(plan.head),
            "source_roots": plan.source_roots,
            "working_tree_analyzed": plan.working_tree_analyzed,
            "uncommitted_analyzed": plan.uncommitted_analyzed,
            "scope": SCOPE_DESCRIPTION | {"analyzed": plan.scope_statement},
            "evidence": plan.evidence,
            "counts": {
                "modules_base": len(plan.base_index.modules),
                "modules_head": len(plan.head_index.modules),
                "symbols_base": len(plan.base_index.symbols),
                "symbols_head": len(plan.head_index.symbols),
                "targets": len(plan.decisions),
                "selected": len(selected),
                "unselected": len(unselected),
                "always_run": sum(
                    any(r.rule == Rule.ALWAYS_RUN for r in d.reasons) for d in selected
                ),
            },
        },
        "changed_symbols": [
            {
                "id": c.id,
                "kind": c.kind,
                "changes": list(c.changes),
                "base_path": c.base.path if c.base else None,
                "head_path": c.head.path if c.head else None,
            }
            for c in plan.changes
        ],
        "selected_targets": [
            _target_dict(d)
            | {
                "rules": sorted({r.rule for r in d.reasons}),
                "conservative": any(r.conservative for r in d.reasons),
                "affected_dependencies": d.affected_dependencies,
            }
            for d in selected
        ],
        "unselected_targets": [
            _target_dict(d) | {"reason": d.unselected_reason} for d in unselected
        ],
        "dependency_explanations": [
            {"runner": d.target.runner, "runner_id": d.target.runner_id}
            | {
                "reasons": [
                    _written_dict(item) for item in _written_reasons(d.reasons, fallbacks, per_rule)
                ]
            }
            for d in selected
        ],
        "unresolved_relationships": [
            {
                "symbol": u.symbol,
                "kind": u.kind,
                "name": u.name,
                "detail": u.detail,
                "revisions": list(u.revisions),
                "matched_affected_symbols": list(u.matched_affected_symbols),
            }
            for u in plan.unresolved
        ],
        "fallback_decisions": [
            {"rule": f.rule, "scope": f.scope, "target": f.target, "detail": f.detail}
            for f in plan.fallbacks
        ],
        "analysis_errors": [
            {"revision": e.revision, "path": e.path, "message": e.message} for e in plan.errors
        ],
        "discovery": [
            {
                "runner": d.runner,
                "targets": len(d.targets),
                "config": d.config,
                "notes": [{"kind": n.kind, "detail": n.detail} for n in d.notes],
            }
            for d in plan.discovery
        ],
    }


@without_cyclic_gc
def to_json(plan: Plan, indent: int | None = 2) -> str:
    return json.dumps(to_dict(plan), indent=indent, sort_keys=False) + "\n"


# How many encoded pieces write_json joins per write.
_PIECES = 65536


@without_cyclic_gc
def write_json(plan: Plan, out: TextIO, indent: int | None = 2) -> None:
    """``to_json(plan)`` written to ``out`` as it is encoded: ``json.dumps``
    keeps every piece in a list before joining them (pandas: millions of
    small strings, 1.7 GB at the end of a plan)."""
    pieces: list[str] = []
    for piece in json.JSONEncoder(indent=indent, sort_keys=False).iterencode(to_dict(plan)):
        pieces.append(piece)
        if len(pieces) >= _PIECES:
            out.write("".join(pieces))
            pieces.clear()
    pieces.append("\n")
    out.write("".join(pieces))


def _format_path(reason: Reason) -> str:
    if not reason.path:
        return ""
    parts = [reason.path[0].source]
    for step in reason.path:
        parts.append(f"-[{step.kind}{':' + step.detail if step.detail else ''}]-> {step.target}")
    return " ".join(parts)


@without_cyclic_gc
def to_text(plan: Plan) -> str:
    lines: list[str] = []
    lines.append(f"diffcone plan: {plan.base.revision} -> {plan.head.revision}")
    lines.append(f"source roots: {', '.join(plan.source_roots)}")
    lines.append(f"base: {plan.base.description}")
    lines.append(f"head: {plan.head.description}")
    for side, info in (("base", plan.base), ("head", plan.head)):
        if info.excluded:
            lines.append(f"{side} excludes from the analysis: {', '.join(info.excluded)}")
    lines.append(f"scope: {plan.scope_statement}")
    lines.append(f"status: {'DEGRADED' if plan.degraded else 'complete'}")
    if plan.evidence is not None:
        ev = plan.evidence
        lines.append(
            f"evidence: {ev['tests']} tests recorded at {ev['commit'][:12]} "
            f"(environment {ev['environment_hash']}, PYTHONHASHSEED={ev['hash_seed']}); "
            f"planned {', '.join(ev['planned'])}"
        )
        if ev.get("environment_checked"):
            lines.append("  environment checked: the run's matched the recording's")
        else:
            lines.append(
                "  environment NOT checked: this plan holds only if the tests run in the "
                "recorded environment (`diffcone run --evidence` checks it before any test "
                "runs, and plans from the code when it differs)"
            )
        if ev["changes_outside_range"]:
            lines.append(
                f"  {len(ev['changes_outside_range'])} change(s) since the evidence lie outside "
                "base..head and were planned too"
            )
    if plan.declarations:
        lines.append(f"declared dependencies ({len(plan.declarations)}):")
        for d in plan.declarations:
            lines.append(f"  {d.source} -> {d.target}" + (f"  ({d.why})" if d.why else ""))
    if plan.always_run:
        lines.append(f"always run ({len(plan.always_run)}):")
        for a in plan.always_run:
            matched = plan.always_run_matched.get(a, 0)
            # Matching nothing is not an error (a job may skip what an entry
            # names), so the count is the only sign of a mistyped pattern.
            lines.append(
                f"  {a.label}: {matched} target(s)"
                + (
                    ""
                    if matched
                    else ": MATCHES NOTHING in this plan; if this job runs those tests, "
                    "the pattern is wrong and they are not selected"
                )
                + (f"  ({a.why})" if a.why else "")
            )
    if plan.build_set_aside:
        lines.append(
            f"set aside by [build] reads ({len(plan.build_set_aside)}): changed build "
            "input(s) outside the source roots that no pattern names; if the tests' build "
            "reads one, the table is wrong and this plan may miss tests:"
        )
        lines.extend(f"  {path}" for path in plan.build_set_aside)
    if plan.incomplete_discovery:
        lines.append(
            f"discovery INCOMPLETE: {len(plan.incomplete_discovery)} place(s) where a runner "
            "may collect tests that are not targets (see the notes below)"
        )
    lines.append("")
    lines.append(f"changed symbols ({len(plan.changes)}):")
    for c in plan.changes or []:
        lines.append(f"  {c.id} [{c.kind}] {', '.join(c.changes)}")
    if not plan.changes:
        lines.append("  (none)")
    lines.append("")
    selected = plan.selected
    fallbacks = _global_fallbacks(plan)
    per_rule = _per_rule(fallbacks)
    lines.append(f"selected targets ({len(selected)}):")
    for d in selected:
        flag = " (conservative)" if any(r.conservative for r in d.reasons) else ""
        lines.append(f"  {d.target.runner}: {d.target.runner_id}{flag}")
        for kind, r, n in _written_reasons(d.reasons, fallbacks, per_rule):
            if kind == "all":
                which = "the fallback decision" if n == 1 else f"every fallback decision ({n})"
                lines.append(f"    - {r.rule}: {which} below with this rule")
                continue
            if kind == "fallback":
                lines.append(f"    - {r.rule}: fallback decision {n + 1} (below)")
                continue
            path = _format_path(r)
            lines.append(f"    - {r.rule}: {r.detail}")
            if path:
                lines.append(f"      {path}")
    if not selected:
        lines.append("  (none)")
    lines.append("")
    unselected = plan.unselected
    lines.append(f"unselected targets ({len(unselected)}):")
    for d in unselected:
        lines.append(f"  {d.target.runner}: {d.target.runner_id}")
    if not unselected:
        lines.append("  (none)")
    matched = [u for u in plan.unresolved if u.matched_affected_symbols]
    dynamic = [u for u in plan.unresolved if u.kind == UnresolvedKind.DYNAMIC]
    lines.append("")
    lines.append(
        f"unresolved relationships: {len(plan.unresolved)} "
        f"({len(matched)} matching an affected symbol, {len(dynamic)} dynamic)"
    )
    for u in matched:
        lines.append(
            f"  {u.symbol}: {u.kind} {u.detail!r} may refer to "
            f"{', '.join(u.matched_affected_symbols)}"
        )
    for u in dynamic:
        lines.append(f"  {u.symbol}: dynamic {u.detail}")
    if plan.fallbacks:
        lines.append("")
        lines.append(f"fallback decisions ({len(plan.fallbacks)}):")
        for n, f in enumerate(plan.fallbacks, 1):
            where = f" [{f.target}]" if f.target else ""
            lines.append(f"  {n}. {f.rule} ({f.scope}){where}: {f.detail}")
    if plan.errors:
        lines.append("")
        lines.append(f"analysis errors ({len(plan.errors)}):")
        for e in plan.errors:
            lines.append(f"  {e.revision}:{e.path}: {e.message}")
    for d in plan.discovery:
        lines.append("")
        lines.append(f"discovery ({d.runner}): {len(d.targets)} target(s), {len(d.notes)} note(s)")
        for source, values in (d.config.get("run_arguments") or {}).items():
            # What discovery read besides the configuration file.
            lines.append(f"  read ({source}): {' '.join(values)}")
        for n in d.notes:
            lines.append(f"  {n.kind}: {n.detail}")
    return "\n".join(lines) + "\n"

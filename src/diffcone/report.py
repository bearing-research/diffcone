"""Human-readable and JSON reports for a plan."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from diffcone.model import SnapshotInfo
from diffcone.planner import Decision, Plan, Reason

SCHEMA_VERSION = 1

SCOPE_DESCRIPTION = {
    "granularity": "whole functions, methods, classes and modules",
    "resolved": [
        "module-level functions, methods and classes as symbols",
        "direct name and attribute references resolvable through module-level "
        "definitions, import aliases, star imports within source roots, or self/cls",
        "absolute and relative imports within source roots",
        "importlib.import_module / getattr with literal arguments",
        "transitive dependencies",
        "manifest-declared entry and lifecycle dependencies",
    ],
    "not_resolved": [
        "type inference, dynamic dispatch, inheritance lookup",
        "instance attributes and arbitrary object.method() calls (name-bounded fallback)",
        "dynamic imports / getattr / eval with non-literal arguments (dynamic fallback)",
        "pytest fixture or ASV setup discovery (must be declared in the manifest)",
    ],
}


def _reason_dict(reason: Reason) -> dict[str, Any]:
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
        "lifecycle_dependencies": list(t.lifecycle_dependencies),
    }


def snapshot_to_dict(info: SnapshotInfo) -> dict[str, Any]:
    return {
        "revision": info.revision,
        "commit": info.commit,
        "kind": info.kind,
        "uncommitted": not info.committed,
        "description": info.description,
    }


def to_dict(plan: Plan) -> dict[str, Any]:
    selected = [d for d in plan.decisions if d.selected]
    unselected = [d for d in plan.decisions if not d.selected]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "degraded" if plan.degraded else "complete",
        "analysis": {
            "repo": plan.repo,
            "base": snapshot_to_dict(plan.base),
            "head": snapshot_to_dict(plan.head),
            "source_roots": plan.source_roots,
            "working_tree_analyzed": plan.working_tree_analyzed,
            "uncommitted_analyzed": plan.uncommitted_analyzed,
            "scope": SCOPE_DESCRIPTION | {"analyzed": plan.scope_statement},
            "counts": {
                "modules_base": len(plan.base_index.modules),
                "modules_head": len(plan.head_index.modules),
                "symbols_base": len(plan.base_index.symbols),
                "symbols_head": len(plan.head_index.symbols),
                "targets": len(plan.decisions),
                "selected": len(selected),
                "unselected": len(unselected),
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
            | {"reasons": [_reason_dict(r) for r in d.reasons]}
            for d in selected
        ],
        "unresolved_relationships": [
            {
                "symbol": u.symbol,
                "kind": u.kind,
                "name": u.name,
                "detail": u.detail,
                "revisions": list(u.revisions),
                "matched_changed_symbols": list(u.matched_changed_symbols),
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


def to_json(plan: Plan, indent: int | None = 2) -> str:
    return json.dumps(to_dict(plan), indent=indent, sort_keys=False) + "\n"


def _format_path(reason: Reason) -> str:
    if not reason.path:
        return ""
    parts = [reason.path[0].source]
    for step in reason.path:
        parts.append(f"-[{step.kind}{':' + step.detail if step.detail else ''}]-> {step.target}")
    return " ".join(parts)


def to_text(plan: Plan) -> str:
    lines: list[str] = []
    lines.append(f"diffcone plan: {plan.base.revision} -> {plan.head.revision}")
    lines.append(f"source roots: {', '.join(plan.source_roots)}")
    lines.append(f"base: {plan.base.description}")
    lines.append(f"head: {plan.head.description}")
    lines.append(f"scope: {plan.scope_statement}")
    lines.append(f"status: {'DEGRADED' if plan.degraded else 'complete'}")
    lines.append("")
    lines.append(f"changed symbols ({len(plan.changes)}):")
    for c in plan.changes or []:
        lines.append(f"  {c.id} [{c.kind}] {', '.join(c.changes)}")
    if not plan.changes:
        lines.append("  (none)")
    lines.append("")
    selected = plan.selected
    lines.append(f"selected targets ({len(selected)}):")
    for d in selected:
        flag = " (conservative)" if any(r.conservative for r in d.reasons) else ""
        lines.append(f"  {d.target.runner}: {d.target.runner_id}{flag}")
        for r in d.reasons:
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
    matched = [u for u in plan.unresolved if u.matched_changed_symbols]
    dynamic = [u for u in plan.unresolved if u.kind == "dynamic"]
    lines.append("")
    lines.append(
        f"unresolved relationships: {len(plan.unresolved)} "
        f"({len(matched)} matching a changed symbol, {len(dynamic)} dynamic)"
    )
    for u in matched:
        lines.append(
            f"  {u.symbol}: {u.kind} {u.detail!r} may refer to "
            f"{', '.join(u.matched_changed_symbols)}"
        )
    for u in dynamic:
        lines.append(f"  {u.symbol}: dynamic {u.detail}")
    if plan.fallbacks:
        lines.append("")
        lines.append(f"fallback decisions ({len(plan.fallbacks)}):")
        for f in plan.fallbacks:
            where = f" [{f.target}]" if f.target else ""
            lines.append(f"  {f.rule} ({f.scope}){where}: {f.detail}")
    if plan.errors:
        lines.append("")
        lines.append(f"analysis errors ({len(plan.errors)}):")
        for e in plan.errors:
            lines.append(f"  {e.revision}:{e.path}: {e.message}")
    for d in plan.discovery:
        lines.append("")
        lines.append(f"discovery ({d.runner}): {len(d.targets)} target(s), {len(d.notes)} note(s)")
        for n in d.notes:
            lines.append(f"  {n.kind}: {n.detail}")
    return "\n".join(lines) + "\n"

from __future__ import annotations

from diffcone.manifest import Target
from diffcone.planner import Plan


def py_target(runner_id: str, entry: str, *deps: str) -> Target:
    return Target("pytest", runner_id, entry, tuple(deps))


def asv_target(runner_id: str, entry: str, *deps: str) -> Target:
    return Target("asv", runner_id, entry, tuple(deps))


def selected(plan: Plan) -> set[str]:
    return {d.target.runner_id for d in plan.decisions if d.selected}


def unselected(plan: Plan) -> set[str]:
    return {d.target.runner_id for d in plan.decisions if not d.selected}


def rules(plan: Plan, runner_id: str) -> set[str]:
    for d in plan.decisions:
        if d.target.runner_id == runner_id:
            return {r.rule for r in d.reasons}
    raise KeyError(runner_id)


def reason(plan: Plan, runner_id: str, rule: str = "dependency"):
    for d in plan.decisions:
        if d.target.runner_id == runner_id:
            for r in d.reasons:
                if r.rule == rule:
                    return r
    raise KeyError((runner_id, rule))


def path_ids(reason) -> list[str]:
    """Node ids along a reason's dependency path, source first."""
    if not reason.path:
        return []
    return [reason.path[0].source] + [s.target for s in reason.path]


def changes(plan: Plan) -> dict[str, tuple[str, ...]]:
    return {c.id: c.changes for c in plan.changes}

"""Helpers for writing scenario tests against diffcone.

A scenario is a throwaway git repository built from dicts of file contents,
committed as before/after snapshots, planned, and then checked for exact
target sets and reasons. These helpers are what diffcone's own acceptance
tests use; projects integrating diffcone can use them the same way.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from diffcone.manifest import Manifest, Target, parse_manifest
from diffcone.planner import Plan, Reason, plan

# A hermetic git environment: no user config, deterministic identity.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
}


class FixtureRepo:
    """A throwaway git repository built from dicts of file contents."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def try_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run git and return the completed process, whatever its exit code."""
        return subprocess.run(
            ["git", *args], cwd=self.path, env=GIT_ENV, capture_output=True, text=True
        )

    def git(self, *args: str) -> str:
        """Run git and return stripped stdout; raises on a non-zero exit."""
        proc = self.try_git(*args)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        return proc.stdout.strip()

    def commit(self, files: dict[str, str | None], message: str = "snapshot") -> str:
        """Write (or delete, for ``None``) files and commit. Returns the sha."""
        for rel, content in files.items():
            target = self.path / rel
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, "utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def plan(
        self,
        base: str,
        head: str,
        targets: list[Target] | list[dict],
        source_roots: list[str] | None = None,
        **kwargs,
    ) -> Plan:
        manifest = (
            parse_manifest(targets)
            if targets and isinstance(targets[0], dict)
            else Manifest(list(targets))  # type: ignore[arg-type]
        )
        return plan(self.path, base, head, manifest, source_roots=source_roots, **kwargs)

    def write_manifest(self, targets: list[dict], name: str = "targets.json") -> Path:
        path = self.path.parent / name
        path.write_text(json.dumps({"targets": targets}), "utf-8")
        return path


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


def reason(plan: Plan, runner_id: str, rule: str = "dependency") -> Reason:
    for d in plan.decisions:
        if d.target.runner_id == runner_id:
            for r in d.reasons:
                if r.rule == rule:
                    return r
    raise KeyError((runner_id, rule))


def path_ids(reason: Reason) -> list[str]:
    """Node ids along a reason's dependency path, source first."""
    if not reason.path:
        return []
    return [reason.path[0].source] + [s.target for s in reason.path]


def changes(plan: Plan) -> dict[str, tuple[str, ...]]:
    return {c.id: c.changes for c in plan.changes}

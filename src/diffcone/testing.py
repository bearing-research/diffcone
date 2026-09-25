"""Helpers for writing scenario tests against diffcone.

A scenario is a throwaway git repository built from dicts of file contents,
committed as before/after snapshots, planned, and then checked for exact
target sets and reasons. These helpers are what diffcone's own acceptance
tests use; projects integrating diffcone can use them the same way.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from diffcone.cache import IndexCache
from diffcone.evidence import Evidence
from diffcone.manifest import Manifest, Target, parse_manifest
from diffcone.planner import Plan, Reason, plan
from diffcone.report import to_dict

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
    """A throwaway git repository built from dicts of file contents.

    With ``check_cache`` (diffcone's own suite turns it on) every ``plan``
    call is also planned through a cold and a warm cache in ``cache_dir``
    (default: ``.fixture-cache`` beside the repository) and the reports are
    asserted equal, which triples its cost."""

    def __init__(
        self, path: Path, *, check_cache: bool = False, cache_dir: Path | None = None
    ) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.check_cache = check_cache
        self.cache_dir = Path(cache_dir) if cache_dir else self.path.parent / ".fixture-cache"
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

    def commit(self, files: dict[str, str | bytes | None], message: str = "snapshot") -> str:
        """Write (or delete, for ``None``) files and commit. Returns the sha.
        ``bytes`` content is written as given, for a file whose encoding is
        the point (a PEP 263 coding cookie)."""
        for rel, content in files.items():
            target = self.path / rel
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    target.write_bytes(content)
                else:
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
        result = plan(self.path, base, head, manifest, source_roots=source_roots, **kwargs)
        if self.check_cache and "cache" not in kwargs:
            self._check_cache_invisible(result, base, head, manifest, source_roots, kwargs)
        return result

    def _check_cache_invisible(self, result, base, head, manifest, source_roots, kwargs) -> None:
        """The caches are an optimisation only: a plan served cold (module
        facts and resolution computed and stored) and one served warm (every
        module's facts and resolution loaded, whole-index entries removed so
        the module cache is what answers) must equal the uncached plan."""
        directory = self.cache_dir
        shutil.rmtree(directory, ignore_errors=True)
        expected = json.dumps(to_dict(result), sort_keys=True)
        for warm in (False, True):
            if warm:
                shutil.rmtree(directory / "index", ignore_errors=True)
            cache = IndexCache(directory)
            again = plan(
                self.path, base, head, manifest, source_roots=source_roots, cache=cache, **kwargs
            )
            got = json.dumps(to_dict(again), sort_keys=True)
            assert got == expected, f"{'warm' if warm else 'cold'} cached plan differs"

    def collect(
        self,
        rev: str | None = None,
        *,
        source_roots: list[str] | None = None,
        reverse_check: bool = False,
        command: str | None = None,
        extra: list[str] | None = None,
    ) -> Evidence:
        """Record execution evidence (``diffcone collect``) at ``rev`` (default:
        the clean checkout), running the suite with this interpreter's pytest."""
        from diffcone.execution import collect_evidence  # runs project code

        return collect_evidence(
            self.path,
            command=command or f"{sys.executable} -m pytest",
            source_roots=source_roots or ["."],
            rev=rev,
            reverse_check=reverse_check,
            extra=extra,
        ).evidence

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


def executed(evidence: Evidence, test_id: str) -> set[str]:
    """The symbols a test executed in the evidence run."""
    return evidence.executed(evidence.tests[test_id])


def touched(evidence: Evidence, test_id: str) -> set[str]:
    """The repository paths a test opened, stat'ed or listed in the evidence run."""
    return evidence.touched(evidence.tests[test_id])


def changes(plan: Plan) -> dict[str, tuple[str, ...]]:
    return {c.id: c.changes for c in plan.changes}

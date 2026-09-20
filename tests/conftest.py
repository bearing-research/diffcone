from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from diffcone.manifest import Manifest, Target, parse_manifest
from diffcone.planner import Plan, plan

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
        self.path = path
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.path, env=GIT_ENV, capture_output=True, text=True
        )
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
    ) -> Plan:
        manifest = (
            parse_manifest(targets)
            if targets and isinstance(targets[0], dict)
            else Manifest(list(targets))  # type: ignore[arg-type]
        )
        return plan(self.path, base, head, manifest, source_roots=source_roots)

    def write_manifest(self, targets: list[dict], name: str = "targets.json") -> Path:
        path = self.path.parent / name
        path.write_text(json.dumps({"targets": targets}), "utf-8")
        return path


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    work = tmp_path / "repo"
    work.mkdir()
    return FixtureRepo(work)

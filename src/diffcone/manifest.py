"""Temporary target manifest.

This is an explicit integration boundary for the prototype: it tells the
planner which runnable targets exist, which symbol each one enters through,
and which setup/fixture symbols it depends on. Real pytest and ASV discovery
are future work; naming conventions alone do not implement either.

Format (JSON)::

    {
      "source_roots": ["src", "tests"],          # optional, CLI overrides
      "targets": [
        {
          "runner": "pytest",
          "runner_id": "tests/test_calc.py::test_add",
          "entry_symbol": "tests.test_calc.test_add",
          "lifecycle_dependencies": ["tests.conftest.db"]
        }
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestError(Exception):
    """The manifest is malformed."""


@dataclass(frozen=True, order=True)
class Target:
    runner: str
    runner_id: str
    entry_symbol: str
    lifecycle_dependencies: tuple[str, ...] = ()

    @property
    def node_id(self) -> str:
        return f"target:{self.runner}:{self.runner_id}"


@dataclass
class Manifest:
    targets: list[Target]
    source_roots: list[str] | None = None


def _require_str(obj: dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: {key!r} must be a non-empty string")
    return value


def parse_manifest(data: Any) -> Manifest:
    if isinstance(data, list):
        data = {"targets": data}
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object or a list of targets")
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list):
        raise ManifestError("manifest: 'targets' must be a list")
    source_roots = data.get("source_roots")
    if source_roots is not None and (
        not isinstance(source_roots, list) or not all(isinstance(r, str) for r in source_roots)
    ):
        raise ManifestError("manifest: 'source_roots' must be a list of strings")

    targets: list[Target] = []
    seen: set[tuple[str, str]] = set()
    for i, raw in enumerate(raw_targets):
        where = f"targets[{i}]"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where}: must be an object")
        runner = _require_str(raw, "runner", where)
        runner_id = _require_str(raw, "runner_id", where)
        entry = _require_str(raw, "entry_symbol", where)
        deps = raw.get("lifecycle_dependencies", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) and d for d in deps):
            raise ManifestError(f"{where}: 'lifecycle_dependencies' must be a list of strings")
        key = (runner, runner_id)
        if key in seen:
            raise ManifestError(f"{where}: duplicate target {runner}:{runner_id}")
        seen.add(key)
        targets.append(Target(runner, runner_id, entry, tuple(sorted(set(deps)))))
    return Manifest(targets=targets, source_roots=source_roots)


def load_manifest(path: str | Path) -> Manifest:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers UnicodeDecodeError
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
    return parse_manifest(data)

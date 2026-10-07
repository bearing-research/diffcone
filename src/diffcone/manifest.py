"""Target manifest.

Tells the planner which runnable targets exist, which symbol each one enters
through, and which setup/fixture symbols it depends on: hand-written, or
written by ``diffcone discover`` from static discovery. Unknown keys are
errors, so a misspelt key cannot silently drop dependencies.

Format (JSON)::

    {
      "source_roots": ["src", "tests"],          # optional, CLI overrides; "DIR=PREFIX" allowed
      "targets": [
        {
          "runner": "pytest",
          "runner_id": "tests/test_calc.py::test_add",
          "entry_symbol": "tests.test_calc.test_add",
          "lifecycle_dependencies": ["tests.conftest.db"]
        }
      ],
      "discovery": {...}                         # optional, written by discover
    }

``discovery`` carries the notes discovery made (``runners[].notes``); notes
saying the target list may be short keep a plan made from the manifest at
exit code 3, as a plan that discovered the targets itself would be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    # (runner, kind, detail, path) for each note ``discover`` recorded.
    notes: list[tuple[str, str, str, str]] = field(default_factory=list)


MANIFEST_KEYS = frozenset({"source_roots", "targets", "discovery"})
TARGET_KEYS = frozenset({"runner", "runner_id", "entry_symbol", "lifecycle_dependencies"})


def _unknown_keys(obj: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ManifestError(
            f"{where}: unknown key(s) {', '.join(map(repr, unknown))} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _discovery_notes(discovery: Any) -> list[tuple[str, str, str, str]]:
    if not isinstance(discovery, dict) or not isinstance(discovery.get("runners", []), list):
        raise ManifestError("manifest: 'discovery' must be an object with a 'runners' list")
    notes: list[tuple[str, str, str, str]] = []
    for i, runner in enumerate(discovery.get("runners", [])):
        where = f"discovery.runners[{i}]"
        if not isinstance(runner, dict) or not isinstance(runner.get("notes", []), list):
            raise ManifestError(f"{where}: must be an object with a 'notes' list")
        name = _require_str(runner, "runner", where)
        for j, note in enumerate(runner.get("notes", [])):
            if not isinstance(note, dict):
                raise ManifestError(f"{where}.notes[{j}]: must be an object")
            path = note.get("path", "")
            notes.append(
                (
                    name,
                    _require_str(note, "kind", f"{where}.notes[{j}]"),
                    _require_str(note, "detail", f"{where}.notes[{j}]"),
                    path if isinstance(path, str) else "",
                )
            )
    return notes


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
    _unknown_keys(data, MANIFEST_KEYS, "manifest")
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
        _unknown_keys(raw, TARGET_KEYS, where)
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
    notes = _discovery_notes(data["discovery"]) if "discovery" in data else []
    return Manifest(targets=targets, source_roots=source_roots, notes=notes)


def manifest_to_dict(
    targets: list[Target], source_roots: list[str] | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if source_roots:
        data["source_roots"] = list(source_roots)
    data["targets"] = [
        {
            "runner": t.runner,
            "runner_id": t.runner_id,
            "entry_symbol": t.entry_symbol,
            "lifecycle_dependencies": list(t.lifecycle_dependencies),
        }
        for t in sorted(targets)
    ]
    return data


def load_manifest(path: str | Path) -> Manifest:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers UnicodeDecodeError
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
    return parse_manifest(data)

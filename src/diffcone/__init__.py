"""Diffcone: static-first, function-level change-impact engine for Python."""

from importlib.metadata import PackageNotFoundError, version

from diffcone.manifest import Manifest, Target, load_manifest
from diffcone.planner import Plan, plan

try:
    __version__ = version("diffcone")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0+unknown"

__all__ = ["Manifest", "Plan", "Target", "__version__", "load_manifest", "plan"]

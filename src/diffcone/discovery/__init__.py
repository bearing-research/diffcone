"""Static runner discovery.

Discovery turns a head snapshot into manifest targets without importing or
executing project code. Each runner module documents the subset of its
runner's collection rules that it reproduces; anything outside that subset
surfaces as a note or as an unresolved lifecycle dependency, which the
planner treats conservatively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from diffcone.manifest import Target
from diffcone.model import SourceIndex
from diffcone.snapshot import Snapshot

RUNNERS = ("pytest", "asv")

# Modules the runner's own process imports and runs, whoever the project
# is: when one of them (or anything under it) is in the source roots, the
# runner executes project code for every target it runs. pytest imports
# only ``packaging.version``/``packaging.requirements`` from packaging.
RUNNER_MODULES: dict[str, tuple[str, ...]] = {
    "pytest": (
        "_pytest",
        "pytest",
        "pluggy",
        "iniconfig",
        "exceptiongroup",
        "tomli",
        "colorama",
        "packaging.version",
        "packaging.requirements",
    ),
}


@dataclass(frozen=True, order=True)
class DiscoveryNote:
    runner: str
    kind: str
    detail: str


@dataclass
class DiscoveryOptions:
    # Fixture names supplied by installed plugins that should not be treated
    # as unresolved, in addition to the well-known ones
    # (``pytest_static.WELL_KNOWN_PLUGIN_FIXTURES``).
    external_fixtures: frozenset[str] = frozenset()
    # Consult the well-known plugin fixture table; every assumed name is
    # reported in an ``external_fixture`` discovery note.
    well_known_fixtures: bool = True


# Note kinds that mean the target list may be short of what the runner
# collects. The others are conservative: an unknown fixture or an unparsed
# file makes a target's dependencies wider, never the target list shorter.
INCOMPLETE_NOTE_KINDS = frozenset(
    {"uncollected_test_class", "imported_test_out_of_scope", "unknown_base_class"}
)


@dataclass
class DiscoveryResult:
    runner: str
    targets: list[Target] = field(default_factory=list)
    notes: list[DiscoveryNote] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def incomplete(self) -> list[DiscoveryNote]:
        """Notes saying this runner may collect tests that are not targets."""
        return [n for n in self.notes if n.kind in INCOMPLETE_NOTE_KINDS]


def discover(
    runner: str,
    snapshot: Snapshot,
    index: SourceIndex,
    options: DiscoveryOptions | None = None,
) -> DiscoveryResult:
    options = options or DiscoveryOptions()
    if runner == "pytest":
        from diffcone.discovery.pytest_static import discover_pytest

        return discover_pytest(snapshot, index, options)
    if runner == "asv":
        from diffcone.discovery.asv_static import discover_asv

        return discover_asv(snapshot, index, options)
    raise ValueError(f"unknown runner {runner!r}; expected one of {', '.join(RUNNERS)}")

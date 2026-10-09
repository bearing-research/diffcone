"""Static runner discovery.

Discovery turns a head snapshot into manifest targets without importing or
executing project code. Each runner module documents the subset of its
runner's collection rules that it reproduces; anything outside that subset
surfaces as a note or as an unresolved lifecycle dependency, which the
planner treats conservatively.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
    # The file the note is about, for the notes that mean the target list
    # may be short: evidence mode checks whether it changed since a
    # recording settled the note (roadmap item 9).
    path: str = ""


@dataclass
class DiscoveryOptions:
    # Fixture names supplied by installed plugins that should not be treated
    # as unresolved, in addition to the well-known ones
    # (``pytest_static.WELL_KNOWN_PLUGIN_FIXTURES``).
    external_fixtures: frozenset[str] = frozenset()
    # Consult the well-known plugin fixture table; every assumed name is
    # reported in an ``external_fixture`` discovery note.
    well_known_fixtures: bool = True
    # pytest options the run adds after ``--``, read as if they were in
    # ``addopts`` (``--doctest-modules`` collects doctests; options discovery
    # cannot model make it incomplete).
    runner_args: tuple[str, ...] = ()
    # The rest of what pytest reads besides its configuration file, in the
    # order it reads it (ini ``addopts``, ``PYTEST_ADDOPTS``, the command's
    # own arguments, then ``runner_args``): the ``PYTEST_ADDOPTS`` and
    # ``PYTEST_PLUGINS`` environment variables at plan time, and the pytest
    # arguments written into ``--command`` (``uv run pytest -p plugins.x``).
    env_addopts: tuple[str, ...] = ()
    env_plugins: tuple[str, ...] = ()
    command_args: tuple[str, ...] = ()
    # Why the command's pytest arguments could not be told apart (a command
    # whose shape is not recognised), reported as a note; empty otherwise.
    command_problem: str = ""

    @property
    def pytest_args(self) -> tuple[str, ...]:
        """Every pytest argument besides the configuration's ``addopts``, in
        pytest's order."""
        return self.env_addopts + self.command_args + self.runner_args


# Program names that are pytest itself, as a command's executable or the
# module after ``-m``.
PYTEST_PROGRAMS = frozenset({"pytest", "py.test", "pytest.exe", "py.test.exe"})


def pytest_command_arguments(argv: list[str]) -> tuple[tuple[str, ...], str]:
    """The arguments a command line passes to pytest, and why they could not
    be found (empty when they could): what follows ``-m pytest`` (``python
    -m pytest``, ``coverage run -m pytest``), or else what follows the one
    token naming the pytest program (``pytest``, ``uv run pytest``,
    ``.venv/bin/pytest``). A command where neither is found, or where the
    program is named more than once, is not recognised."""
    for i, token in enumerate(argv[:-1]):
        if token == "-m" and argv[i + 1] in ("pytest", "py.test"):
            return tuple(argv[i + 2 :]), ""
    programs = [
        i
        for i, token in enumerate(argv)
        if token.replace("\\", "/").rsplit("/", 1)[-1].lower() in PYTEST_PROGRAMS
    ]
    if len(programs) == 1:
        return tuple(argv[programs[0] + 1 :]), ""
    shown = " ".join(argv)
    if not programs:
        return (), f"the command {shown!r} names no pytest program (pytest, -m pytest)"
    return (), f"the command {shown!r} names pytest more than once"


def plugin_specs(value: str) -> tuple[str, ...]:
    """The modules ``PYTEST_PLUGINS`` names: a comma-separated list, as
    pytest's ``_get_plugin_specs_as_list`` reads it."""
    return tuple(p.strip() for p in value.split(",") if p.strip())


def relative_arguments(tokens: Iterable[str], repo: Path) -> tuple[str, ...]:
    """``tokens`` with every absolute path inside ``repo`` (a positional
    path, or the value of ``--opt=/abs/path``) made relative to it, as
    pytest, run there, resolves both: discovery matches repository paths."""
    top = repo.resolve()

    def relative(value: str) -> str:
        if not os.path.isabs(value):
            return value
        try:
            inner = Path(value).resolve().relative_to(top).as_posix()
        except (ValueError, OSError):
            return value
        return inner or "."

    out: list[str] = []
    for token in tokens:
        if token.startswith("-") and "=" in token:
            option, value = token.split("=", 1)
            out.append(f"{option}={relative(value)}")
        else:
            out.append(relative(token))
    return tuple(out)


def environment_options(environ: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """``env_addopts`` and ``env_plugins`` for DiscoveryOptions from an
    environment: ``PYTEST_ADDOPTS`` split as pytest splits it (``shlex``),
    ``PYTEST_PLUGINS`` as plugin_specs reads it."""
    raw = environ.get("PYTEST_ADDOPTS", "")
    try:
        addopts = tuple(shlex.split(raw))
    except ValueError:  # an unbalanced quote: pytest fails; be lenient
        addopts = tuple(raw.split())
    return {
        "env_addopts": addopts,
        "env_plugins": plugin_specs(environ.get("PYTEST_PLUGINS", "")),
    }


# Note kinds that mean the target list may be short of what the runner
# collects. The others are conservative: an unknown fixture or an unparsed
# file makes a target's dependencies wider, never the target list shorter.
INCOMPLETE_NOTE_KINDS = frozenset(
    {
        "uncollected_test_class",
        "imported_test_out_of_scope",
        "unknown_base_class",
        "plugin_collects_files",
        # A file pytest collects that could not be parsed or named: whatever
        # tests it holds are not targets.
        "unparsed_file",
        # A file pytest collects lies outside the source roots, so it was
        # never read: its tests are not targets.
        "test_file_outside_roots",
        # A module or class attribute with a test's name bound to something
        # discovery cannot follow (``TestMachine = Machine.TestCase``).
        "unmodelled_test_binding",
        # An option given to pytest after ``--`` that discovery cannot model.
        "unmodelled_runner_option",
        # Evidence mode: pytest collected tests at the recorded commit that
        # were not targets there.
        "collected_not_target",
    }
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

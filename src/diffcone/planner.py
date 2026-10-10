"""Impact planner.

Operates purely on the explicit data structures produced by the snapshot
reader, indexer and classifier. It knows nothing about pytest or ASV: targets
are opaque (runner, runner_id, entry symbol, lifecycle dependencies) records
supplied by the manifest.

Propagation rules (a dependency edge ``X -> Y`` carries impact from Y to X):

* ``references``/``entry``/``lifecycle``/``unresolved_name_match``: any impact
  on Y affects X (behaviour-level).
* ``defined_in``: X is affected only when its container Y is *structurally*
  affected (added, deleted, definition or dependencies changed, a class body
  change, or itself structurally invalidated by its own container). A plain
  body change of a module does not invalidate every member; members that use
  module state carry their own ``references`` edges.
* ``imports``: importing a module runs its import-time code, so any impact
  on the imported module reaches the importer (behaviour-level); deleting
  it invalidates the importer and every member of it (structural).
* ``imports_name``: only a deletion of the imported name propagates
  (structural); the accompanying ``imports`` edge to the module carries
  import-time impact.
* ``references`` with the detail ``mutated_by`` (variable -> writer): a
  changed writer changes what the variable holds (CONTENT), which reaches
  its readers but not a ``writes`` site, code that only empties or adds to
  a builtin container (``REG.clear()``); any other impact on the variable
  reaches those sites too. ``rebinds`` (``mod.X = v``) carries only a
  deletion.

A change that runs at import (a module body change, a variable, a class, a
function's decorators or defaults, an added or deleted definition) also
seeds its module, so every module that transitively imports it is reached,
and every variable a function it calls mutates in place (``mutated_by``),
directly or through what that calls: changing the arguments of an
import-time call changes what the callee leaves behind as a change to its
body would (_import_call_effects). The same holds for any affected code,
whoever runs it: what it hands the functions it calls may differ
(``_store(m + "!")``, ``_store(compute())``), so a variable written in
place depends on ``calls:`` of its writers, a pseudo-node that every
caller reaches (_add_caller_effects; ``called_by`` steps).

Every selection is backed by a concrete edge path or an explicit fallback
rule. See internal/design.md.
"""

from __future__ import annotations

import functools
import gc
import re
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TypeVar

from diffcone.cache import IndexCache
from diffcone.classify import (
    ADDED,
    DEFINITION_CHANGED,
    DELETED,
    DOCSTRING_CHANGED,
    SymbolChange,
    classify,
)
from diffcone.declarations import FILENAME as DECLARATION_FILE
from diffcone.declarations import AlwaysRun, Declaration
from diffcone.declarations import load as load_declarations
from diffcone.discovery import (
    INCOMPLETE_NOTE_KINDS,
    RUNNER_MODULES,
    RUNNERS,
    DiscoveryNote,
    DiscoveryOptions,
    DiscoveryResult,
    discover,
)
from diffcone.evidence import Evidence, EvidenceError, has_commit
from diffcone.indexer import build_index
from diffcone.indexer.scripts import RUNS_SCRIPT
from diffcone.manifest import Manifest, Target
from diffcone.model import (
    ANY_MODULE,
    CLASS,
    DECLARED,
    DEFINED_IN,
    ENTRY,
    EXTERNAL_WRITTEN,
    IMPORTS,
    IMPORTS_NAME,
    INSTALLED,
    INSTALLED_ANYWHERE,
    LIFECYCLE,
    METHOD,
    MODULE,
    REBINDS,
    REFERENCES,
    UNRESOLVED_ATTRIBUTE,
    UNRESOLVED_DYNAMIC,
    UNRESOLVED_NAME_MATCH,
    VARIABLE,
    WRITES,
    AnalysisError,
    Edge,
    SnapshotInfo,
    SourceIndex,
    UnresolvedReference,
    attribute_reachable,
)
from diffcone.snapshot import (
    BUILD_SCRIPTS,
    CONFIG_FILES,
    INDEX,
    PROJECT_BUILD_SCRIPTS,
    WORKTREE,
    GitError,
    Snapshot,
    changed_paths,
    commit_description,
    file_id,
    read_snapshot,
    resolve_commit,
    split_root,
)

# Impact modes, weakest first. REGISTERED: what a registry holds changed (a
# function ``@app.command`` registered): code that later calls the registry
# behaves differently, the import-time code that registers does not.
# CONTENT: what a variable holds changed in place (a writer of it changed,
# a ``mutated_by`` edge): code reading it behaves differently, code that only
# empties or adds to it (a ``writes`` edge: ``X.clear()``) does not.
REGISTERED = 1
CONTENT = 2
BEHAVIOR = 3
STRUCTURAL = 4

RULE_DEPENDENCY = "dependency"
RULE_UNRESOLVED_NAME_MATCH = "unresolved_name_match"
RULE_DYNAMIC_REFERENCE = "dynamic_reference"
RULE_ENTRY_UNRESOLVED = "entry_symbol_unresolved"
RULE_LIFECYCLE_UNRESOLVED = "lifecycle_dependency_unresolved"
RULE_ANALYSIS_ERROR = "analysis_error"
RULE_RUNNER_DEPENDENCY = "runner_dependency"
RULE_ENTRY_DOCSTRING = "entry_docstring_changed"
RULE_DECLARED_DEPENDENCY = "declared_dependency"
# A target diffcone.toml says to run on every change ([[always_run]]).
RULE_ALWAYS_RUN = "always_run"
RULE_NEW_TARGET = "new_target"
# A discovered target whose lifecycle dependencies differ between the
# snapshots: a fixture, hook or plugin now applies to it, or no longer does,
# although nothing it depends on changed (``pytest_plugins`` added in another
# test module registers a plugin for the whole session).
RULE_LIFECYCLE_CHANGED = "lifecycle_changed"
RULE_UNANALYSED_FILE = "unanalysed_file_changed"
# Evidence mode (evidence_plan.py). ``escalated`` is a selection made by
# static planning of a change that evidence cannot bound.
RULE_ESCALATED = "escalated"
RULE_EXECUTED_CHANGED = "executed_changed"
RULE_EXECUTED_READER = "executed_reader"
RULE_LOOKUP_SITE = "lookup_site"
RULE_TOUCHED_FILE = "touched_file"
RULE_TEST_SCOPE = "test_scope"
RULE_CHANGED_TARGET = "changed_target"
RULE_NO_EVIDENCE = "no_evidence"
RULE_UNSTABLE = "unstable"
RULE_SUBPROCESS = "subprocess"
# Evidence: the test ran code compiled from text no file holds (a doctest).
RULE_TEXT_CODE = "text_code"
RULE_PYTEST_HOOK = "pytest_hook_changed"
RULE_UNOBSERVED_FILE = "unobserved_file_changed"
RULE_UNINDEXED_IMPORT = "unindexed_import"
# A test executed a Cython function that names a changed nogil or cpdef one,
# which a profiled build does not report itself (roadmap item 7).
RULE_CYTHON_CALLER = "cython_caller"
# A declared endpoint that names a container stands for everything in it;
# beyond this many pairs the declaration is too coarse to be useful.
DECLARATION_FANOUT = 5000

# A lifecycle dependency ``dynamic:<module>`` says the target runs code with
# that module's globals (a doctest): it is affected by any impact-carrying
# change in the module's import closure, as a dynamic reference is.
DYNAMIC_DEPENDENCY = "dynamic:"

CONSERVATIVE_RULES = frozenset(
    {
        RULE_UNRESOLVED_NAME_MATCH,
        RULE_DYNAMIC_REFERENCE,
        RULE_ENTRY_UNRESOLVED,
        RULE_LIFECYCLE_UNRESOLVED,
        RULE_ANALYSIS_ERROR,
        RULE_RUNNER_DEPENDENCY,
        RULE_UNANALYSED_FILE,
        RULE_LOOKUP_SITE,
        RULE_NO_EVIDENCE,
        RULE_UNSTABLE,
        RULE_SUBPROCESS,
        RULE_TEXT_CODE,
        RULE_PYTEST_HOOK,
        RULE_UNOBSERVED_FILE,
        RULE_UNINDEXED_IMPORT,
    }
)

# Files under the source roots that are diffcone's own, not the project's:
# the cache, and the declarations the planner already reads from both
# revisions.
OWN_FILES = ("diffcone.toml",)
# A directory holding one of these is a project's: a ``build.py`` there is
# its build script (snapshot.PROJECT_BUILD_SCRIPTS, is_build_script).
PROJECT_FILES = ("pyproject.toml", "setup.cfg")
OWN_DIRS = (".diffcone/",)
UNANALYSED_PATHS_SHOWN = 5

# Files no dependency edge or test record can see read (build_input):
# sources compiled into extensions, and what pytest or the build reads
# before any test runs.
COMPILED_SUFFIXES = (
    ".pyx",
    ".pxd",
    ".pxi",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hh",
    ".hpp",
    ".f",
    ".f77",
    ".for",
    ".f90",
    ".f95",
    ".pyf",
    ".rs",
    ".cu",
    ".i",
    ".swg",
    ".m",
    ".mm",
    ".src",
    ".in",
    ".tpl",
    ".so",
    ".pyd",
    ".dylib",
    ".dll",
)
BUILD_FILES = frozenset(
    name.lower()
    for name in (
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "pytest.toml",
        ".pytest.toml",
        "pytest.ini",
        ".pytest.ini",
        "uv.toml",
        "hatch.toml",
        "poetry.toml",
        "pdm.toml",
        ".pdm.toml",
        "MANIFEST.in",
        "meson.build",
        "meson.options",
        "meson_options.txt",
        "CMakeLists.txt",
        "Makefile",
        "GNUmakefile",
        "Pipfile",
        "pixi.toml",
        ".python-version",
        ".coveragerc",
        "Cargo.toml",
        "build.rs",
        # Checkers that pytest plugins run as tests (pytest-mypy,
        # pytest-ruff, pytest-flake8, pytest-pylint).
        "mypy.ini",
        ".mypy.ini",
        "pyrightconfig.json",
        "ruff.toml",
        ".ruff.toml",
        ".flake8",
        ".pylintrc",
        "pylintrc",
        # Environment variables a test session may load (pytest-dotenv,
        # pipenv, python-dotenv in the project's own code).
        ".env",
        # Continuous integration: which Python, which packages, which command.
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
        ".travis.yml",
        "appveyor.yml",
        # Build scripts are Python, but nobody imports them.
        *BUILD_SCRIPTS,
        *PROJECT_BUILD_SCRIPTS,
    )
)
# Directories whose files configure continuous integration.
CI_DIRS = (".github/workflows/", ".github/actions/", ".circleci/", "ci/", ".ci/")
# Dependency declarations, by a word in their name or in the name of a
# directory holding them, in any case: ``requirements-dev.txt``,
# ``dev-requirements.txt``, ``Requirements.txt``, ``requirements.pip``,
# ``requirements/dev.txt``, ``constraints.txt``, ``environment.yml``,
# ``conda-lock.yml``, pandas' ``ci/deps/actions-311.yaml``.
DEPENDENCY_SUFFIXES = (".txt", ".in", ".pip", ".yml", ".yaml")
DEPENDENCY_NAME = re.compile(
    r"requirement|constraint|^environment|conda-lock|(?:^|[-_.])(?:deps|dependencies)(?:[-_.]|$)"
)
# Directories of a distribution's metadata (``pkg-1.0.dist-info``).
DISTRIBUTION_SUFFIXES = (".dist-info", ".egg-info", ".egg", ".egg-link")


def build_input(path: str) -> bool:
    """Whether a file decides what is compiled, installed or collected, or
    how the tests run: a compiled source; build, pytest or task-runner
    configuration; a dependency declaration or lock file (``uv.lock``,
    ``pylock.toml``); a continuous-integration file; a ``.env`` file; a
    ``.pth`` file (once installed, the interpreter runs its import lines at
    start); or a distribution's metadata (under ``*.dist-info``,
    ``*.egg-info``, ``*.egg`` or ``*.egg-link``, which ``importlib.metadata``
    and ``pkg_resources`` find by name on ``sys.path``). Nothing the index or
    a test's record sees reads it, so a change to one anywhere selects every
    target. Names are compared without case: a case-insensitive filesystem
    finds ``Requirements.txt`` under any spelling."""
    lowered = path.lower()
    parts = lowered.split("/")
    name = parts[-1]
    return (
        lowered.endswith(COMPILED_SUFFIXES)
        or any(part.endswith(DISTRIBUTION_SUFFIXES) for part in parts)
        or name in BUILD_FILES
        or name.startswith(".env.")
        or name.endswith((".lock", ".pth"))
        or (name.startswith("pylock.") and name.endswith(".toml"))
        or lowered.startswith(CI_DIRS)
        or (
            name.endswith(DEPENDENCY_SUFFIXES)
            and any(DEPENDENCY_NAME.search(part) for part in parts)
        )
    )


def _project_dirs(base: SourceIndex, head: SourceIndex) -> set[str]:
    """Directories under the roots holding a project's metadata
    (``pyproject.toml``, ``setup.cfg``) in either revision."""
    return {
        path.rpartition("/")[0]
        for index in (base, head)
        for path in index.other_files
        if path.rpartition("/")[2] in PROJECT_FILES
    }


def is_build_script(path: str, project_dirs: set[str] | frozenset[str] = frozenset()) -> bool:
    """Whether a Python file under the roots is a build script, a task
    runner's file or an interpreter start-up module (BUILD_SCRIPTS): nobody
    imports it, but it decides what is installed or how the tests run, so a
    change to one is as unbounded as a changed compiled source. A
    ``build.py`` counts at the repository root or in one of
    ``project_dirs`` (_project_dirs)."""
    directory, _, name = path.rpartition("/")
    if name in BUILD_SCRIPTS:
        return True
    return name in PROJECT_BUILD_SCRIPTS and (directory == "" or directory in project_dirs)


def _changed_unanalysed_files(base: SourceIndex, head: SourceIndex) -> list[str]:
    """Non-Python files under the source roots whose content differs between
    the snapshots (added, deleted or edited). The index reads none of them,
    so it cannot say who depends on one: a data file the code opens, a
    compiled extension's source, a configuration file."""
    paths = base.other_files.keys() | head.other_files.keys()
    return sorted(
        p
        for p in paths
        if base.other_files.get(p) != head.other_files.get(p)
        and p not in OWN_FILES
        and not p.startswith(OWN_DIRS)
    )


def _runner_files_outside_roots(
    repo: Path, base: SourceIndex, head: SourceIndex, roots: list[str]
) -> list[str]:
    """Changed files outside the source roots that decide what is installed
    and how the tests run: the runners' configuration at the repository root
    (``pyproject.toml``'s pytest table, ``asv.conf.json`` anywhere),
    conftests, and anything ``build_input`` names (build scripts, dependency
    declarations and lock files, compiled sources). Inside a root such a
    file is an unanalysed file; out of every root nothing else would see it
    change."""
    if "" in (split_root(r)[0] for r in roots):
        return []
    if base.snapshot.committed:
        fixed, other = base.snapshot.commit, head.snapshot
    elif head.snapshot.committed:
        fixed, other = head.snapshot.commit, base.snapshot
    elif base.snapshot.kind == head.snapshot.kind:
        return []  # the same uncommitted state on both sides
    else:  # the index and the working tree
        fixed = INDEX
        other = head.snapshot if head.snapshot.is_worktree else base.snapshot
    changed = changed_paths(repo, fixed, other.commit, other.kind)
    dirs = [split_root(r)[0] for r in roots]
    return sorted(
        path
        for path in changed
        if not any(path.startswith(d + "/") for d in dirs)
        and (
            path in CONFIG_FILES
            or build_input(path)
            or PurePosixPath(path).name in ("conftest.py", "asv.conf.json")
        )
    )


@dataclass(frozen=True)
class Seeds:
    """Where a restricted search starts (evidence mode's escalation): the
    changes to plan from, plus other nodes with the reason each one is a
    starting point (a module whose import ran changed code). A restricted
    search adds no dynamic-reference pseudo-seeds, no unanalysed-file
    fallback and no target-level rules (new target, entry docstring,
    ``dynamic:`` dependencies): the caller decides those."""

    changes: frozenset[str] = frozenset()
    nodes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    source: str
    target: str
    kind: str
    detail: str
    revisions: tuple[str, ...]


@dataclass(frozen=True)
class Reason:
    rule: str
    detail: str
    path: tuple[Step, ...] = ()
    changed_symbol: str | None = None
    changes: tuple[str, ...] = ()

    @property
    def conservative(self) -> bool:
        return self.rule in CONSERVATIVE_RULES


@dataclass
class Decision:
    target: Target
    selected: bool
    reasons: list[Reason]
    affected_dependencies: list[str]
    unselected_reason: str | None = None


@dataclass(frozen=True)
class Fallback:
    rule: str
    scope: str  # "all_targets" | "target"
    detail: str
    target: str | None = None


@dataclass(frozen=True)
class UnresolvedRecord:
    symbol: str
    kind: str
    name: str
    detail: str
    revisions: tuple[str, ...]
    matched_affected_symbols: tuple[str, ...]


@dataclass
class Plan:
    repo: str
    source_roots: list[str]
    changes: list[SymbolChange]
    decisions: list[Decision]
    fallbacks: list[Fallback]
    unresolved: list[UnresolvedRecord]
    errors: list[AnalysisError]
    base_index: SourceIndex = field(repr=False)
    head_index: SourceIndex = field(repr=False)
    discovery: list[DiscoveryResult] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    declarations: list[Declaration] = field(default_factory=list)
    always_run: list[AlwaysRun] = field(default_factory=list)
    always_run_matched: dict[AlwaysRun, int] = field(default_factory=dict)
    # Evidence mode: which store planned the pytest targets, and from where
    # (evidence_plan._summary); None for a static plan.
    evidence: dict | None = None

    @property
    def selected(self) -> list[Decision]:
        return [d for d in self.decisions if d.selected]

    @property
    def unselected(self) -> list[Decision]:
        return [d for d in self.decisions if not d.selected]

    @property
    def degraded(self) -> bool:
        return bool(self.errors)

    @property
    def incomplete_discovery(self) -> list:
        """Discovery notes saying a runner may collect tests that are not
        targets. A degraded plan runs too much; this runs too little, so the
        two are reported (and exited) separately."""
        return [n for d in self.discovery for n in d.incomplete]

    @property
    def base(self) -> SnapshotInfo:
        return self.base_index.snapshot

    @property
    def head(self) -> SnapshotInfo:
        return self.head_index.snapshot

    @property
    def uncommitted_analyzed(self) -> bool:
        """True when either snapshot is the index or the working tree."""
        return not (self.base.committed and self.head.committed)

    @property
    def working_tree_analyzed(self) -> bool:
        return self.base.is_worktree or self.head.is_worktree

    @property
    def scope_statement(self) -> str:
        """One sentence saying exactly what was analysed."""
        if not self.uncommitted_analyzed:
            return "two committed snapshots; the working tree was not analyzed"
        sides = [
            name for name, info in (("base", self.base), ("head", self.head)) if not info.committed
        ]
        return f"UNCOMMITTED state was analyzed as {' and '.join(sides)}; see base/head"


# --------------------------------------------------------------------------- graph


@dataclass
class _Graph:
    """Union of both revisions' edges, indexed for backward traversal."""

    reverse: dict[str, list[tuple[str, Edge, tuple[str, ...]]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    forward: dict[str, list[tuple[str, Edge, tuple[str, ...]]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def add(self, edge: Edge, revisions: tuple[str, ...]) -> None:
        self.reverse[edge.target].append((edge.source, edge, revisions))
        self.forward[edge.source].append((edge.target, edge, revisions))

    def freeze(self) -> None:
        # A plain tuple key: dataclass ordering on Edge is far slower.
        for adj in (self.reverse, self.forward):
            for key in adj:
                adj[key].sort(
                    key=lambda item: (item[0], item[1].kind, item[1].detail, item[1].target)
                )


T = TypeVar("T")


def _union(base_items: Iterable[T], head_items: Iterable[T]) -> dict[T, tuple[str, ...]]:
    """Merge two revisions' items, tagging each with the revisions it appears in.

    Insertion order is not significant: adjacency lists are sorted in
    ``_Graph.freeze`` and records are sorted before reporting, so the items
    are not sorted here (comparing tens of thousands of dataclasses was a
    third of planning time)."""
    revs: dict[T, list[str]] = defaultdict(list)
    for item in base_items:
        revs[item].append("base")
    for item in head_items:
        revs[item].append("head")
    return {item: tuple(r) for item, r in revs.items()}


def _propagate(
    edge: Edge, target_mode: int, target_change: SymbolChange | None, source_is_module: bool
) -> int | None:
    if edge.kind == DEFINED_IN:
        return STRUCTURAL if target_mode == STRUCTURAL else None
    if edge.detail == "registers":
        return REGISTERED
    if edge.detail == REBINDS:
        # ``mod.X = v`` needs ``X`` to exist (a deletion), not its value.
        return STRUCTURAL if target_mode == STRUCTURAL else None
    if edge.detail == WRITES and target_mode <= CONTENT:
        return None  # emptying or adding to a container reads nothing of it
    if target_mode == REGISTERED and source_is_module:
        return None  # registering ran at import, unchanged
    if edge.kind in (IMPORTS, IMPORTS_NAME):
        if target_change is not None and DELETED in target_change.changes:
            return STRUCTURAL
        # Importing a module runs its import-time code.
        return BEHAVIOR if edge.kind == IMPORTS else None
    if edge.detail == MUTATED_BY:
        return CONTENT
    return BEHAVIOR


def _runs_at_import(change: SymbolChange) -> bool:
    """Module bodies, variable initialisers and class bodies run at import;
    so does a ``def`` statement's decorators, defaults and (when evaluated
    eagerly) annotations. A function body does not (what import-time code
    calls is reached through its edges), and an inert ``def`` at both
    revisions only binds a name (removing or rebinding it reaches its users
    through deletion and resolution edges)."""
    symbol = change.symbol
    if symbol.kind in (MODULE, CLASS):
        return True
    if symbol.kind == VARIABLE:
        # Binding a literal runs nothing; a computed value does, and so does
        # an eagerly evaluated annotation that changed (definition_changed).
        if DEFINITION_CHANGED in change.changes:
            return True
        return not all(s.inert_definition for s in (change.base, change.head) if s is not None)
    if not {ADDED, DELETED, DEFINITION_CHANGED} & set(change.changes):
        return False
    return not all(s.inert_definition for s in (change.base, change.head) if s is not None)


def _sides(change: SymbolChange) -> tuple[str, ...]:
    """The revisions a changed symbol exists in."""
    return tuple(r for r, s in (("base", change.base), ("head", change.head)) if s is not None)


# Explanation steps that are not index edges: a module runs a changed
# symbol when it is imported (_runs_at_import), and import-time code calls a
# function (_import_call_effects).
RUNS_AT_IMPORT = "runs_at_import"
# A symbol reading docstrings (``f.__doc__``) of something whose docstring
# changed.
READS_DOCSTRING = "reads_docstring"
CALLED_AT_IMPORT = "called_at_import"
CALLED_BY = "called_by"
MUTATED_BY = "mutated_by"
# What a call made at import can run: what the caller references (a
# function, a class's constructor and special methods, a variable holding a
# function), what an unresolved call may be, what the project declares.
_CALL_KINDS = frozenset({REFERENCES, UNRESOLVED_NAME_MATCH, DECLARED})
# Edge details that are no call: a write in place, an object installed in
# ``sys.modules``.
_NO_CALL = frozenset({MUTATED_BY, INSTALLED, INSTALLED_ANYWHERE})


def _import_call_effects(
    graph: _Graph, changes: list[SymbolChange], module_nodes: set[str]
) -> dict[str, tuple[tuple[Step, ...], str]]:
    """The state a changed piece of import-time code can now leave
    different: every variable mutated in place (a ``mutated_by`` edge) by a
    function the changed code calls, directly or through what that calls.
    ``X = set_mode("faster")`` leaves ``MODE`` holding something else
    although ``set_mode`` did not change. Each variable comes with the steps
    from it to the change and the rule they amount to (a name match on the
    way makes it ``unresolved_name_match``, a declared edge
    ``declared_dependency``).

    The changed code's references stand for what it calls: nothing tells a
    call from a read, and a function's decorators, defaults and body share
    one symbol, so this over-approximates. A module the code refers to is
    not entered: referring to it runs none of its code."""
    parent: dict[str, tuple[str, Edge, tuple[str, ...]] | None] = {}
    queue: deque[str] = deque()
    for change in sorted(changes, key=lambda c: c.id):
        if change.id not in parent:
            parent[change.id] = None
            queue.append(change.id)
    effects: dict[str, tuple[tuple[Step, ...], str]] = {}
    while queue:
        node = queue.popleft()
        if not _is_name_node(node):
            for source, edge, revs in graph.reverse.get(node, ()):
                if edge.detail == MUTATED_BY and source not in effects:
                    first = Step(source, node, edge.kind, edge.detail, revs)
                    effects[source] = _effect_path(first, parent)
        if node in module_nodes and parent[node] is not None:
            continue
        for target, edge, revs in graph.forward.get(node, ()):
            if edge.kind in _CALL_KINDS and edge.detail not in _NO_CALL and target not in parent:
                parent[target] = (node, edge, revs)
                queue.append(target)
    return effects


# ``calls:Y``: Y may now be called differently (see _add_caller_effects).
CALLS = "calls:"
CALLER = "caller"


def _add_caller_effects(graph: _Graph, module_nodes: set[str]) -> None:
    """A function that writes state in place leaves it holding what its
    callers handed it: when a caller behaves differently (it changed, or
    something it depends on did), it may hand the writer something else
    (``_store(m + "!")``, ``_store(compute())``), or call it more or less
    often. The pseudo-node ``calls:Y`` stands for "Y may now be called
    differently": it depends on every caller of Y and on ``calls:`` of each
    caller, along the edges a call can take (references, name matches,
    declared edges; a module is not entered: referring to one runs none of
    its code); a variable a function writes in place depends on ``calls:``
    of the writer as it depends on the writer. So a variable is reached
    whenever the behaviour of anything that can call one of its writers,
    directly or through what it calls, may differ. The edges keep the kind
    of the call edge they come from, so a name match on the way shows in the
    rule; explanations show them as ``called_by`` steps (_call_steps)."""
    added: list[tuple[Edge, tuple[str, ...]]] = []
    for source, adj in list(graph.forward.items()):
        for target, edge, revs in adj:
            if edge.kind not in _CALL_KINDS or edge.detail in _NO_CALL:
                continue
            if target in module_nodes:
                continue
            detail = f"{CALLER}:{edge.detail}" if edge.kind == DECLARED else CALLER
            if not _is_name_node(source):
                added.append((Edge(CALLS + target, source, edge.kind, detail), revs))
            if source not in module_nodes:
                added.append((Edge(CALLS + target, CALLS + source, edge.kind, detail), revs))
    for target, adj in list(graph.reverse.items()):
        for source, edge, revs in adj:
            if edge.detail == MUTATED_BY:
                added.append((Edge(source, CALLS + target, edge.kind, MUTATED_BY), revs))
    for edge, revs in added:
        graph.add(edge, revs)


def _call_steps(steps: list[Step]) -> list[Step]:
    """Explanation steps through ``calls:`` pseudo-nodes as real symbols:
    ``Y -[called_by]-> X`` for "X calls Y", a name match collapsed into the
    step's detail."""
    out: list[Step] = []
    pending: str | None = None  # the name a ``calls:name:`` node matched on
    for step in steps:
        source, target = step.source, step.target
        if not (source.startswith(CALLS) or target.startswith(CALLS)):
            out.append(step)
            continue
        source = source.removeprefix(CALLS)
        target = target.removeprefix(CALLS)
        if step.detail == MUTATED_BY:
            out.append(Step(source, target, REFERENCES, MUTATED_BY, step.revisions))
            continue
        if _is_name_node(source) and out and out[-1].target == source:
            source = out.pop().source
        if _is_name_node(target):
            pending = _matched_name(target)
            out.append(Step(source, target, CALLED_BY, "", step.revisions))
            continue
        detail = f"by name match on {pending!r}" if pending is not None else ""
        if step.detail.startswith(f"{CALLER}:"):
            detail = "declared"
        out.append(Step(source, target, CALLED_BY, detail, step.revisions))
        pending = None
    return out


def _effect_path(
    first: Step, parent: dict[str, tuple[str, Edge, tuple[str, ...]] | None]
) -> tuple[tuple[Step, ...], str]:
    """The steps from a mutated variable back to the changed code whose call
    reached its mutator (_import_call_effects), name-match pseudo-nodes
    collapsed, and the rule they amount to."""
    steps = [first]
    rule = RULE_DEPENDENCY
    node = first.target
    pending: str | None = None  # the name a pseudo-node matched on
    while (link := parent.get(node)) is not None:
        caller, edge, revs = link
        if edge.kind == UNRESOLVED_NAME_MATCH:
            rule = RULE_UNRESOLVED_NAME_MATCH
        elif edge.kind == DECLARED and rule == RULE_DEPENDENCY:
            rule = RULE_DECLARED_DEPENDENCY
        if _is_name_node(caller):
            pending = _matched_name(caller)
            node = caller
            continue
        if pending is not None:
            detail = f"by name match on {pending!r}"
        else:
            detail = "declared" if edge.kind == DECLARED else ""
        steps.append(Step(steps[-1].target, caller, CALLED_AT_IMPORT, detail, revs))
        pending = None
        node = caller
    return tuple(steps), rule


# --------------------------------------------------------------------------- planning


def merge_targets(manifest: Manifest | None, discovered: list[DiscoveryResult]) -> list[Target]:
    """Manifest targets win over discovered ones with the same runner and id."""
    merged: dict[tuple[str, str], Target] = {}
    for result in discovered:
        for target in result.targets:
            merged.setdefault((target.runner, target.runner_id), target)
    if manifest is not None:
        for target in manifest.targets:
            merged[(target.runner, target.runner_id)] = target
    return sorted(merged.values())


def _members_by_container(symbols: set[str]) -> dict[str, list[str]]:
    """Symbols inside each module or class, by qualified name. Identity is the
    dotted path, so a container's members are the symbols under its prefix."""
    members: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols:
        prefix = symbol
        while "." in prefix:
            prefix = prefix.rsplit(".", 1)[0]
            if prefix in symbols:
                members[prefix].append(symbol)
    return members


def _runner_only_classes(
    base: SourceIndex,
    head: SourceIndex,
    discovered: list[DiscoveryResult],
    edges: dict[Edge, tuple[str, ...]],
) -> set[str]:
    """Test classes whose instances only the test runner ever holds.

    A call ``obj.m()`` reaches ``C.m`` only if ``obj`` is an instance of C or
    of a subclass. pytest instantiates a test class to run its tests; if the
    analysed code never constructs it, never refers to it as a value and
    never hands an instance on, those are the only instances, and they never
    leave the class's own methods. Nothing outside it can be holding one, so
    no name-matched call from outside can land on its members. (Calls on
    ``self`` inside it are resolved through the MRO and are not name matches.)

    pandas is why this matters: its library code says ``x.dtype``,
    ``x.index``, ``x.copy`` thousands of times, and every one of those matched
    a fixture or helper of the same name on some test class -- one of which
    held a dynamic reference that every test reached through ``DataFrame``.

    A class counts as runner-instantiated when it owns the entry of a pytest
    target, or is the collecting class a target lists among its lifecycle
    dependencies. It is *held* -- and keeps its members as candidates -- when
    an instance or the class is passed on (``escaped_classes``), when a
    symbol outside every runner class refers to it, or when a held class
    inherits from it. A read of an attribute named ``instance`` anywhere is
    pytest's ``request.instance``, the one channel that hands a test instance
    to other code, and turns the rule off. (Evidence mode has its own
    version, evidence_plan._runner_only.)"""
    symbols = {**base.symbols, **head.symbols}
    runner: set[str] = set()
    for result in discovered:
        if result.runner != "pytest":
            continue
        for target in result.targets:
            entry = symbols.get(target.entry_symbol)
            if entry is not None and entry.kind == METHOD and entry.container:
                runner.add(entry.container)
            for dep in target.lifecycle_dependencies:
                owner = symbols.get(dep)
                if owner is not None and owner.kind == CLASS:
                    runner.add(dep)
    if not runner:
        return set()
    for index in (base, head):
        if any(u.kind != UNRESOLVED_DYNAMIC and u.name == "instance" for u in index.unresolved):
            return set()

    def within_runner(symbol_id: str) -> bool:
        return _inside(symbol_id, runner, base, head)

    held = (base.escaped_classes | head.escaped_classes) & runner
    for edge in edges:
        if edge.kind == REFERENCES and edge.target in runner and not within_runner(edge.source):
            held.add(edge.target)
    # A held subclass holds its bases too: its instances carry their methods.
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if (
                edge.kind == REFERENCES
                and edge.source in held
                and edge.target in runner
                and edge.target not in held
            ):
                held.add(edge.target)
                changed = True
    return runner - held


def _inside(symbol_id: str, classes: set[str], base: SourceIndex, head: SourceIndex) -> bool:
    """Whether ``symbol_id`` is one of ``classes`` or sits inside one."""
    current: str | None = symbol_id
    while current:
        if current in classes:
            return True
        symbol = head.symbols.get(current) or base.symbols.get(current)
        current = symbol.container if symbol is not None else None
    return False


def plan_from_indexes(
    base: SourceIndex,
    head: SourceIndex,
    manifest: Manifest | None,
    *,
    repo: str = "",
    source_roots: list[str] | None = None,
    discovered: list[DiscoveryResult] | None = None,
    declarations: list[Declaration] | None = None,
    base_target_ids: set[str] | None = None,
    seeds: Seeds | None = None,
    runner_files: Iterable[str] = (),
    lifecycle_changes: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
) -> Plan:
    """``runner_files``: changed runner configuration outside the source
    roots (_runner_files_outside_roots), which selects every target.
    ``lifecycle_changes``: (runner, runner_id) -> (added, removed) lifecycle
    dependencies of a discovered target between the snapshots
    (diff_lifecycles), which selects it."""
    discovered = list(discovered or []) + _manifest_notes(manifest, discovered or [])
    discovered_ids = {t.runner_id for result in discovered for t in result.targets}
    declared = list(declarations or [])
    targets = merge_targets(manifest, discovered)
    changes = classify(base, head)
    change_by_id = {c.id: c for c in changes}
    known_symbols = set(base.symbols) | set(head.symbols)
    fallbacks: list[Fallback] = []
    errors = sorted(base.errors + head.errors)

    graph = _Graph()
    union = _union(base.edges, head.edges)
    if seeds is not None:
        # Evidence mode's escalation: the record shows what an object
        # installed under a name nothing bounds ran (W25).
        union = {e: r for e, r in union.items() if e.detail != INSTALLED_ANYWHERE}
    for edge, revs in union.items():
        graph.add(edge, revs)

    # An instance handed to someone else can have any attribute read off it by
    # a name nothing resolves (``invoke(obj, name)``), and no static rule can
    # say which. So referring to such a class depends on its members, not only
    # on its structure.
    class_members = _members_by_container(set(base.symbols) | set(head.symbols))
    for cls in sorted(base.escaped_classes | head.escaped_classes):
        for member in class_members.get(cls, ()):
            graph.add(
                Edge(cls, member, REFERENCES, "attribute of a class passed to other code"),
                ("base", "head"),
            )
    # So can a module handed on as a value (``read(ops, name)`` reading
    # ``getattr(obj, name)``): what refers to it depends on its members. The
    # edges go from each referrer, not from the module, whose importers an
    # impact on it would reach.
    escaped_modules = base.escaped_modules | head.escaped_modules
    # What such a module's imports bind is as reachable off it (``api.core``,
    # ``api.TABLE``; audit round 3, W16): referrers reach it through one node
    # per module (``<module>.<imports>``), which depends on every reached
    # symbol and module member, so the edges stay one per referrer.
    module_reach: dict[str, set[str]] = defaultdict(set)
    for index in (base, head):
        for module, found in index.module_reach.items():
            module_reach[module].update(found)
    reached: set[str] = set()
    for edge, revs in sorted(union.items()):
        if edge.kind == REFERENCES and edge.target in escaped_modules:
            for member in class_members.get(edge.target, ()):
                if member != edge.source:
                    graph.add(
                        Edge(
                            edge.source,
                            member,
                            REFERENCES,
                            "attribute of a module passed to other code",
                        ),
                        revs,
                    )
            if module_reach.get(edge.target):
                node = f"{edge.target}.<imports>"
                graph.add(
                    Edge(
                        edge.source, node, REFERENCES, "attribute of a module passed to other code"
                    ),
                    revs,
                )
                reached.add(edge.target)
    for module in sorted(reached):
        node = f"{module}.<imports>"
        for target in sorted(module_reach[module]):
            for symbol in [target, *class_members.get(target, ())]:
                graph.add(
                    Edge(node, symbol, REFERENCES, f"what {module} imports binds"),
                    ("base", "head"),
                )

    # Dependencies the project declares (diffcone.toml): the analysis cannot
    # see them, and they only add edges, so they widen selection and never
    # narrow it. An endpoint that is in neither revision is an analysis
    # error: a declaration that silently does nothing is worth failing on.
    # An endpoint that names a module or a class means everything in it, so
    # it expands to that container's members -- a change to one of them is
    # what the declaration is about, and the container node alone would
    # never see it.
    members = _members_by_container(known_symbols)
    for decl in declared:
        missing = [e for e in (decl.source, decl.target) if e not in known_symbols]
        if missing:
            errors.append(
                AnalysisError(
                    revision=head.snapshot.revision,
                    path=DECLARATION_FILE,
                    message=(
                        f"declared edge {decl.source!r} -> {decl.target!r} names "
                        f"{' and '.join(repr(m) for m in missing)}, which is in neither revision"
                    ),
                )
            )
            continue
        from_side = [decl.source, *members.get(decl.source, ())]
        to_side = [decl.target, *members.get(decl.target, ())]
        if len(from_side) * len(to_side) > DECLARATION_FANOUT:
            errors.append(
                AnalysisError(
                    revision=head.snapshot.revision,
                    path=DECLARATION_FILE,
                    message=(
                        f"declared edge {decl.source!r} -> {decl.target!r} joins "
                        f"{len(from_side)} and {len(to_side)} symbols, more than "
                        f"{DECLARATION_FANOUT} pairs; declare the symbols that depend "
                        "on each other instead"
                    ),
                )
            )
            continue
        for source in from_side:
            for target in to_side:
                if source != target:
                    graph.add(Edge(source, target, DECLARED, decl.detail), ("declared",))

    # Conservative edges from unresolved references: ``obj.run()`` may be any
    # known ``run`` (function, method or class) in either revision, and
    # impact flows through the graph as usual (matching only *changed*
    # symbols would miss a ``run`` that is unchanged but calls something that
    # changed). Each name gets one pseudo-node ``name:<n>`` so the edge count
    # is linear in references plus symbols. Dunder names (``__init__``,
    # ``__eq__``) are excluded: they exist on nearly every class and bound
    # nothing; constructors are reached through explicit class references.
    # Dynamic references are pseudo-seeds.
    #
    # An attribute read off a value of unknown type (``v.real``) finds a
    # module-level name only through the module object, so it matches the
    # module-level symbols of the modules such a value may be
    # (``SourceIndex.attribute_modules``: escaped, held through a handle,
    # what those reach; and the modules the test runner imports and hands
    # out, ``request.module``), and members of classes whatever the
    # receiver: it goes to ``name:.<n>`` (audit round 3, W20). A bare name,
    # or a name looked up on a module that does not bind it, may be any
    # symbol of that name: ``name:<n>``.
    symbols_by_name: dict[str, list[str]] = defaultdict(list)
    attribute_by_name: dict[str, list[str]] = defaultdict(list)
    runner_only = _runner_only_classes(base, head, discovered, union)
    reachable = _attribute_modules(base, head, targets, known_symbols)
    for symbol_id in sorted(known_symbols):
        symbol = head.symbols.get(symbol_id) or base.symbols[symbol_id]
        if symbol.kind != MODULE and not _is_dunder(symbol.name):
            if _inside(symbol_id, runner_only, base, head):
                continue  # only the test runner can hold an instance: see below
            symbols_by_name[symbol.name].append(symbol_id)
            if attribute_reachable(symbol, reachable):
                attribute_by_name[symbol.name].append(symbol_id)
    for node_of, by_name in ((_name_node, symbols_by_name), (_attribute_node, attribute_by_name)):
        for name, symbols in by_name.items():
            for symbol_id in symbols:
                graph.add(Edge(node_of(name), symbol_id, UNRESOLVED_NAME_MATCH), ("both",))
                # Whatever obtains a class this way can call it, which runs
                # the constructor its MRO resolves to (a resolved class
                # reference gets the same edges from the indexer). The hooks
                # are dunders, so no name matches them directly.
                for hook, revs in _constructor_hooks(symbol_id, base, head):
                    graph.add(Edge(node_of(name), hook, UNRESOLVED_NAME_MATCH, "constructor"), revs)
    any_module_readers = base.any_module_readers | head.any_module_readers
    pending_unresolved: list[tuple[UnresolvedReference, tuple[str, ...]]] = []
    dynamic_symbols: dict[str, tuple[str, ...]] = {}
    # Dynamic references that see any change, not only their import closure:
    # a dynamic *import* (any module may be behind it), and a lookup on an
    # external module in-scope code writes to (any code may run a writer, at
    # import or in a test, and change what it finds). Symbol -> which.
    unbounded_dynamic: dict[str, str] = {}
    # A symbol that runs a ``.py`` file no module name maps to
    # (diffcone.indexer.scripts) sees a change to the file; one whose program
    # computes what it imports sees any change.
    script_refs = sorted(base.script_refs | head.script_refs)
    for ref, revs in _union(base.unresolved, head.unresolved).items():
        if ref.kind == UNRESOLVED_DYNAMIC:
            dynamic_symbols.setdefault(ref.symbol, revs)
            if ref.detail.startswith(RUNS_SCRIPT):
                paths = sorted(p for s, p in script_refs if s == ref.symbol)
                unbounded_dynamic.setdefault(ref.symbol, "script:" + ", ".join(paths))
            elif "import" in ref.detail:
                unbounded_dynamic[ref.symbol] = "import"
            elif ref.detail.endswith(EXTERNAL_WRITTEN):
                unbounded_dynamic.setdefault(ref.symbol, "written")
        elif ref.name in symbols_by_name and not _is_dunder(ref.name):
            node = (
                _attribute_node(ref.name)
                if _on_unknown_value(ref, any_module_readers, base, head)
                else _name_node(ref.name)
            )
            graph.add(Edge(ref.symbol, node, UNRESOLVED_NAME_MATCH, ref.detail), revs)
        pending_unresolved.append((ref, revs))

    # Targets join the graph as nodes with explicit dependency edges.
    dynamic_deps: dict[str, list[str]] = defaultdict(list)
    # Target -> its entry's module, when the manifest does not list it.
    entry_modules: dict[str, str] = {}
    for target in targets:
        if target.entry_symbol in known_symbols:
            graph.add(Edge(target.node_id, target.entry_symbol, ENTRY), ("manifest",))
            entry = head.symbols.get(target.entry_symbol) or base.symbols.get(target.entry_symbol)
            if entry is not None and entry.module != target.entry_symbol:
                if entry.module not in target.lifecycle_dependencies:
                    entry_modules[target.node_id] = entry.module
        else:
            fallbacks.append(
                Fallback(
                    RULE_ENTRY_UNRESOLVED,
                    "target",
                    f"entry symbol {target.entry_symbol!r} was not found in either revision",
                    target=target.node_id,
                )
            )
        for dep in target.lifecycle_dependencies:
            module = dep[len(DYNAMIC_DEPENDENCY) :] if dep.startswith(DYNAMIC_DEPENDENCY) else None
            if module is not None and module in known_symbols:
                dynamic_deps[target.node_id].append(module)
            elif dep in known_symbols:
                graph.add(Edge(target.node_id, dep, LIFECYCLE), ("manifest",))
            else:
                fallbacks.append(
                    Fallback(
                        RULE_LIFECYCLE_UNRESOLVED,
                        "target",
                        f"lifecycle dependency {dep!r} was not found in either revision",
                        target=target.node_id,
                    )
                )
    _add_caller_effects(
        graph, {s.id for i in (base, head) for s in i.symbols.values() if s.kind == MODULE}
    )
    graph.freeze()
    seeded = changes if seeds is None else [c for c in changes if c.id in seeds.changes]
    fallbacks += _runner_dependency_fallbacks(targets, seeded, base, head)
    unanalysed = _changed_unanalysed_files(base, head) if seeds is None else []
    if seeds is None:
        # A build script is Python nobody imports, but it decides what is
        # compiled and installed: a change to it is as unbounded as a changed
        # compiled source.
        project_dirs = _project_dirs(base, head)
        unanalysed += sorted(
            {c.symbol.path for c in changes if is_build_script(c.symbol.path, project_dirs)}
        )
    if unanalysed:
        shown = ", ".join(unanalysed[:UNANALYSED_PATHS_SHOWN])
        more = len(unanalysed) - UNANALYSED_PATHS_SHOWN
        fallbacks.append(
            Fallback(
                RULE_UNANALYSED_FILE,
                "all_targets",
                f"{len(unanalysed)} file(s) the analysis does not read changed under the source "
                f"roots ({shown}{f', and {more} more' if more > 0 else ''}); code or tests may "
                "read them, so every supplied target is selected",
            )
        )
    outside = sorted(runner_files) if seeds is None else []
    if outside:
        shown = ", ".join(outside[:UNANALYSED_PATHS_SHOWN])
        more = len(outside) - UNANALYSED_PATHS_SHOWN
        fallbacks.append(
            Fallback(
                RULE_UNANALYSED_FILE,
                "all_targets",
                f"{len(outside)} build, dependency or runner configuration file(s) outside the "
                f"source roots changed ({shown}{f', and {more} more' if more > 0 else ''}); they "
                "decide what is installed and how the tests run, so every supplied target is "
                "selected",
            )
        )

    # Backward reachability from every changed symbol.
    mode: dict[str, int] = {}
    via: dict[str, tuple[Edge, tuple[str, ...], str] | None] = {}
    queue: deque[str] = deque()
    impacting = [c for c in seeded if c.carries_impact]
    for change in impacting:
        mode[change.id] = STRUCTURAL if change.structural else BEHAVIOR
        via[change.id] = None
        queue.append(change.id)
    module_nodes = {
        s.id for index in (base, head) for s in index.symbols.values() if s.kind == MODULE
    }
    # Nodes seeded for a change they are not themselves: the steps from the
    # node to the change, and the rule they make the explanation.
    seed_paths: dict[str, tuple[tuple[Step, ...], str]] = {}
    # A change that runs when its module is imported affects the module's
    # import, hence (through ``imports`` edges) every importer.
    at_import = [c for c in impacting if _runs_at_import(c)]
    for change in at_import:
        symbol = change.symbol
        if symbol.module not in mode:
            mode[symbol.module] = BEHAVIOR
            via[symbol.module] = None
            step = Step(symbol.module, change.id, RUNS_AT_IMPORT, "", _sides(change))
            seed_paths[symbol.module] = ((step,), RULE_DEPENDENCY)
            queue.append(symbol.module)
    # What import-time code calls runs with the arguments written there
    # (``X = set_mode("slow")``, a class attribute, a decorator's argument):
    # changing them changes what the callee does at import as a change to
    # its body would. State the callee -- or anything it calls -- mutates in
    # place (``mutated_by`` edges) holds something else, and every reader of
    # it is affected, however it reaches that state; code that only empties
    # or adds to it is not (CONTENT).
    for node, (steps, rule) in sorted(_import_call_effects(graph, at_import, module_nodes).items()):
        if node not in mode:
            mode[node] = CONTENT
            via[node] = None
            seed_paths[node] = (steps, rule)
            queue.append(node)
    # Whatever else may now call a writer differently reaches what it
    # writes through the ``calls:`` pseudo-nodes (_add_caller_effects).
    # A symbol reading docstrings (``f.__doc__``, ``getdoc(cls)``, its
    # module's ``__doc__``) sees a docstring-only change of what it references.
    documented = {c.id for c in seeded if DOCSTRING_CHANGED in c.changes}
    # A docstring its decorator reads (pandas' ``@doc`` formats it) changes
    # what the decorator does when the module is imported.
    decorated = documented & (base.doc_decorated | head.doc_decorated)
    for change in seeded:
        if change.id in decorated:
            for node in (change.id, change.symbol.module):
                if node not in mode:
                    mode[node] = BEHAVIOR
                    via[node] = None
                    if node != change.id:
                        step = Step(node, change.id, RUNS_AT_IMPORT, "its decorator reads the "
                                    "docstring", _sides(change))  # fmt: skip
                        seed_paths[node] = ((step,), RULE_DEPENDENCY)
                    queue.append(node)
    if documented:
        for index in (base, head):
            read: dict[str, set[str]] = {
                s.id: {s.module} for s in index.symbols.values() if s.reads_docstrings
            }
            for e in index.edges:
                if e.source in read and e.kind != DEFINED_IN:
                    read[e.source].add(e.target)
            for reader, referenced in sorted(read.items()):
                if reader not in mode and referenced & documented:
                    mode[reader] = BEHAVIOR
                    via[reader] = None
                    target = min(referenced & documented)
                    sides = _sides(next(c for c in seeded if c.id == target))
                    step = Step(reader, target, READS_DOCSTRING, "", sides)
                    seed_paths[reader] = ((step,), RULE_DEPENDENCY)
                    queue.append(reader)
    seed_reasons = dict(seeds.nodes) if seeds is not None else {}
    for node in sorted(seed_reasons):
        if node not in mode:
            mode[node] = BEHAVIOR
            via[node] = None
            queue.append(node)
    # A dynamic reference (eval/exec/getattr with an unbounded name) can reach
    # whatever its module's globals can reach: the module itself and every
    # module it imports, transitively. A dynamic *import* can reach anything.
    # That bound holds only while the object read is one of those globals.
    # ``def invoke(obj, name): getattr(obj, name)`` reads an object a caller
    # supplied, which can belong to any module; the index binds that read at
    # the other end instead (a class whose instances are handed around gains
    # an edge to each of its members), and the closure check stays as well.
    changed_modules = {c.symbol.module for c in impacting}
    reach = _ImportReach(base, head)

    if seeds is None:
        changed_scripts = {
            path
            for path in base.scripts.keys() | head.scripts.keys()
            if base.scripts.get(path) != head.scripts.get(path)
        }
        ran: dict[str, list[str]] = defaultdict(list)
        for symbol, path in script_refs:
            if path in changed_scripts:
                ran[symbol].append(path)
        for symbol, paths in sorted(ran.items()):
            if symbol not in mode:
                mode[symbol] = BEHAVIOR
                via[symbol] = None
                queue.append(symbol)
                unbounded_dynamic[symbol] = "changed-script:" + ", ".join(paths)
    if impacting and seeds is None:
        for symbol in sorted(dynamic_symbols):
            if symbol in mode:
                continue
            # A symbol can hold several dynamic references; the widest one
            # decides, so an import that names anything is not narrowed by a
            # getattr beside it.
            if symbol in unbounded_dynamic or reach.closure_of(symbol) & changed_modules:
                mode[symbol] = BEHAVIOR
                via[symbol] = None
                queue.append(symbol)
    while queue:
        node = queue.popleft()
        node_mode = mode[node]
        for source, edge, revs in graph.reverse.get(node, ()):
            new_mode = _propagate(edge, node_mode, change_by_id.get(node), source in module_nodes)
            if new_mode is None or new_mode <= mode.get(source, 0):
                continue
            mode[source] = new_mode
            # A node whose mode rises keeps its old explanation when the new
            # one would lead back through itself (REG -> register -> REG): the
            # explanation must end at a change, not loop.
            if source not in via or not _leads_to(via, node, source):
                via[source] = (edge, revs, node)
            queue.append(source)
    # The runner imports a target's module to reach it, so the module's
    # import-time code runs before the target whether or not a hand-written
    # manifest lists the module (discovered targets always do). Applied after
    # the search, so a target that a more specific path reaches keeps it.
    for node_id, module in sorted(entry_modules.items()):
        if node_id not in mode and module in mode:
            mode[node_id] = BEHAVIOR
            via[node_id] = (Edge(node_id, module, LIFECYCLE), ("manifest",), module)

    affected_by_name = {
        name: tuple(s for s in symbols if s in mode) for name, symbols in symbols_by_name.items()
    }
    affected_by_attribute = {
        name: tuple(s for s in symbols if s in mode) for name, symbols in attribute_by_name.items()
    }

    def matched(ref: UnresolvedReference) -> tuple[str, ...]:
        if ref.kind == UNRESOLVED_DYNAMIC:
            return ()
        by_name = (
            affected_by_attribute
            if _on_unknown_value(ref, any_module_readers, base, head)
            else affected_by_name
        )
        return tuple(s for s in by_name.get(ref.name, ()) if s != ref.symbol)

    unresolved_records = [
        UnresolvedRecord(ref.symbol, ref.kind, ref.name, ref.detail, revs, matched(ref))
        for ref, revs in pending_unresolved
    ]

    if errors:
        fallbacks.append(
            Fallback(
                RULE_ANALYSIS_ERROR,
                "all_targets",
                f"{len(errors)} analysis error(s); the dependency graph is incomplete, "
                "so every supplied target is selected",
            )
        )
    target_fallbacks: dict[str, list[Fallback]] = defaultdict(list)
    global_fallbacks: list[Fallback] = []
    for fb in fallbacks:
        if fb.scope == "target" and fb.target:
            target_fallbacks[fb.target].append(fb)
        else:
            global_fallbacks.append(fb)

    decisions: list[Decision] = []
    for target in targets:
        reasons: list[Reason] = []
        if target.node_id in mode:
            reasons.append(
                _explain(
                    target.node_id,
                    via,
                    change_by_id,
                    dynamic_symbols,
                    unbounded_dynamic,
                    seed_reasons,
                    seed_paths,
                )
            )
        for fb in target_fallbacks.get(target.node_id, ()):
            reasons.append(Reason(fb.rule, fb.detail))
        for fb in global_fallbacks:
            reasons.append(Reason(fb.rule, fb.detail))
        if seeds is not None:
            decisions.append(_decision(target, reasons, mode))
            continue
        for module in dynamic_deps.get(target.node_id, ()):
            if reach.closure_of(module) & changed_modules:
                reasons.append(
                    Reason(
                        RULE_DYNAMIC_REFERENCE,
                        f"the target runs code with the globals of {module}, and a change lies "
                        "in that module's import closure",
                    )
                )
        # A discovered target the base snapshot did not have is new, whatever
        # its entry symbol did: ``from support import test_shared as
        # test_new`` adds a test whose entry is untouched. Only discovery can
        # answer this, and only because it runs at the base as well; a
        # manifest names targets without saying when they appeared.
        if (
            base_target_ids is not None
            and target.runner_id in discovered_ids
            and target.runner_id not in base_target_ids
        ):
            reasons.append(
                Reason(
                    RULE_NEW_TARGET,
                    f"{target.runner_id} is not in the base snapshot: a new target is selected "
                    "whatever its entry symbol did",
                )
            )
        moved = (lifecycle_changes or {}).get((target.runner, target.runner_id))
        if moved is not None and target.runner_id in discovered_ids:
            reasons.append(lifecycle_reason(target.runner_id, *moved, "between the snapshots"))
        entry_change = change_by_id.get(target.entry_symbol)
        if entry_change is not None and DOCSTRING_CHANGED in entry_change.changes:
            # For a doctest the docstring is the test; for anything else
            # re-running a target whose own docstring changed is cheap.
            reasons.append(
                Reason(
                    RULE_ENTRY_DOCSTRING,
                    f"the docstring of the entry symbol {target.entry_symbol} changed",
                )
            )
        decisions.append(_decision(target, reasons, mode))

    return Plan(
        repo=repo,
        source_roots=list(source_roots or []),
        changes=changes,
        decisions=decisions,
        fallbacks=fallbacks,
        unresolved=sorted(unresolved_records, key=lambda r: (r.symbol, r.kind, r.name, r.detail)),
        errors=errors,
        declarations=sorted(declared),
        base_index=base,
        head_index=head,
        discovery=discovered,
        targets=targets,
    )


def _decision(target: Target, reasons: list[Reason], mode: dict[str, int]) -> Decision:
    affected = sorted(
        dep for dep in (target.entry_symbol, *target.lifecycle_dependencies) if dep in mode
    )
    selected = bool(reasons)
    return Decision(
        target=target,
        selected=selected,
        reasons=reasons,
        affected_dependencies=affected,
        unselected_reason=None
        if selected
        else "no dependency path from this target to a changed symbol in either revision",
    )


def _runner_dependency_fallbacks(
    targets: list[Target], changes: list[SymbolChange], base: SourceIndex, head: SourceIndex
) -> list[Fallback]:
    """A runner whose own process imports modules that are in the source
    roots (pytest runs pluggy's hooks for every test) runs project code for
    every target: a change there selects all of that runner's targets. The
    modules are discovery's RUNNER_MODULES, those under them, and their
    import closure."""
    impacting = [c for c in changes if c.carries_impact]
    if not impacting:
        return []
    modules = base.modules | head.modules
    reach = _ImportReach(base, head)
    fallbacks: list[Fallback] = []
    for runner in sorted({t.runner for t in targets}):
        entries = RUNNER_MODULES.get(runner, ())
        roots = {m for m in modules if any(m == e or m.startswith(e + ".") for e in entries)}
        if not roots:
            continue
        runner_modules: set[str] = set()
        for module in roots:
            runner_modules |= reach.closure_of(module)
        hit = sorted(c.id for c in impacting if c.symbol.module in runner_modules)
        if not hit:
            continue
        shown = ", ".join(hit[:3]) + (f" and {len(hit) - 3} more" if len(hit) > 3 else "")
        detail = (
            f"{shown} changed in code the {runner} runner itself imports and runs for every "
            "target, so every target of that runner is selected"
        )
        for target in targets:
            if target.runner == runner:
                fallbacks.append(
                    Fallback(RULE_RUNNER_DEPENDENCY, "target", detail, target=target.node_id)
                )
    return fallbacks


class _ImportReach:
    """Transitive import closure of modules, over both revisions: what a
    module's globals can name. A module other code puts objects on (an
    attribute stored, the module handed on, another object installed under
    its name: ``SourceIndex.module_writers``) can hold whatever that code's
    module can name, so the closure follows from it to the writer's module
    too, and to the modules of code that may write any module (audit round
    3, W24)."""

    def __init__(
        self,
        base: SourceIndex,
        head: SourceIndex,
        writer: Callable[[str], bool] | None = None,
    ) -> None:
        """``writer``: which writers count (evidence mode leaves out code that
        ran only inside tests: what it stores is seen in a test that ran
        it)."""
        self.module_of: dict[str, str] = {}
        self.imports_of: dict[str, set[str]] = defaultdict(set)
        for index in (base, head):
            for symbol in index.symbols.values():
                self.module_of[symbol.id] = symbol.module
        for index in (base, head):
            for edge in index.edges:
                if edge.kind == IMPORTS and edge.target in self.module_of:
                    source_module = self.module_of.get(edge.source)
                    if source_module is not None:
                        self.imports_of[source_module].add(edge.target)
        self._anywhere: set[str] = set()
        for index in (base, head):
            for module, writers in index.module_writers.items():
                found = {
                    self.module_of[w]
                    for w in writers
                    if w in self.module_of and (writer is None or writer(w))
                }
                if module == ANY_MODULE:
                    self._anywhere |= found
                else:
                    self.imports_of[module] |= found - {module}
        self._closures: dict[str, set[str]] = {}

    def closure_of(self, symbol_id: str) -> set[str]:
        module = self.module_of.get(symbol_id)
        if module is None:
            return set()
        if module not in self._closures:
            seen = {module, *self._anywhere}
            stack = [module, *self._anywhere]
            while stack:
                for target in self.imports_of.get(stack.pop(), ()):
                    if target not in seen:
                        seen.add(target)
                        stack.append(target)
            self._closures[module] = seen
        return self._closures[module]


def _name_node(name: str) -> str:
    return f"name:{name}"


def _attribute_node(name: str) -> str:
    """The name-match pseudo-node of an attribute read off a value of
    unknown type (see plan_from_indexes)."""
    return f"name:.{name}"


def _is_name_node(node_id: str) -> bool:
    return node_id.startswith("name:")


def _matched_name(node_id: str) -> str:
    """The name a name-match pseudo-node matches on."""
    return node_id[len("name:") :].removeprefix(".")


def _on_unknown_value(
    ref: UnresolvedReference, any_module_readers: set[str], base: SourceIndex, head: SourceIndex
) -> bool:
    """Whether ``ref`` reads an attribute off a value of unknown type that
    can be a module only through ``SourceIndex.attribute_modules``: not a
    bare name, not a name looked up on a module, not in a module holding a
    module named at run time."""
    if ref.kind != UNRESOLVED_ATTRIBUTE:
        return False
    symbol = head.symbols.get(ref.symbol) or base.symbols.get(ref.symbol)
    return symbol is None or symbol.module not in any_module_readers


def _attribute_modules(
    base: SourceIndex, head: SourceIndex, targets: list[Target], known_symbols: set[str]
) -> set[str]:
    """What a value of unknown type may be (SourceIndex.attribute_modules,
    over both revisions), and the modules the test runner imports and can
    hand out (``request.module``, a collector's ``obj``, the plugin
    manager): those of each target's entry and lifecycle dependencies, and
    every ``conftest``."""
    found = base.attribute_modules | head.attribute_modules
    for target in targets:
        for dep in (target.entry_symbol, *target.lifecycle_dependencies):
            symbol = head.symbols.get(dep) or base.symbols.get(dep)
            if symbol is not None:
                found.add(symbol.id if symbol.kind == MODULE else symbol.module)
    for module in known_symbols:
        if module.rsplit(".", 1)[-1] == "conftest":
            found.add(module)
    return found


def _constructor_hooks(
    class_id: str, base: SourceIndex, head: SourceIndex
) -> list[tuple[str, tuple[str, ...]]]:
    """``__init__``/``__new__`` of a class and of its in-scope ancestors, per
    revision: calling the class runs the first of each its MRO finds, and
    every ancestor's is a superset of that. Empty for anything not a class."""
    found: dict[str, list[str]] = defaultdict(list)
    for label, index in (("base", base), ("head", head)):
        symbol = index.symbols.get(class_id)
        if symbol is None or symbol.kind != CLASS:
            continue
        stack, seen = [class_id], {class_id}
        while stack:
            cls = stack.pop()
            for hook in ("__init__", "__new__"):
                if f"{cls}.{hook}" in index.symbols:
                    found[f"{cls}.{hook}"].append(label)
            for up in index.class_bases.get(cls, ()):
                if up not in seen:
                    seen.add(up)
                    stack.append(up)
    return [(hook, tuple(labels)) for hook, labels in sorted(found.items())]


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _leads_to(
    via: dict[str, tuple[Edge, tuple[str, ...], str] | None], start: str, target: str
) -> bool:
    """Whether the explanation chain from ``start`` passes through ``target``."""
    seen: set[str] = set()
    current: str | None = start
    while current is not None and current not in seen:
        if current == target:
            return True
        seen.add(current)
        link = via.get(current)
        current = link[2] if link is not None else None
    return False


def _explain(
    node: str,
    via: dict[str, tuple[Edge, tuple[str, ...], str] | None],
    change_by_id: dict[str, SymbolChange],
    dynamic_symbols: dict[str, tuple[str, ...]],
    unbounded_dynamic: dict[str, str],
    seed_reasons: dict[str, str] | None = None,
    seed_paths: dict[str, tuple[tuple[Step, ...], str]] | None = None,
) -> Reason:
    steps: list[Step] = []
    current = node
    rule = RULE_DEPENDENCY
    pending: tuple[str, str, tuple[str, ...]] | None = None  # (source, detail, revs)
    walked: set[str] = set()
    while True:
        link = via[current]
        if link is None or current in walked:
            break
        walked.add(current)
        edge, revs, nxt = link
        if edge.kind == DECLARED and rule == RULE_DEPENDENCY:
            # The path only holds because the project said so; say which rule
            # carried it rather than calling it an ordinary dependency. A name
            # match anywhere on the path outranks it: that one is our guess,
            # this one is the project's statement.
            rule = RULE_DECLARED_DEPENDENCY
        if edge.kind == UNRESOLVED_NAME_MATCH:
            rule = RULE_UNRESOLVED_NAME_MATCH  # outranks a declared edge
            if _is_name_node(edge.target):
                pending = (edge.source, edge.detail, revs)  # collapse the pseudo-node
                current = nxt
                continue
            if pending is not None:
                source, detail, revs = pending
                steps.append(Step(source, edge.target, UNRESOLVED_NAME_MATCH, detail, revs))
                pending = None
                current = nxt
                continue
        steps.append(Step(edge.source, edge.target, edge.kind, edge.detail, revs))
        current = nxt
    steps = _call_steps(steps)
    if seed_paths and current in seed_paths and current not in change_by_id:
        # Seeded for a change it is not (its module's import, a callee's
        # effect): the seed's own steps lead on to the change.
        seed_steps, seed_rule = seed_paths[current]
        steps += seed_steps
        current = seed_steps[-1].target
        if rule != RULE_UNRESOLVED_NAME_MATCH and seed_rule != RULE_DEPENDENCY:
            rule = seed_rule
    change = change_by_id.get(current)
    if change is not None:
        detail = f"{current} {'/'.join(change.changes)}"
        return Reason(rule, detail, tuple(steps), current, change.changes)
    if seed_reasons and current in seed_reasons:
        return Reason(RULE_ESCALATED, seed_reasons[current], tuple(steps))
    # Pseudo-seed: a symbol with a dynamic reference.
    revs = dynamic_symbols.get(current, ())
    if unbounded_dynamic.get(current) == "written":
        return Reason(
            RULE_DYNAMIC_REFERENCE,
            f"{current} looks up a name on an external module that in-scope code writes to "
            f"({', '.join(revs)}); code anywhere may run a writer, so any change can change "
            "what it finds",
            tuple(steps),
        )
    kind = unbounded_dynamic.get(current, "")
    if kind.startswith("changed-script:"):
        return Reason(
            RULE_UNANALYSED_FILE,
            f"{current} runs {kind[len('changed-script:') :]}, a Python file no module name "
            "maps to, which changed (the analysis reads only what it imports)",
            tuple(steps),
        )
    if kind.startswith("script:"):
        return Reason(
            RULE_DYNAMIC_REFERENCE,
            f"{current} runs {kind[len('script:') :]}, a Python file no module name maps to "
            f"({', '.join(revs)}) that imports or runs code by a name it computes, so any "
            "change can affect it",
            tuple(steps),
        )
    if current in unbounded_dynamic:
        return Reason(
            RULE_DYNAMIC_REFERENCE,
            f"{current} imports a module named at runtime ({', '.join(revs)}); any module in "
            "scope may be behind it, so its dependencies cannot be bounded statically",
            tuple(steps),
        )
    return Reason(
        RULE_DYNAMIC_REFERENCE,
        f"{current} uses a dynamic import/attribute access ({', '.join(revs)}); "
        "a change is reachable from its module's imports, so its dependencies cannot be "
        "bounded statically",
        tuple(steps),
        None,
        (),
    )


def _index_snapshot(
    repo_path: Path,
    revision: str,
    roots: list[str],
    *,
    with_config: bool,
    cache: IndexCache | None,
) -> tuple[SourceIndex, Snapshot | None]:
    """Index a snapshot, serving committed snapshots from the cache. Returns
    the snapshot too when it had to be read (discovery needs its files)."""
    if cache is not None and revision not in (WORKTREE, INDEX) and not with_config:
        try:
            commit = resolve_commit(repo_path, revision)
        except GitError:
            commit = None
        if commit is not None:
            cached = cache.load(commit, roots)
            if cached is not None:
                # The cached index was built for whatever spelling of this
                # commit came first; the report shows this one.
                info = replace(
                    cached.snapshot,
                    revision=revision,
                    description=commit_description(commit, revision),
                )
                cached = replace(
                    cached,
                    snapshot=info,
                    errors=[replace(e, revision=revision) for e in cached.errors],
                )
                return cached, None
    snapshot = read_snapshot(repo_path, revision, roots, with_config=with_config)
    index = build_index(snapshot, module_cache=cache.modules if cache is not None else None)
    if cache is not None and snapshot.info.committed:
        cache.store(index, roots)
    return index, snapshot


def _cacheable_commit(repo_path: Path, revision: str) -> str | None:
    """The commit a revision names, or None for ``WORKTREE``, ``INDEX`` and
    anything that does not resolve: what may be served from a cache."""
    if revision in (WORKTREE, INDEX):
        return None
    try:
        return resolve_commit(repo_path, revision)
    except GitError:
        return None


def without_cyclic_gc(func):
    """Run ``func`` with Python's cyclic garbage collector suspended, and
    restore its state after. A plan holds two whole indexes (on pandas,
    millions of objects) while it allocates syntax trees and sets, so each
    collection walks the whole heap: on pandas the collector took two thirds
    of a warm plan (46-56 s with it, 18 s without). Planning makes almost
    no reference cycles; they are collected once the collector resumes.
    Rendering a plan's report is the same: the indexes are still alive
    (on pandas 4.7 s CPU with the collector, 0.9 s without)."""

    @functools.wraps(func)
    def inner(*args, **kwargs):
        enabled = gc.isenabled()
        gc.disable()
        try:
            return func(*args, **kwargs)
        finally:
            if enabled:
                gc.enable()

    return inner


def _discovery_at(
    repo: Path,
    commit: str,
    index: SourceIndex,
    roots: list[str],
    runner: str,
    options: DiscoveryOptions,
    cache: IndexCache | None,
) -> DiscoveryResult:
    """Static discovery at a commit, through the discovery cache."""
    found = cache.discovery.load(commit, roots, runner, options) if cache is not None else None
    if found is None:
        snapshot = read_snapshot(repo, commit, roots, with_config=True)
        found = discover(runner, snapshot, index, options)
        if cache is not None:
            cache.discovery.store(found, commit, roots, options)
    return found


def _since_recording(
    repo: Path,
    evidence: Evidence,
    evidence_index: SourceIndex,
    base: SourceIndex,
    base_lifecycle: Lifecycles,
    head_lifecycle: Lifecycles,
    roots: list[str],
    options: DiscoveryOptions,
    cache: IndexCache | None,
) -> LifecycleChanges:
    """The pytest targets whose lifecycle dependencies differ between the
    recording at C and the head, or the base (evidence plans C -> base and
    C -> head): what the recorded run set up around them is not what runs
    now, and a fixture, hook or plugin it never ran has no record to meet a
    change. Added and removed are merged over both sides."""
    at_c = _lifecycles(
        [_discovery_at(repo, evidence.commit, evidence_index, roots, "pytest", options, cache)]
    )
    sides = [head_lifecycle]
    if not (base.snapshot.committed and base.snapshot.commit == evidence.commit):
        sides.append(base_lifecycle)
    merged: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    for side in sides:
        pytest_side = {key: deps for key, deps in side.items() if key[0] == "pytest"}
        for key, (added, removed) in diff_lifecycles(at_c, pytest_side).items():
            into = merged.setdefault(key, (set(), set()))
            into[0].update(added)
            into[1].update(removed)
    return {key: (tuple(sorted(a)), tuple(sorted(r))) for key, (a, r) in merged.items()}


def _settle_discovery(
    repo: Path,
    head: str,
    evidence: Evidence,
    evidence_index: SourceIndex,
    discovered: list[DiscoveryResult],
    roots: list[str],
    options: DiscoveryOptions,
    cache: IndexCache | None,
) -> list[DiscoveryResult]:
    """What the recording says about pytest discovery's completeness
    (roadmap item 9). It ran pytest's real collection at C, in the
    environment ``run`` checks before any test. A note saying a plugin may
    collect tests that are not targets stops counting when the same note was
    there at C, its file is unchanged since C, and pytest collected nothing
    at C that was not a target then. A test it did collect that was not a
    target is a gap the recording proves, noted as incomplete itself."""
    if evidence.collected is None or not any(d.runner == "pytest" for d in discovered):
        return discovered
    at_c = _discovery_at(repo, evidence.commit, evidence_index, roots, "pytest", options, cache)
    targets_at_c = {t.runner_id for t in at_c.targets}
    extra = sorted(evidence.collected - targets_at_c)
    notes_at_c = {(n.kind, n.detail) for n in at_c.notes}
    short = evidence.commit[:12]
    out = []
    for result in discovered:
        if result.runner != "pytest":
            out.append(result)
            continue
        notes = []
        for note in result.notes:
            if (
                note.kind in INCOMPLETE_NOTE_KINDS
                and not extra
                and note.path
                and (note.kind, note.detail) in notes_at_c
                and file_id(repo, evidence.commit, note.path) == file_id(repo, head, note.path)
            ):
                note = DiscoveryNote(
                    note.runner,
                    "settled_by_evidence",
                    f"{note.detail} [settled: at {short}, where this note stood too, pytest "
                    f"collected no test that was not a target, and {note.path} is unchanged "
                    "since]",
                    note.path,
                )
            notes.append(note)
        if extra:
            notes.append(
                DiscoveryNote(
                    "pytest",
                    "collected_not_target",
                    f"the recording at {short} collected {len(extra)} test(s) that were not "
                    f"targets there, e.g. {', '.join(extra[:3])}",
                )
            )
        out.append(DiscoveryResult(result.runner, result.targets, notes, result.config))
    return out


def _check_roots_match(
    roots: list[str], base: str, base_index: SourceIndex, head: str, head_index: SourceIndex
) -> None:
    """A source root holding no Python file in either revision is almost
    certainly a mistake (a typo, a path outside the repository), and would
    plan nothing as complete: make it an analysis error, so the plan
    selects everything and says why."""
    paths = [
        symbol.path
        for index in (base_index, head_index)
        for symbol in index.symbols.values()
        if symbol.kind == MODULE
    ]
    for root in roots:
        directory = split_root(root)[0]
        if not directory:
            continue
        if not any(path.startswith(directory + "/") for path in paths):
            head_index.errors.append(
                AnalysisError(
                    revision=head,
                    path=directory,
                    message=(
                        f"source root {root!r} holds no Python file at {base} or {head}; "
                        "check the path (it is relative to the repository)"
                    ),
                )
            )


def _manifest_notes(
    manifest: Manifest | None, discovered: list[DiscoveryResult]
) -> list[DiscoveryResult]:
    """The notes ``discover`` wrote into a manifest, for runners this plan
    did not discover itself: a target list it said may be short keeps the
    plan at exit code 3."""
    if manifest is None or not manifest.notes:
        return []
    fresh = {result.runner for result in discovered}
    by_runner: dict[str, list[DiscoveryNote]] = defaultdict(list)
    for runner, kind, detail, path in manifest.notes:
        if runner not in fresh:
            by_runner[runner].append(DiscoveryNote(runner, kind, detail, path))
    return [
        DiscoveryResult(runner, notes=notes, config={"from_manifest": True})
        for runner, notes in sorted(by_runner.items())
    ]


# Lifecycle dependencies named in a ``lifecycle_changed`` reason.
LIFECYCLE_SHOWN = 5

# (runner, runner_id) -> its lifecycle dependencies; and -> (added, removed).
Lifecycles = dict[tuple[str, str], tuple[str, ...]]
LifecycleChanges = dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]]


def lifecycle_reason(
    runner_id: str, added: tuple[str, ...], removed: tuple[str, ...], between: str
) -> Reason:
    """The ``lifecycle_changed`` reason for a target whose lifecycle
    dependencies moved ``between`` two snapshots."""
    parts = [
        f"{label} {', '.join(deps[:LIFECYCLE_SHOWN])}"
        + (f" and {len(deps) - LIFECYCLE_SHOWN} more" if len(deps) > LIFECYCLE_SHOWN else "")
        for label, deps in (("now runs", added), ("no longer runs", removed))
        if deps
    ]
    return Reason(
        RULE_LIFECYCLE_CHANGED,
        f"what the runner sets up around {runner_id} changed {between}: it {'; it '.join(parts)}",
    )


def _lifecycles(discovered: list[DiscoveryResult]) -> Lifecycles:
    return {
        (target.runner, target.runner_id): target.lifecycle_dependencies
        for result in discovered
        for target in result.targets
    }


def diff_lifecycles(before: Lifecycles, after: Lifecycles) -> LifecycleChanges:
    """(runner, runner_id) -> (added, removed) for each target both snapshots
    have, with other lifecycle dependencies: a fixture, hook or plugin that
    now applies to it (an autouse fixture of a plugin another test module
    now registers), or no longer does, decides what it runs although neither
    the target nor that dependency changed."""
    changes: LifecycleChanges = {}
    for key, deps in after.items():
        if key not in before:
            continue
        old, new = set(before[key]), set(deps)
        if old != new:
            changes[key] = (tuple(sorted(new - old)), tuple(sorted(old - new)))
    return changes


def _with_base_lifecycle(
    discovered: list[DiscoveryResult], base_lifecycle: dict[tuple[str, str], tuple[str, ...]]
) -> list[DiscoveryResult]:
    """Head targets with the lifecycle dependencies the same target had at
    the base added: a fixture, conftest or setup the test used before the
    change and no longer does (deleted with its autouse fixture, a removed
    override) still decides whether the change reaches it, as every other
    edge counts in both revisions."""
    merged: list[DiscoveryResult] = []
    for result in discovered:
        targets = []
        for target in result.targets:
            before = base_lifecycle.get((target.runner, target.runner_id), ())
            extra = [d for d in before if d not in target.lifecycle_dependencies]
            if extra:
                deps = tuple(sorted({*target.lifecycle_dependencies, *extra}))
                target = replace(target, lifecycle_dependencies=deps)
            targets.append(target)
        merged.append(replace(result, targets=targets))
    return merged


@without_cyclic_gc
def plan(
    repo: str | Path,
    base: str,
    head: str,
    manifest: Manifest | None = None,
    source_roots: list[str] | None = None,
    discover_runners: Iterable[str] = (),
    discovery_options: DiscoveryOptions | None = None,
    cache: IndexCache | None = None,
    evidence: Evidence | None = None,
) -> Plan:
    """Analyse two snapshots and produce a selection plan.

    Targets come from the manifest, from static discovery of the head
    snapshot for each runner in ``discover_runners``, or both. ``base`` and
    ``head`` are git revisions, ``INDEX`` or ``WORKTREE``; the plan records
    which kind each one was. With ``evidence`` (recorded by ``diffcone
    collect``) pytest targets are selected on what each test executed
    (evidence_plan.py); the evidence's source roots must match.
    """
    repo_path = Path(repo)
    manifest_roots = manifest.source_roots if manifest is not None else None
    roots = list(source_roots or manifest_roots or ["."])
    runners = list(discover_runners)
    options = discovery_options or DiscoveryOptions()
    base_index, _ = _index_snapshot(repo_path, base, roots, with_config=False, cache=cache)
    # Discovery of a committed head may come from the cache, and then so may
    # the head index; otherwise discovery needs the head snapshot's files.
    discovery_cache = cache.discovery if cache is not None else None
    head_commit = _cacheable_commit(repo_path, head) if discovery_cache is not None else None
    cached_head: list[DiscoveryResult] | None = None
    if runners and head_commit is not None and discovery_cache is not None:
        found = [discovery_cache.load(head_commit, roots, r, options) for r in runners]
        hits = [result for result in found if result is not None]
        if len(hits) == len(runners):
            cached_head = hits
    head_index, head_snapshot = _index_snapshot(
        repo_path, head, roots, with_config=bool(runners) and cached_head is None, cache=cache
    )
    # Both revisions, as every other edge is: a commit that deletes a
    # declaration while changing what it pointed at must still select.
    # Discovery at the base too, so a target the base did not have is known
    # to be new. Only the snapshot is read again (0.1 s on the largest
    # repositories); the base index still comes from the cache.
    base_target_ids: set[str] | None = None
    base_lifecycle: dict[tuple[str, str], tuple[str, ...]] = {}
    if runners:
        base_commit = _cacheable_commit(repo_path, base) if discovery_cache is not None else None
        base_target_ids = set()
        base_snapshot: Snapshot | None = None
        for runner in runners:
            result = (
                discovery_cache.load(base_commit, roots, runner, options)
                if base_commit is not None and discovery_cache is not None
                else None
            )
            if result is None:
                if base_snapshot is None:
                    base_snapshot = read_snapshot(repo_path, base, roots, with_config=True)
                result = discover(runner, base_snapshot, base_index, options)
                if base_commit is not None and discovery_cache is not None:
                    discovery_cache.store(result, base_commit, roots, options)
            base_target_ids |= {target.runner_id for target in result.targets}
            for target in result.targets:
                base_lifecycle[(target.runner, target.runner_id)] = target.lifecycle_dependencies
    _check_roots_match(roots, base, base_index, head, head_index)
    declared: list[Declaration] = []
    always: dict[AlwaysRun, None] = {}
    always_from: dict[AlwaysRun, tuple[str, SourceIndex]] = {}
    for revision, index in ((base, base_index), (head, head_index)):
        found = load_declarations(repo_path, revision)
        declared += found.edges
        always.update(dict.fromkeys(found.always_run))
        for entry in found.always_run:
            always_from.setdefault(entry, (revision, index))
        for problem in found.problems:
            index.errors.append(
                AnalysisError(revision=revision, path=DECLARATION_FILE, message=problem)
            )
    declared = sorted(set(declared))
    discovered: list[DiscoveryResult] = []
    if cached_head is not None:
        discovered = cached_head
    elif runners:
        if head_snapshot is None:  # pragma: no cover
            raise GitError("discovery needs the head snapshot's files")
        discovered = [discover(runner, head_snapshot, head_index, options) for runner in runners]
        if head_commit is not None and discovery_cache is not None:
            for result in discovered:
                discovery_cache.store(result, head_commit, roots, options)
    head_lifecycle = _lifecycles(discovered)
    lifecycle_changes = diff_lifecycles(base_lifecycle, head_lifecycle)
    discovered = _with_base_lifecycle(discovered, base_lifecycle)
    _check_always_run_runners(always_from, manifest, discovered)
    if evidence is not None:
        from diffcone.evidence_plan import plan_with_evidence

        if not has_commit(repo_path, evidence.commit):
            raise EvidenceError(
                f"the evidence was recorded at {evidence.commit[:12]}, which this checkout does "
                f"not have (a shallow clone?); fetch it: git fetch --depth=1 origin "
                f"{evidence.commit}"
            )
        if sorted(evidence.source_roots) != sorted(roots):
            raise EvidenceError(
                f"the evidence was recorded with source roots {evidence.source_roots}, the plan "
                f"uses {roots}: symbols would not line up"
            )
        evidence_index, _ = _index_snapshot(
            repo_path, evidence.commit, roots, with_config=False, cache=cache
        )
        discovered = _settle_discovery(
            repo_path, head, evidence, evidence_index, discovered, roots, options, cache
        )
        since_c: LifecycleChanges = {}
        if "pytest" in runners:
            since_c = _since_recording(
                repo_path,
                evidence,
                evidence_index,
                base_index,
                base_lifecycle,
                head_lifecycle,
                roots,
                options,
                cache,
            )
        planned = plan_with_evidence(
            base_index,
            head_index,
            evidence,
            evidence_index,
            manifest,
            repo=str(repo_path),
            source_roots=roots,
            discovered=discovered,
            declarations=declared,
            base_target_ids=base_target_ids,
            lifecycle_changes=lifecycle_changes,
            lifecycle_since_recording=since_c,
        )
    else:
        planned = plan_from_indexes(
            base_index,
            head_index,
            manifest,
            repo=str(repo_path),
            source_roots=roots,
            discovered=discovered,
            declarations=declared,
            base_target_ids=base_target_ids,
            runner_files=_runner_files_outside_roots(repo_path, base_index, head_index, roots),
            lifecycle_changes=lifecycle_changes,
        )
    return _with_always_run(planned, sorted(always))


def _check_always_run_runners(
    entries: dict[AlwaysRun, tuple[str, SourceIndex]],
    manifest: Manifest | None,
    discovered: list[DiscoveryResult],
) -> None:
    """An ``[[always_run]]`` entry's ``runner`` must name a runner diffcone
    discovers (``pytest``, ``asv``) or one of this plan's targets (a
    manifest may use any label): anything else is a typo (``pyest``) that
    would match nothing in every job, and is an analysis error. A known
    runner with no targets in this plan, like a pattern matching nothing, is
    only counted: one file serves jobs that plan different runners."""
    known = set(RUNNERS) | {t.runner for t in merge_targets(manifest, discovered)}
    for entry, (revision, index) in sorted(entries.items()):
        if entry.runner and entry.runner not in known:
            index.errors.append(
                AnalysisError(
                    revision=revision,
                    path=DECLARATION_FILE,
                    message=(
                        f"always_run {entry.targets!r} names the runner {entry.runner!r}, which "
                        f"is neither one diffcone discovers ({', '.join(RUNNERS)}) nor a runner "
                        "of this plan's targets"
                    ),
                )
            )


def _with_always_run(plan: Plan, entries: list[AlwaysRun]) -> Plan:
    """Select every target an ``[[always_run]]`` entry of either snapshot
    matches, after the planner has decided the rest. An entry matching
    nothing is counted, not an error: one file serves jobs that run
    different parts of a suite (a job that ignores a directory), and a
    target no entry names is planned as any other."""
    plan.always_run = entries
    plan.always_run_matched = dict.fromkeys(entries, 0)
    for decision in plan.decisions:
        target = decision.target
        hits = [e for e in entries if e.matches(target.runner, target.runner_id)]
        if not hits:
            continue
        for e in hits:
            plan.always_run_matched[e] += 1
        decision.reasons += [Reason(RULE_ALWAYS_RUN, e.detail) for e in hits]
        decision.selected = True
        decision.unselected_reason = None
    return plan

"""Static pytest discovery.

Reproduces the collection rules below without importing test code. Anything
outside them is reported, never guessed.

Collected:

* files matching ``python_files`` (default ``test_*.py``, ``*_test.py``) under
  the source roots, restricted to ``testpaths`` when configured;
* module-level functions matching ``python_functions`` (default prefix
  ``test``), methods of classes matching ``python_classes`` (default prefix
  ``Test``) that have no ``__init__``, nested test classes, and methods of
  classes whose bases end in ``TestCase``;
* configuration from ``pytest.ini``, ``pyproject.toml``
  (``[tool.pytest.ini_options]``), ``tox.ini`` or ``setup.cfg`` at the
  repository root.

Lifecycle dependencies attached to each test:

* fixtures requested by parameter name, by ``@pytest.mark.usefixtures`` on
  the function, class or module (``pytestmark``), and transitively by other
  fixtures, resolved in pytest's order: class, module, nearest ``conftest.py``
  outward, then ``pytest_plugins`` modules within the source roots;
* ``autouse`` fixtures visible from the test;
* the test module, every ``conftest.py`` on the path, and each
  ``pytest_*`` hook function in those conftests;
* xunit-style setup/teardown functions and methods when present.

Fixtures are recognised by a decorator whose dotted name ends in ``fixture``
(``@pytest.fixture``, ``@pytest.fixture(name=...)``, ``@fixture``,
``@pytest_asyncio.fixture``). Overriding follows nearest-scope-wins.
Fixture parametrisation, ``indirect`` parametrisation, dynamic
``request.getfixturevalue`` and fixtures from installed plugins are not
modelled; an unknown fixture becomes the lifecycle dependency
``fixture:<name>`` so the planner selects its users unless the name is
declared external.
"""

from __future__ import annotations

import ast
import configparser
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

from diffcone.discovery import DiscoveryNote, DiscoveryOptions, DiscoveryResult
from diffcone.discovery.common import (
    ParsedModule,
    decorator_chain,
    keyword_value,
    parameter_names,
    parse_modules,
    scope_assignments,
    scope_classes,
    scope_functions,
    string_literals,
)
from diffcone.model import SourceIndex
from diffcone.snapshot import Snapshot

RUNNER = "pytest"

DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
DEFAULT_PYTHON_CLASSES = ("Test",)
DEFAULT_PYTHON_FUNCTIONS = ("test",)

BUILTIN_FIXTURES = frozenset(
    {
        "cache",
        "capfd",
        "capfdbinary",
        "caplog",
        "capsys",
        "capsysbinary",
        "capteesys",
        "doctest_namespace",
        "monkeypatch",
        "pytestconfig",
        "record_property",
        "record_testsuite_property",
        "record_xml_attribute",
        "recwarn",
        "request",
        "subtests",
        "testdir",
        "pytester",
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
    }
)

CLASS_SETUP_METHODS = (
    "setup_class",
    "teardown_class",
    "setup_method",
    "teardown_method",
    "setup",
    "teardown",
    "setUp",
    "tearDown",
    "setUpClass",
    "tearDownClass",
)
MODULE_SETUP_FUNCTIONS = (
    "setup_module",
    "teardown_module",
    "setup_function",
    "teardown_function",
)


# --------------------------------------------------------------------------- config


def _split(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return ()


def read_pytest_config(snapshot: Snapshot) -> dict[str, Any]:
    """Return python_files/classes/functions/testpaths and where they came from."""
    config: dict[str, Any] = {
        "source": None,
        "python_files": DEFAULT_PYTHON_FILES,
        "python_classes": DEFAULT_PYTHON_CLASSES,
        "python_functions": DEFAULT_PYTHON_FUNCTIONS,
        "testpaths": (),
    }
    section: dict[str, Any] | None = None
    files = snapshot.config_files
    # pytest's precedence: pytest.ini, pyproject.toml, tox.ini, setup.cfg.
    if "pytest.ini" in files:
        section = _ini_section(files["pytest.ini"], "pytest")
        config["source"] = "pytest.ini"
    if section is None and "pyproject.toml" in files:
        try:
            data = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
            section = data.get("tool", {}).get("pytest", {}).get("ini_options")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            section = None
        if section is not None:
            config["source"] = "pyproject.toml"
    if section is None and "tox.ini" in files:
        section = _ini_section(files["tox.ini"], "pytest")
        if section is not None:
            config["source"] = "tox.ini"
    if section is None and "setup.cfg" in files:
        section = _ini_section(files["setup.cfg"], "tool:pytest")
        if section is not None:
            config["source"] = "setup.cfg"
    if section:
        for key in ("python_files", "python_classes", "python_functions", "testpaths"):
            if key in section:
                values = _split(section[key])
                if values:
                    config[key] = values
    return config


def _ini_section(raw: bytes, name: str) -> dict[str, Any] | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(raw.decode("utf-8"))
    except (configparser.Error, UnicodeDecodeError):
        return None
    if not parser.has_section(name):
        return None
    return dict(parser.items(name))


def _matches(patterns: tuple[str, ...], name: str) -> bool:
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            if fnmatch(name, pattern):
                return True
        elif name.startswith(pattern):
            return True
    return False


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class Fixture:
    name: str
    symbol: str
    autouse: bool
    requests: tuple[str, ...]


@dataclass
class ModuleFacts:
    parsed: ParsedModule
    fixtures: dict[str, Fixture] = field(default_factory=dict)
    class_fixtures: dict[str, dict[str, Fixture]] = field(default_factory=dict)  # class id ->
    hooks: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    usefixtures: tuple[str, ...] = ()
    setup_functions: list[str] = field(default_factory=list)


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, str | None, bool]:
    """(is_fixture, explicit name, autouse)."""
    for dec in node.decorator_list:
        parts, call = decorator_chain(dec)
        if parts and parts[-1] in ("fixture", "yield_fixture"):
            name_node = keyword_value(call, "name")
            name = (
                name_node.value
                if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)
                else None
            )
            autouse_node = keyword_value(call, "autouse")
            autouse = isinstance(autouse_node, ast.Constant) and bool(autouse_node.value)
            return True, name, autouse
    return False, None, False


def _usefixtures_from_decorators(decorators: list[ast.expr]) -> tuple[str, ...]:
    names: list[str] = []
    for dec in decorators:
        parts, call = decorator_chain(dec)
        if call is not None and len(parts) >= 2 and parts[-2:] == ["mark", "usefixtures"]:
            names.extend(string_literals(list(call.args)))
    return tuple(names)


def _usefixtures_from_pytestmark(body: list[ast.stmt]) -> tuple[str, ...]:
    names: list[str] = []
    for name, value in scope_assignments(body):
        if name != "pytestmark":
            continue
        marks = list(value.elts) if isinstance(value, (ast.List, ast.Tuple)) else [value]
        names.extend(_usefixtures_from_decorators(marks))
    return tuple(names)


def _plugins_from_body(body: list[ast.stmt]) -> list[str]:
    for name, value in scope_assignments(body):
        if name == "pytest_plugins":
            return string_literals([value])
    return []


def _fixture_requests(
    node: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool
) -> tuple[str, ...]:
    params = parameter_names(node)
    if is_method and params:
        params = params[1:]
    return tuple(p for p in params if p != "request")


def _collect_facts(parsed: ParsedModule) -> ModuleFacts:
    facts = ModuleFacts(parsed=parsed)
    body = parsed.tree.body
    for func in scope_functions(body):
        is_fixture, explicit, autouse = _is_fixture(func)
        symbol = f"{parsed.module}.{func.name}"
        if is_fixture:
            name = explicit or func.name
            facts.fixtures[name] = Fixture(name, symbol, autouse, _fixture_requests(func, False))
        elif func.name.startswith("pytest_"):
            facts.hooks.append(symbol)
        elif func.name in MODULE_SETUP_FUNCTIONS:
            facts.setup_functions.append(symbol)
    for cls in scope_classes(body):
        _collect_class_fixtures(facts, cls, f"{parsed.module}.{cls.name}")
    facts.plugins = _plugins_from_body(body)
    facts.usefixtures = _usefixtures_from_pytestmark(body)
    return facts


def _collect_class_fixtures(facts: ModuleFacts, cls: ast.ClassDef, class_id: str) -> None:
    fixtures: dict[str, Fixture] = {}
    for func in scope_functions(cls.body):
        is_fixture, explicit, autouse = _is_fixture(func)
        if is_fixture:
            name = explicit or func.name
            symbol = f"{class_id}.{func.name}"
            fixtures[name] = Fixture(name, symbol, autouse, _fixture_requests(func, True))
    facts.class_fixtures[class_id] = fixtures
    for inner in scope_classes(cls.body):
        _collect_class_fixtures(facts, inner, f"{class_id}.{inner.name}")


# --------------------------------------------------------------------------- discovery


class _Resolver:
    """Fixture lookup along pytest's scope chain for one test module."""

    def __init__(
        self,
        module: ModuleFacts,
        conftests: list[ModuleFacts],
        plugins: list[ModuleFacts],
        options: DiscoveryOptions,
        unresolved: Counter[str],
    ) -> None:
        self.module = module
        self.conftests = conftests
        self.plugins = plugins
        self.options = options
        self.unresolved = unresolved

    def chain(self, class_ids: list[str]) -> list[dict[str, Fixture]]:
        levels: list[dict[str, Fixture]] = []
        for class_id in reversed(class_ids):  # innermost class first
            levels.append(self.module.class_fixtures.get(class_id, {}))
        levels.append(self.module.fixtures)
        levels.extend(c.fixtures for c in self.conftests)
        levels.extend(p.fixtures for p in self.plugins)
        return levels

    def lifecycle(self, class_ids: list[str], requests: list[str]) -> list[str]:
        levels = self.chain(class_ids)
        deps: list[str] = []
        seen: set[str] = set()
        queue = list(requests)
        for level in levels:
            for fixture in level.values():
                if fixture.autouse:
                    queue.append(fixture.name)
        while queue:
            name = queue.pop(0)
            if name in seen:
                continue
            seen.add(name)
            fixture = next((lvl[name] for lvl in levels if name in lvl), None)
            if fixture is not None:
                deps.append(fixture.symbol)
                queue.extend(fixture.requests)
            elif name in self.options.external_fixtures or name in BUILTIN_FIXTURES:
                continue
            else:
                self.unresolved[name] += 1
                deps.append(f"fixture:{name}")
        return deps


def _conftest_chain(path: str, facts_by_path: dict[str, ModuleFacts]) -> list[ModuleFacts]:
    """conftest.py files from the test's directory outward, nearest first."""
    chain: list[ModuleFacts] = []
    directory = PurePosixPath(path).parent
    while True:
        candidate = str(directory / "conftest.py") if str(directory) != "." else "conftest.py"
        if candidate in facts_by_path:
            chain.append(facts_by_path[candidate])
        if str(directory) == ".":
            break
        directory = directory.parent
    return chain


def _is_unittest_class(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        parts, _ = decorator_chain(base)
        if parts and parts[-1].endswith("TestCase"):
            return True
    return False


def _has_init(cls: ast.ClassDef) -> bool:
    return any(f.name == "__init__" for f in scope_functions(cls.body))


def _under_testpaths(path: str, testpaths: tuple[str, ...]) -> bool:
    if not testpaths:
        return True
    for tp in testpaths:
        tp = tp.strip("/").rstrip("/")
        if tp in ("", "."):
            return True
        if path == tp or path.startswith(tp + "/"):
            return True
    return False


def discover_pytest(
    snapshot: Snapshot, index: SourceIndex, options: DiscoveryOptions
) -> DiscoveryResult:
    result = DiscoveryResult(runner=RUNNER)
    config = read_pytest_config(snapshot)
    result.config = {k: (list(v) if isinstance(v, tuple) else v) for k, v in config.items()}
    python_files = tuple(config["python_files"])

    test_paths = [
        p
        for p in snapshot.files
        if any(fnmatch(PurePosixPath(p).name, pat) for pat in python_files)
        and _under_testpaths(p, tuple(config["testpaths"]))
    ]
    conftest_paths = [p for p in snapshot.files if PurePosixPath(p).name == "conftest.py"]
    parsed, failed = parse_modules(snapshot, sorted(set(test_paths + conftest_paths)))
    for path in failed:
        result.notes.append(
            DiscoveryNote(RUNNER, "unparsed_file", f"{path}: not parsed or outside source roots")
        )
    facts_by_path = {pm.path: _collect_facts(pm) for pm in parsed}
    facts_by_module = {f.parsed.module: f for f in facts_by_path.values()}

    # pytest_plugins declared in conftests are global; those in a test module
    # apply to that module (pytest only honours them in the root conftest, but
    # we accept both and note out-of-scope ones).
    def plugin_facts(declared: list[str], where: str) -> list[ModuleFacts]:
        found: list[ModuleFacts] = []
        for name in declared:
            if name in facts_by_module:
                found.append(facts_by_module[name])
            elif name in index.modules:
                pm, _ = parse_modules(snapshot, [index.symbols[name].path])
                if pm:
                    facts_by_module[name] = _collect_facts(pm[0])
                    found.append(facts_by_module[name])
            else:
                result.notes.append(
                    DiscoveryNote(
                        RUNNER,
                        "plugin_out_of_scope",
                        f"{where}: pytest_plugins entry {name!r} is not within the source roots",
                    )
                )
        return found

    global_plugins: list[ModuleFacts] = []
    for path in sorted(conftest_paths):
        facts = facts_by_path.get(path)
        if facts is not None and facts.plugins:
            global_plugins.extend(plugin_facts(facts.plugins, path))

    unresolved: Counter[str] = Counter()
    for path in sorted(test_paths):
        facts = facts_by_path.get(path)
        if facts is None:
            continue
        conftests = _conftest_chain(path, facts_by_path)
        plugins = global_plugins + plugin_facts(facts.plugins, path)
        resolver = _Resolver(facts, conftests, plugins, options, unresolved)
        module_deps = [facts.parsed.module]
        module_deps += [c.parsed.module for c in conftests]
        for c in conftests:
            module_deps += c.hooks
        module_deps += facts.setup_functions
        _collect_module_tests(result, facts, resolver, config, module_deps, index)

    for name, count in sorted(unresolved.items()):
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unresolved_fixture",
                f"fixture {name!r} requested by {count} test(s) was not found in the source "
                "roots; its users are selected conservatively (declare it external to opt out)",
            )
        )
    result.targets.sort()
    return result


def _collect_module_tests(
    result: DiscoveryResult,
    facts: ModuleFacts,
    resolver: _Resolver,
    config: dict[str, Any],
    module_deps: list[str],
    index: SourceIndex,
) -> None:
    from diffcone.manifest import Target

    parsed = facts.parsed
    functions = tuple(config["python_functions"])
    classes = tuple(config["python_classes"])

    def add(nodeid: str, entry: str, class_ids: list[str], requests: list[str], extra: list[str]):
        if entry not in index.symbols:
            result.notes.append(
                DiscoveryNote(RUNNER, "missing_symbol", f"{nodeid}: {entry} is not in the index")
            )
        deps = module_deps + extra + resolver.lifecycle(class_ids, requests)
        result.targets.append(Target(RUNNER, nodeid, entry, tuple(sorted(set(deps)))))

    for func in scope_functions(parsed.tree.body):
        if not _matches(functions, func.name) or _is_fixture(func)[0]:
            continue
        requests = list(_fixture_requests(func, False))
        requests += _usefixtures_from_decorators(func.decorator_list)
        requests += facts.usefixtures
        add(f"{parsed.path}::{func.name}", f"{parsed.module}.{func.name}", [], requests, [])

    def walk_class(cls: ast.ClassDef, prefix_ids: list[str], nodeid_prefix: str) -> None:
        unittest_style = _is_unittest_class(cls)
        if not (unittest_style or _matches(classes, cls.name)) or _has_init(cls):
            return
        class_id = f"{prefix_ids[-1] if prefix_ids else parsed.module}.{cls.name}"
        class_ids = prefix_ids + [class_id]
        class_use = _usefixtures_from_decorators(cls.decorator_list)
        class_use += _usefixtures_from_pytestmark(cls.body)
        setup = [
            f"{class_id}.{f.name}"
            for f in scope_functions(cls.body)
            if f.name in CLASS_SETUP_METHODS
        ]
        for func in scope_functions(cls.body):
            if _is_fixture(func)[0]:
                continue
            if not (
                _matches(functions, func.name) or (unittest_style and func.name.startswith("test"))
            ):
                continue
            requests = list(_fixture_requests(func, True))
            requests += _usefixtures_from_decorators(func.decorator_list)
            requests += class_use
            requests += facts.usefixtures
            add(
                f"{nodeid_prefix}::{cls.name}::{func.name}",
                f"{class_id}.{func.name}",
                class_ids,
                requests,
                setup,
            )
        for inner in scope_classes(cls.body):
            walk_class(inner, class_ids, f"{nodeid_prefix}::{cls.name}")

    for cls in scope_classes(parsed.tree.body):
        walk_class(cls, [], parsed.path)

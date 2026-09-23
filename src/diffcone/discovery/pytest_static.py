"""Static pytest discovery.

Reproduces the collection rules below without importing test code. Anything
outside them is reported, never guessed.

Collected:

* files matching ``python_files`` (default ``test_*.py``, ``*_test.py``) under
  the source roots, restricted to ``testpaths`` when configured;
* module-level functions matching ``python_functions`` (default prefix
  ``test``), methods of classes matching ``python_classes`` (default prefix
  ``Test``) that have no ``__init__``, nested test classes, methods
  inherited from base classes defined in the same module or imported from
  one in the source roots, and methods of classes whose bases end in
  ``TestCase``;
* functions and classes imported into a test module (``from docs_src.app
  import test_read_main``) that match the naming rules, named by the bound
  name, with the defining symbol as entry; ``from <module> import *``
  brings in what the module's ``__all__`` lists, or every name it defines
  that does not start with an underscore (poetry's sync tests are the
  install tests, star-imported), except names this module defines itself;
  a name imported from outside the source roots is reported
  (``imported_test_out_of_scope``), since whether it yields tests is
  unknown (``unittest.TestCase`` yields none); a class
  that defines test methods but does not match ``python_classes`` is
  reported too (``uncollected_test_class``): a plugin may collect it, as
  SQLAlchemy's testing plugin collects ``<Name>Test``;
* configuration from ``pytest.ini``, ``pyproject.toml``
  (``[tool.pytest.ini_options]``), ``tox.ini`` or ``setup.cfg`` at the
  repository root.

Lifecycle dependencies attached to each test:

* fixtures requested by parameter name (excluding parameters with defaults,
  ``parametrize`` argnames unless ``indirect``, and arguments injected by
  ``mock.patch`` decorators on the function or, for ``test*`` methods, on
  its class and in-module base classes), by ``@pytest.mark.usefixtures`` on the
  function, class (including enclosing classes) or module (``pytestmark``),
  and transitively by other fixtures, resolved in pytest's order: class and
  its in-module bases, module, nearest ``conftest.py`` outward, then
  ``pytest_plugins`` modules within the source roots; a fixture requesting
  its own name resolves to the next definition outward;
* fixtures requested by literal name through ``request.getfixturevalue``;
* ``autouse`` fixtures visible from the test;
* the test module and its ``pytest_*`` hooks, every ``conftest.py`` on the
  path and each ``pytest_*`` hook function in those conftests;
* xunit-style setup/teardown functions and methods when present;
* for tests inherited from a base class defined in the same module, the
  collecting class itself. Bases defined elsewhere are reported.

Doctests, as pytest collects them: with ``--doctest-modules`` in
``addopts``, every docstring with examples in a collected module (the
module's, its functions', classes', and their methods' and nested classes'),
named ``path::module.Qualified.name`` with the owning symbol as entry and a
``dynamic:<module>`` lifecycle dependency (examples run with the module's
globals; plus one per in-scope module an example imports); text files
matching ``--doctest-glob`` (default ``test*.txt``) become targets whose
entry is not a symbol, so they are always selected.

Fixtures are recognised by a decorator whose dotted name ends in ``fixture``
(``@pytest.fixture``, ``@pytest.fixture(name=...)``, ``@fixture``,
``@pytest_asyncio.fixture``). Overriding follows nearest-scope-wins.
Fixture parametrisation, ``indirect`` parametrisation and dynamic
``request.getfixturevalue`` are not modelled. Fixtures from installed
plugins cannot be seen: a name found at no in-scope level that a well-known
plugin provides (``WELL_KNOWN_PLUGIN_FIXTURES``: ``mocker`` from
pytest-mock, ``httpx_mock`` from pytest-httpx, ...) or that was declared
external is assumed to come from that plugin and reported in an
``external_fixture`` note with its request count; any other unknown fixture
becomes the lifecycle dependency ``fixture:<name>`` so the planner selects
its users.
"""

from __future__ import annotations

import ast
import configparser
import doctest
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
    parse_modules,
    scope_assignments,
    scope_classes,
    scope_functions,
    string_literals,
)
from diffcone.indexer import DEF_NODES, iter_scope_statements, resolve_relative_module
from diffcone.manifest import Target
from diffcone.model import SourceIndex
from diffcone.snapshot import Snapshot

RUNNER = "pytest"

DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
DEFAULT_PYTHON_CLASSES = ("Test",)
DEFAULT_PYTHON_FUNCTIONS = ("test",)

# Fixtures provided by widely used pytest plugins, by distribution. A name
# defined at any in-scope level wins over this table; a project requesting
# one of these without the plugin installed fails at collection anyway, so
# assuming the plugin never hides a real dependency. Every use is reported.
_PLUGIN_FIXTURES: dict[str, tuple[str, ...]] = {
    "pytest-mock": ("mocker", "class_mocker", "module_mocker", "package_mocker", "session_mocker"),
    "pytest-subprocess": ("fp", "fake_process"),
    "pytest-httpx": ("httpx_mock",),
    "respx": ("respx_mock",),
    "requests-mock": ("requests_mock",),
    "pytest-responses": ("responses",),
    "pytest-httpserver": (
        "httpserver",
        "make_httpserver",
        "httpserver_listen_address",
        "httpserver_ssl_context",
    ),
    "pytest-localserver": ("smtpserver",),
    "pytest-httpbin": ("httpbin", "httpbin_secure", "httpbin_both", "httpbin_ca_bundle"),
    "pytest-recording": ("vcr", "vcr_config", "vcr_cassette", "vcr_cassette_name"),
    "pytest-freezer": ("freezer",),
    "time-machine": ("time_machine",),
    "pytest-asyncio": (
        "event_loop",
        "event_loop_policy",
        "unused_tcp_port",
        "unused_tcp_port_factory",
        "unused_udp_port",
        "unused_udp_port_factory",
    ),
    "anyio": (
        "anyio_backend",
        "anyio_backend_name",
        "anyio_backend_options",
        "free_tcp_port",
        "free_tcp_port_factory",
        "free_udp_port",
        "free_udp_port_factory",
    ),
    "pytest-trio": ("nursery",),
    "pytest-aiohttp": (
        "aiohttp_client",
        "aiohttp_server",
        "aiohttp_raw_server",
        "aiohttp_unused_port",
        "aiohttp_client_cls",
    ),
    "pytest-tornasync": ("io_loop", "http_client", "http_server", "http_server_port"),
    "pytest-xdist": ("worker_id", "testrun_uid"),
    "pytest-benchmark": ("benchmark", "benchmark_weave"),
    "pytest-cov": ("cov",),
    "pytest-django": (
        "db",
        "transactional_db",
        "django_db_reset_sequences",
        "django_db_serialized_rollback",
        "django_db_blocker",
        "django_db_setup",
        "django_db_keepdb",
        "django_db_createdb",
        "django_db_modify_db_settings",
        "django_db_use_migrations",
        "client",
        "async_client",
        "rf",
        "async_rf",
        "admin_client",
        "admin_user",
        "django_user_model",
        "django_username_field",
        "settings",
        "live_server",
        "django_assert_num_queries",
        "django_assert_max_num_queries",
        "django_capture_on_commit_callbacks",
        "mailoutbox",
        "django_mail_patch_dns",
        "django_mail_dnsname",
        "django_test_environment",
    ),
    "pytest-flask": (
        "client_class",
        "config",
        "request_ctx",
        "accept_json",
        "accept_jsonp",
        "accept_any",
        "accept_mimetype",
    ),
    "pytest-celery": (
        "celery_app",
        "celery_worker",
        "celery_session_app",
        "celery_session_worker",
        "celery_config",
        "celery_parameters",
        "celery_enable_logging",
        "celery_includes",
        "celery_worker_pool",
        "celery_worker_parameters",
    ),
    "pytest-regressions": (
        "data_regression",
        "file_regression",
        "num_regression",
        "image_regression",
        "dataframe_regression",
        "ndarrays_regression",
    ),
    "pytest-datadir": ("datadir", "shared_datadir", "original_datadir"),
    "pytest-datafiles": ("datafiles",),
    "syrupy": ("snapshot",),
    "pytest-golden": ("golden",),
    "pytest-textual-snapshot": ("snap_compare",),
    "pytest-socket": ("socket_enabled", "socket_disabled"),
    "pytest-console-scripts": ("script_runner",),
    "pytest-check": ("check",),
    "pytest-print": ("printer", "printer_session"),
    "pytest-structlog": ("log",),
    "pytest-metadata": ("metadata",),
    "pytest-base-url": ("base_url",),
    "pytest-variables": ("variables",),
    "pytest-qt": ("qtbot", "qapp", "qapp_args", "qapp_cls", "qtlog", "qtmodeltester"),
    "pytest-playwright": (
        "page",
        "browser",
        "context",
        "playwright",
        "browser_name",
        "browser_type",
        "browser_type_launch_args",
        "browser_context_args",
        "new_context",
    ),
    "pytest-selenium": (
        "selenium",
        "driver",
        "driver_class",
        "driver_args",
        "driver_kwargs",
        "driver_path",
        "chrome_options",
        "firefox_options",
        "capabilities",
        "session_capabilities",
    ),
    "pytest-docker": (
        "docker_ip",
        "docker_services",
        "docker_compose_file",
        "docker_compose_project_name",
        "docker_cleanup",
        "docker_setup",
    ),
    "pytest-postgresql": ("postgresql", "postgresql_proc", "postgresql_noproc"),
    "pytest-mysql": ("mysql", "mysql_proc", "mysql_noproc"),
    "pytest-redis": ("redisdb", "redis_proc", "redis_noproc"),
    "pytest-mongodb": ("mongodb",),
}
WELL_KNOWN_PLUGIN_FIXTURES: dict[str, str] = {
    name: dist for dist, names in _PLUGIN_FIXTURES.items() for name in names
}

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
        "entry_point_plugins": (),  # pytest11 entry points defined by the project itself
        "addopts_plugins": (),  # ``-p name`` entries in addopts
        "doctest_modules": False,  # ``--doctest-modules`` in addopts
        "doctest_globs": ("test*.txt",),  # ``--doctest-glob`` patterns
        "norecursedirs": DEFAULT_NORECURSEDIRS,
    }
    section: dict[str, Any] | None = None
    files = snapshot.config_files
    # pytest's precedence: pytest.ini, pyproject.toml, tox.ini, setup.cfg.
    if "pytest.ini" in files:
        # pytest treats any pytest.ini as *the* config file, even an empty one.
        section = _ini_section(files["pytest.ini"], "pytest") or {}
        config["source"] = "pytest.ini"
    if section is None and "pyproject.toml" in files:
        try:
            data = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
            tool = data.get("tool", {}).get("pytest", {})
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            tool = {}
        section = tool.get("ini_options")
        if section is not None:
            config["source"] = "pyproject.toml"
        elif tool:
            # pytest >= 9 native TOML table: [tool.pytest] with real TOML values.
            section = {k: v for k, v in tool.items() if k != "ini_options"}
            config["source"] = "pyproject.toml [tool.pytest]"
    if section is None and "tox.ini" in files:
        section = _ini_section(files["tox.ini"], "pytest")
        if section is not None:
            config["source"] = "tox.ini"
    if section is None and "setup.cfg" in files:
        section = _ini_section(files["setup.cfg"], "tool:pytest")
        if section is not None:
            config["source"] = "setup.cfg"
    if section:
        for key in (
            "python_files",
            "python_classes",
            "python_functions",
            "testpaths",
            "norecursedirs",
        ):
            if key in section:
                values = _split(section[key])
                if values:
                    config[key] = values
        addopts = _split(section.get("addopts", ""))
        config["addopts_plugins"] = tuple(_addopts_plugins(addopts))
        config["doctest_modules"] = "--doctest-modules" in addopts
        globs = _option_values(addopts, "--doctest-glob")
        if globs:
            config["doctest_globs"] = tuple(globs)
    config["entry_point_plugins"] = tuple(_entry_point_plugins(files))
    return config


def _entry_point_plugins(files: dict[str, bytes]) -> list[str]:
    """Modules the project registers as pytest plugins (``pytest11`` entry
    points in pyproject.toml or setup.cfg). pytest loads them for every test
    session, so their fixtures and hooks are visible everywhere."""
    modules: list[str] = []
    if "pyproject.toml" in files:
        try:
            data = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            data = {}
        entries = data.get("project", {}).get("entry-points", {}).get("pytest11", {})
        if isinstance(entries, dict):
            modules += [str(v).split(":", 1)[0].strip() for v in entries.values()]
        poetry = data.get("tool", {}).get("poetry", {}).get("plugins", {}).get("pytest11", {})
        if isinstance(poetry, dict):
            modules += [str(v).split(":", 1)[0].strip() for v in poetry.values()]
    if "setup.cfg" in files:
        section = _ini_section(files["setup.cfg"], "options.entry_points") or {}
        for line in str(section.get("pytest11", "")).splitlines():
            if "=" in line:
                modules.append(line.split("=", 1)[1].split(":", 1)[0].strip())
    return [m for m in dict.fromkeys(modules) if m]


BUILTIN_PLUGINS = frozenset({"pytester", "pytest", "_pytest"})


# pytest's default ``norecursedirs``.
DEFAULT_NORECURSEDIRS = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)


def _collected_dir(path: str, norecursedirs: tuple[str, ...]) -> bool:
    """Whether pytest recurses into every directory on ``path``."""
    directories = PurePosixPath(path).parts[:-1]
    return not any(fnmatch(part, pattern) for part in directories for pattern in norecursedirs)


def _option_values(addopts: tuple[str, ...], option: str) -> list[str]:
    """Values of ``--option=value`` / ``--option value`` in addopts."""
    values: list[str] = []
    tokens = list(addopts)
    for i, token in enumerate(tokens):
        if token.startswith(option + "="):
            values.append(token.split("=", 1)[1])
        elif token == option and i + 1 < len(tokens):
            values.append(tokens[i + 1])
    return values


def _addopts_plugins(addopts: tuple[str, ...]) -> list[str]:
    """Plugins loaded early through ``-p name`` / ``-pname`` in addopts
    (``-p no:name`` disables one and is ignored)."""
    names: list[str] = []
    tokens = list(addopts)
    for i, token in enumerate(tokens):
        name = None
        if token == "-p" and i + 1 < len(tokens):
            name = tokens[i + 1]
        elif token.startswith("-p") and len(token) > 2 and not token.startswith("--"):
            name = token[2:]
        if name and not name.startswith("no:") and name not in names:
            names.append(name)
    return names


# Bases from the standard library that contribute no test methods: worth no
# ``unknown_base_class`` note (scrapy's spiders are 133 subclasses of ``ABC``).
NO_TEST_BASES = frozenset(
    {
        "ABC",
        "ABCMeta",
        "BaseException",
        "Enum",
        "Exception",
        "Generic",
        "IntEnum",
        "NamedTuple",
        "Protocol",
        "StrEnum",
        "TypedDict",
        "object",
    }
)


def _star_names(tree: ast.Module) -> list[str]:
    """What ``from <module> import *`` binds: ``__all__`` when it is a literal
    list of strings, otherwise every name the module defines that does not
    start with an underscore. Names the module itself imported (which a star
    import without ``__all__`` re-exports) are not included."""
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets
        ):
            if isinstance(stmt.value, (ast.List, ast.Tuple)):
                names = [
                    e.value
                    for e in stmt.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if len(names) == len(stmt.value.elts):
                    return names
            return []
    return [n.name for n in tree.body if isinstance(n, DEF_NODES) and not n.name.startswith("_")]


def _absolute_module(parsed: ParsedModule, node: ast.ImportFrom) -> str:
    """Absolute module of a ``from ... import`` statement in ``parsed``."""
    return resolve_relative_module(
        parsed.module, parsed.path.endswith("__init__.py"), node.module, node.level
    )


def _ini_section(raw: bytes, name: str) -> dict[str, Any] | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(raw.decode("utf-8"))
    except (configparser.Error, UnicodeDecodeError):
        return None
    if not parser.has_section(name):
        return None
    return dict(parser.items(name))


def _matches_python_file(path: str, pattern: str) -> bool:
    """pytest's ``fnmatch_ex``: a pattern without a path separator matches the
    basename; one with a separator matches the whole (repo-relative) path."""
    if "/" in pattern:
        pattern = pattern.lstrip("./")
        return fnmatch(path, pattern)
    return fnmatch(PurePosixPath(path).name, pattern)


# Module-level names pytest reads from a test module or conftest; when they are
# variable symbols, every test in the module depends on them.
MODULE_LEVEL_PYTEST_NAMES = (
    "pytestmark",
    "pytest_plugins",
    "collect_ignore",
    "collect_ignore_glob",
)


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


@dataclass(frozen=True)
class Marks:
    """The mark-derived facts that flow from module to class to function."""

    usefixtures: tuple[str, ...] = ()
    parametrized: frozenset[str] = frozenset()  # argnames supplied by parametrize
    indirect: frozenset[str] = frozenset()  # parametrized names that are still fixtures

    def __add__(self, other: Marks) -> Marks:
        return Marks(
            self.usefixtures + other.usefixtures,
            self.parametrized | other.parametrized,
            self.indirect | other.indirect,
        )


NO_MARKS = Marks()


def _split_argnames(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [n.strip() for n in node.value.replace(",", " ").split() if n.strip()]
    return string_literals([node])


def _marks_from_expressions(exprs: list[ast.expr]) -> Marks:
    use: list[str] = []
    parametrized: set[str] = set()
    indirect: set[str] = set()
    for expr in exprs:
        parts, call = decorator_chain(expr)
        if call is None or len(parts) < 2 or parts[-2] != "mark":
            continue
        if parts[-1] == "usefixtures":
            use.extend(string_literals(list(call.args)))
        elif parts[-1] == "parametrize" and call.args:
            names = _split_argnames(call.args[0])
            parametrized.update(names)
            ind = keyword_value(call, "indirect")
            if isinstance(ind, ast.Constant) and ind.value is True:
                indirect.update(names)
            elif isinstance(ind, (ast.List, ast.Tuple)):
                indirect.update(string_literals(list(ind.elts)))
    return Marks(tuple(use), frozenset(parametrized), frozenset(indirect))


def _marks_from_pytestmark(body: list[ast.stmt]) -> Marks:
    exprs: list[ast.expr] = []
    for name, value in scope_assignments(body):
        if name == "pytestmark":
            exprs += list(value.elts) if isinstance(value, (ast.List, ast.Tuple)) else [value]
    return _marks_from_expressions(exprs)


def _usefixtures_from_decorators(decorators: list[ast.expr]) -> tuple[str, ...]:
    return _marks_from_expressions(decorators).usefixtures


def _usefixtures_from_pytestmark(body: list[ast.stmt]) -> tuple[str, ...]:
    return _marks_from_pytestmark(body).usefixtures


def _injected_patch_count(decorators: list[ast.expr]) -> int:
    """Number of ``mock.patch``/``patch.object`` decorators that inject an argument."""
    count = 0
    for dec in decorators:
        parts, call = decorator_chain(dec)
        if call is None or not parts:
            continue
        if parts[-1] == "patch":
            positional_new = len(call.args) >= 2
        elif len(parts) >= 2 and parts[-2] == "patch" and parts[-1] == "object":
            positional_new = len(call.args) >= 3
        else:
            continue
        if not positional_new and keyword_value(call, "new") is None:
            count += 1
    return count


def _plugins_from_body(body: list[ast.stmt]) -> list[str]:
    for name, value in scope_assignments(body):
        if name == "pytest_plugins":
            return string_literals([value])
    return []


def _fixture_requests(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    is_method: bool,
    marks: Marks = NO_MARKS,
    class_injected: int = 0,
) -> tuple[str, ...]:
    """Parameter names pytest would look up as fixtures.

    Mirrors ``getfuncargnames``: drops ``self``, parameters with defaults,
    arguments injected by ``mock.patch`` decorators (``class_injected`` of
    them by decorators on the enclosing class and its bases, which
    ``unittest.mock`` applies to every ``test*`` method), and names supplied
    by ``parametrize`` unless they are marked ``indirect``. Fixtures the
    body asks for by literal name (``request.getfixturevalue("db")``, how
    pytest-django's autouse ``_django_db_marker`` reaches
    ``_django_db_helper``) count as requests too.
    """
    args = node.args
    positional = args.posonlyargs + args.args
    n_defaults = len(args.defaults)
    required = [a.arg for a in positional[: len(positional) - n_defaults]]
    required += [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is None]
    if is_method and required:
        required = required[1:]
    injected = _injected_patch_count(node.decorator_list)
    if node.name.startswith("test"):
        injected += class_injected
    if injected:
        required = required[injected:]
    skip = set(marks.parametrized - marks.indirect)
    # hypothesis ``@given``: keyword strategies fill parameters by name,
    # positional strategies fill the *last* parameters (from the right).
    given_positional, given_keywords = _given_arguments(node.decorator_list)
    skip |= given_keywords
    remaining = [p for p in required if p not in skip]
    if given_positional:
        skip |= set(remaining[len(remaining) - given_positional :])
    names = [p for p in required if p != "request" and p not in skip]
    return tuple(dict.fromkeys(names + _getfixturevalue_names(node)))


def _getfixturevalue_names(node: ast.AST) -> list[str]:
    """Fixture names requested as ``<request>.getfixturevalue("name")`` with a
    literal, anywhere in the body (nested functions included)."""
    found: list[str] = []
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "getfixturevalue"
            and inner.args
            and isinstance(inner.args[0], ast.Constant)
            and isinstance(inner.args[0].value, str)
        ):
            found.append(inner.args[0].value)
    return found


def _given_arguments(decorators: list[ast.expr]) -> tuple[int, set[str]]:
    positional = 0
    keywords: set[str] = set()
    for dec in decorators:
        parts, call = decorator_chain(dec)
        if call is not None and parts and parts[-1] == "given":
            positional += len(call.args)
            keywords |= {k.arg for k in call.keywords if k.arg is not None}
    return positional, keywords


def _collect_facts(parsed: ParsedModule) -> ModuleFacts:
    facts = ModuleFacts(parsed=parsed)
    body = parsed.tree.body
    for func in scope_functions(body):
        is_fixture, explicit, autouse = _is_fixture(func)
        symbol = parsed.member_id(func.name)
        if is_fixture:
            name = explicit or func.name
            facts.fixtures[name] = Fixture(name, symbol, autouse, _fixture_requests(func, False))
        elif func.name.startswith("pytest_"):
            facts.hooks.append(symbol)
        elif func.name in MODULE_SETUP_FUNCTIONS:
            facts.setup_functions.append(symbol)
    # ``mocker = pytest.fixture(scope="function")(_mocker)``: a fixture made by
    # calling the decorator on an in-module function and binding the result.
    functions = {f.name: f for f in scope_functions(body)}
    for name, value in scope_assignments(body):
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Call):
            continue
        parts, call = decorator_chain(value.func)
        if not parts or parts[-1] not in ("fixture", "yield_fixture") or len(value.args) != 1:
            continue
        target = value.args[0]
        if not (isinstance(target, ast.Name) and target.id in functions):
            continue
        func = functions[target.id]
        explicit = keyword_value(call, "name")
        fixture_name = (
            explicit.value
            if isinstance(explicit, ast.Constant) and isinstance(explicit.value, str)
            else name
        )
        autouse_node = keyword_value(call, "autouse")
        autouse = isinstance(autouse_node, ast.Constant) and bool(autouse_node.value)
        facts.fixtures[fixture_name] = Fixture(
            fixture_name, parsed.member_id(func.name), autouse, _fixture_requests(func, False)
        )
    for cls in scope_classes(body):
        _collect_class_fixtures(facts, cls, parsed.member_id(cls.name))
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
        assumed: Counter[str],
    ) -> None:
        self.module = module
        self.conftests = conftests
        self.plugins = plugins
        self.options = options
        self.unresolved = unresolved
        self.assumed = assumed  # external fixture name -> requesting tests

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
        seen: set[tuple[str, int]] = set()
        # (name, first level to search): a fixture that requests its own name
        # (``def db(db)``) refers to the next definition outward.
        queue: list[tuple[str, int]] = [(name, 0) for name in requests]
        for level in levels:
            for fixture in level.values():
                if fixture.autouse:
                    queue.append((fixture.name, 0))
        while queue:
            name, start = queue.pop(0)
            if (name, start) in seen:
                continue
            seen.add((name, start))
            found = next(
                ((i, lvl[name]) for i, lvl in enumerate(levels) if i >= start and name in lvl),
                None,
            )
            if found is not None:
                level_index, fixture = found
                deps.append(fixture.symbol)
                for req in fixture.requests:
                    queue.append((req, level_index + 1 if req == name else 0))
            elif name in BUILTIN_FIXTURES:
                continue
            elif name in self.options.external_fixtures or (
                self.options.well_known_fixtures and name in WELL_KNOWN_PLUGIN_FIXTURES
            ):
                self.assumed[name] += 1
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
    """Whether ``path`` lies under one of pytest's ``testpaths`` entries.

    Entries may be files, directories or globs (``tests/integ*``); a leading
    ``./`` is ignored.
    """
    if not testpaths:
        return True
    parents = [str(p) for p in PurePosixPath(path).parents if str(p) != "."]
    for raw in testpaths:
        tp = raw.strip()
        while tp.startswith("./"):
            tp = tp[2:]
        tp = tp.strip("/")
        if tp in ("", "."):
            return True
        if any(ch in tp for ch in "*?["):
            if fnmatch(path, tp) or any(fnmatch(parent, tp) for parent in parents):
                return True
        elif path == tp or path.startswith(tp + "/"):
            return True
    return False


def _reexported_facts(facts: ModuleFacts, module_facts) -> list[ModuleFacts]:
    """Fixtures and hooks a plugin module exposes by importing them from
    submodules (``from .plugin import mocker``): pytest registers whatever the
    entry module's namespace holds, so follow one level of ``from`` imports,
    merged per submodule. Names are matched on what is imported (the original
    name, not an alias: a fixture keeps its own name); a hook counts only when
    it is imported too."""
    imported: dict[str, set[str] | None] = {}  # submodule -> names, None for *
    for stmt in iter_scope_statements(facts.parsed.tree.body):
        if not isinstance(stmt, ast.ImportFrom):
            continue
        base = _absolute_module(facts.parsed, stmt)
        if not base:
            continue
        names = {a.name for a in stmt.names}
        if "*" in names:
            imported[base] = None
        elif imported.get(base, set()) is not None:
            imported.setdefault(base, set()).update(names)
    out: list[ModuleFacts] = []
    for base, names in imported.items():
        sub = module_facts(base)
        if sub is None or sub is facts:
            continue
        if names is None:
            out.append(sub)
            continue
        visible = ModuleFacts(parsed=sub.parsed)
        visible.fixtures = {
            n: f
            for n, f in sub.fixtures.items()
            if n in names or f.symbol.rsplit(".", 1)[-1] in names
        }
        visible.hooks = [h for h in sub.hooks if h.rsplit(".", 1)[-1] in names]
        if visible.fixtures or visible.hooks:
            out.append(visible)
    return out


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
        if any(_matches_python_file(p, pat) for pat in python_files)
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
    def module_facts(name: str) -> ModuleFacts | None:
        if name in facts_by_module:
            return facts_by_module[name]
        if name in index.modules:
            pm, _ = parse_modules(snapshot, [index.symbols[name].path])
            if pm:
                facts_by_module[name] = _collect_facts(pm[0])
                return facts_by_module[name]
        return None

    def plugin_facts(
        declared: list[str], where: str, kind: str = "pytest_plugins"
    ) -> list[ModuleFacts]:
        found: list[ModuleFacts] = []
        for name in declared:
            # pytest's own plugins are builtin unless this repository *is*
            # pytest and ships them under _pytest.
            if name not in index.modules and f"_pytest.{name}" in index.modules:
                name = f"_pytest.{name}"
            if name.split(".")[0] in BUILTIN_PLUGINS and name not in index.modules:
                continue
            facts = module_facts(name)
            if facts is None:
                result.notes.append(
                    DiscoveryNote(
                        RUNNER,
                        "plugin_out_of_scope",
                        f"{where}: {kind} entry {name!r} is not within the source roots",
                    )
                )
                continue
            found.append(facts)
            found.extend(_reexported_facts(facts, module_facts))
        return found

    global_plugins: list[ModuleFacts] = []
    if config["entry_point_plugins"]:
        global_plugins.extend(
            plugin_facts(list(config["entry_point_plugins"]), "pyproject/setup.cfg", "pytest11")
        )
    if config["addopts_plugins"]:
        global_plugins.extend(plugin_facts(list(config["addopts_plugins"]), "addopts", "-p"))
    for path in sorted(conftest_paths):
        facts = facts_by_path.get(path)
        if facts is not None and facts.plugins:
            global_plugins.extend(plugin_facts(facts.plugins, path))
    plugin_hooks = [h for p in global_plugins for h in p.hooks]

    unresolved: Counter[str] = Counter()
    assumed: Counter[str] = Counter()
    # Filled while walking every module: class symbols followed as a base,
    # and (symbol, detail) for each class pytest's rules skipped.
    used_as_base: set[str] = set()
    uncollected: list[tuple[str, str]] = []
    for path in sorted(test_paths):
        facts = facts_by_path.get(path)
        if facts is None:
            continue
        conftests = _conftest_chain(path, facts_by_path)
        plugins = global_plugins + plugin_facts(facts.plugins, path)
        resolver = _Resolver(facts, conftests, plugins, options, unresolved, assumed)
        module_deps = [facts.parsed.module]
        module_deps += [c.parsed.module for c in conftests]
        for c in conftests:
            module_deps += c.hooks
        module_deps += facts.hooks  # e.g. pytest_generate_tests in the test module
        module_deps += plugin_hooks  # hooks of the project's own pytest plugins
        module_deps += facts.setup_functions
        for owner in (facts, *conftests):
            for name in MODULE_LEVEL_PYTEST_NAMES:
                symbol = owner.parsed.member_id(name)
                if symbol in index.symbols:
                    module_deps.append(symbol)
        _collect_module_tests(
            result,
            facts,
            resolver,
            config,
            module_deps,
            index,
            module_facts,
            used_as_base,
            uncollected,
        )

    # A class pytest's own rules skip is only worth reporting if nothing
    # collected it through inheritance either, which is known once every
    # module has been walked (networkx's ``BaseGraphTester`` is a base in
    # another module, alembic's ``BatchApplyTest`` is a base nowhere).
    for symbol, detail in uncollected:
        if symbol not in used_as_base:
            result.notes.append(DiscoveryNote(RUNNER, "uncollected_test_class", detail))

    _collect_doctests(result, snapshot, index, config, facts_by_path)

    for name, count in sorted(unresolved.items()):
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unresolved_fixture",
                f"fixture {name!r} requested by {count} test(s) was not found in the source "
                "roots; its users are selected conservatively (declare it external to opt out)",
            )
        )
    for name, count in sorted(assumed.items()):
        origin = (
            "declared external"
            if name in options.external_fixtures
            else f"assumed from the installed plugin {WELL_KNOWN_PLUGIN_FIXTURES[name]}"
        )
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "external_fixture",
                f"fixture {name!r} requested by {count} test(s) is not in the source roots and "
                f"was {origin}; it is not a dependency of its users",
            )
        )
    result.targets.sort()
    return result


def _collect_doctests(
    result: DiscoveryResult,
    snapshot: Snapshot,
    index: SourceIndex,
    config: dict[str, Any],
    facts_by_path: dict[str, ModuleFacts],
) -> None:
    """Doctest targets (see the module docstring)."""
    testpaths = tuple(config["testpaths"])
    norecurse = tuple(config["norecursedirs"])
    parser = doctest.DocTestParser()
    for path, content in sorted(snapshot.text_files.items()):
        name = PurePosixPath(path).name
        if not (
            _under_testpaths(path, testpaths)
            and _collected_dir(path, norecurse)
            and any(fnmatch(name, g) for g in config["doctest_globs"])
        ):
            continue
        try:
            has_examples = bool(parser.get_examples(content.decode("utf-8", "replace")))
        except ValueError:
            has_examples = True  # pytest reports a malformed file as a failing item
        if has_examples:
            # Not Python: a change to it is invisible, so it is always selected.
            result.targets.append(Target(RUNNER, f"{path}::{name}", f"doctest-file:{path}", ()))
    if not config["doctest_modules"]:
        return
    paths = [
        p
        for p in snapshot.files
        if _under_testpaths(p, testpaths)
        and _collected_dir(p, norecurse)
        and PurePosixPath(p).name not in ("setup.py", "__main__.py")
    ]
    parsed, _ = parse_modules(snapshot, sorted(paths))
    for pm in parsed:
        conftests = _conftest_chain(pm.path, facts_by_path)
        base_deps = [c.parsed.module for c in conftests] + [h for c in conftests for h in c.hooks]
        for qualname, symbol, docstring in _docstrings(pm):
            try:
                examples = parser.get_examples(docstring)
            except ValueError:
                examples = None  # pytest reports it as a failing item
            if examples == []:
                continue
            deps = [*base_deps, f"dynamic:{pm.module}"]
            if examples is None:
                deps.append("doctest:unparsed")
            else:
                for module in _example_imports(examples):
                    if module is None:
                        deps.append("doctest:unparsed")
                    elif module in index.modules:
                        deps.append(f"dynamic:{module}")
            name = pm.module if not qualname else f"{pm.module}.{qualname}"
            result.targets.append(
                Target(RUNNER, f"{pm.path}::{name}", symbol, tuple(sorted(set(deps))))
            )


def _docstrings(pm: ParsedModule) -> list[tuple[str, str, str]]:
    """(qualified name, symbol id, docstring) of every object doctest's finder
    visits: the module, its functions and classes, and recursively their
    methods and nested classes."""
    found: list[tuple[str, str, str]] = []
    doc = ast.get_docstring(pm.tree, clean=False)
    if doc:
        found.append(("", pm.module, doc))

    def walk(body: list[ast.stmt], prefix: str, container: str | None) -> None:
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            qual = f"{prefix}.{node.name}" if prefix else node.name
            symbol = f"{container}.{node.name}" if container else pm.member_id(node.name)
            doc = ast.get_docstring(node, clean=False)
            if doc:
                found.append((qual, symbol, doc))
            if isinstance(node, ast.ClassDef):
                walk(node.body, qual, symbol)

    walk(pm.tree.body, "", None)
    return found


def _example_imports(examples: list[doctest.Example]) -> list[str | None]:
    """Absolute modules the examples import; None for an example that does
    not parse (what it uses is unknown)."""
    modules: list[str | None] = []
    for example in examples:
        try:
            tree = ast.parse(example.source)
        except SyntaxError:
            modules.append(None)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules.append(node.module)
                modules += [f"{node.module}.{a.name}" for a in node.names]
    return modules


def _collect_module_tests(
    result: DiscoveryResult,
    facts: ModuleFacts,
    resolver: _Resolver,
    config: dict[str, Any],
    module_deps: list[str],
    index: SourceIndex,
    module_facts: Any = None,
    used_as_base: set[str] | None = None,
    uncollected: list[tuple[str, str]] | None = None,
) -> None:
    parsed = facts.parsed
    functions = tuple(config["python_functions"])
    classes = tuple(config["python_classes"])
    module_marks = _marks_from_pytestmark(parsed.tree.body)
    module_classes = {c.name: c for c in scope_classes(parsed.tree.body)}
    # Names used as a base anywhere in the module: such a class contributes
    # its methods through its subclasses, so it is not an uncollected class.
    base_names = {
        (decorator_chain(base)[0] or [""])[-1]
        for node in ast.walk(parsed.tree)
        if isinstance(node, ast.ClassDef)
        for base in node.bases
    }

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
        marks = module_marks + _marks_from_expressions(func.decorator_list)
        requests = list(_fixture_requests(func, False, marks)) + list(marks.usefixtures)
        add(f"{parsed.path}::{func.name}", parsed.member_id(func.name), [], requests, [])

    # pytest collects every module attribute matching the naming rules,
    # including functions and classes imported from elsewhere (fastapi's
    # tutorial tests import ``test_read_main`` from ``docs_src``).
    defined = {n.name for n in parsed.tree.body if isinstance(n, DEF_NODES)}
    for stmt in iter_scope_statements(parsed.tree.body):
        if not isinstance(stmt, ast.ImportFrom):
            continue
        source = _absolute_module(parsed, stmt)
        aliases = list(stmt.names)
        if any(a.name == "*" for a in aliases):
            # ``from tests.test_install import *`` re-runs another module's
            # tests here (poetry's sync command does exactly this).
            origin = module_facts(source) if module_facts is not None else None
            if origin is None:
                result.notes.append(
                    DiscoveryNote(
                        RUNNER,
                        "imported_test_out_of_scope",
                        f"{parsed.path}: ``from {source} import *`` names a module outside "
                        "the source roots; any tests pytest collects through it are not targets",
                    )
                )
                continue
            aliases = [ast.alias(name=n, asname=None) for n in _star_names(origin.parsed.tree)]
        for alias in aliases:
            bound = alias.asname or alias.name
            if bound in defined:
                continue
            is_function = _matches(functions, bound)
            is_class = _matches(classes, bound)
            if not (is_function or is_class):
                continue
            origin = module_facts(source) if module_facts is not None else None
            node = None
            if origin is not None:
                node = next(
                    (
                        n
                        for n in origin.parsed.tree.body
                        if isinstance(n, DEF_NODES) and n.name == alias.name
                    ),
                    None,
                )
            nodeid = f"{parsed.path}::{bound}"
            if node is None:
                # Outside the source roots (or not a definition there): whether
                # pytest collects anything from it is unknown (unittest's own
                # ``TestCase`` yields nothing), so it is reported, not guessed.
                if source.split(".")[0] != "unittest":
                    result.notes.append(
                        DiscoveryNote(
                            RUNNER,
                            "imported_test_out_of_scope",
                            f"{nodeid}: imported from {source}, outside the source roots; "
                            "any tests pytest collects from it are not targets",
                        )
                    )
                continue
            entry = origin.parsed.member_id(alias.name)
            if isinstance(node, ast.ClassDef):
                if not is_class or _has_init(node):
                    continue
                for method in scope_functions(node.body):
                    if _matches(functions, method.name) and not _is_fixture(method)[0]:
                        requests = list(_fixture_requests(method, True, module_marks))
                        add(f"{nodeid}::{method.name}", f"{entry}.{method.name}", [], requests, [])
            elif is_function and not _is_fixture(node)[0]:
                marks = module_marks + _marks_from_expressions(node.decorator_list)
                requests = list(_fixture_requests(node, False, marks)) + list(marks.usefixtures)
                add(nodeid, entry, [], requests, [origin.parsed.module])

    # Module scopes reached through base classes: name -> (parsed, classes,
    # imported class names). None for a module outside the source roots.
    scopes: dict[str, tuple[Any, dict[str, ast.ClassDef], dict[str, tuple[str, str]]] | None] = {}

    def scope_for(module: str) -> tuple[Any, dict[str, ast.ClassDef], dict[str, tuple[str, str]]]:
        if module in scopes:
            return scopes[module]  # type: ignore[return-value]
        scopes[module] = None  # guards import cycles while this one is built
        facts = module_facts(module) if module_facts is not None else None
        if facts is None:
            return None  # type: ignore[return-value]
        classes = {c.name: c for c in scope_classes(facts.parsed.tree.body)}
        imported: dict[str, tuple[str, str]] = {}
        for stmt in iter_scope_statements(facts.parsed.tree.body):
            if not isinstance(stmt, ast.ImportFrom):
                continue
            src = _absolute_module(facts.parsed, stmt)
            for alias in stmt.names:
                if alias.name == "*":
                    sub_scope = scope_for(src)
                    for name in sub_scope[1] if sub_scope else ():
                        imported.setdefault(name, (src, name))
                else:
                    imported[alias.asname or alias.name] = (src, alias.name)
        scopes[module] = (facts.parsed, classes, imported)
        return scopes[module]  # type: ignore[return-value]

    own_scope = (parsed, module_classes, (scope_for(parsed.module) or (None, {}, {}))[2])

    def mro(cls: ast.ClassDef, nodeid: str) -> list[tuple[ast.ClassDef, str]]:
        """Base classes, nearest first, with their symbol ids. A base defined
        in another module in the source roots is followed too (networkx's
        ``TestDiGraph(BaseGraphTester)``), and its own bases resolve in the
        module that defines it, not in this one."""
        chain: list[tuple[ast.ClassDef, str]] = []
        seen: set[tuple[str, str]] = set()
        queue = [(base, own_scope) for base in cls.bases]
        while queue:
            base, (owner, classes_here, imports_here) = queue.pop(0)
            parts, _ = decorator_chain(base)
            name = parts[-1] if parts else ""
            if name in ("object", "") or (owner.module, name) in seen:
                continue
            seen.add((owner.module, name))
            found = None
            if name in classes_here and not (owner.module == parsed.module and name == cls.name):
                found = (
                    classes_here[name],
                    owner.member_id(name),
                    (owner, classes_here, imports_here),
                )
            elif name in imports_here:
                source, original = imports_here[name]
                scope = scope_for(source)
                if scope is not None and original in scope[1]:
                    found = (scope[1][original], scope[0].member_id(original), scope)
            if found is not None:
                base_cls, base_id, base_scope = found
                if used_as_base is not None:
                    used_as_base.add(base_id)
                chain.append((base_cls, base_id))
                queue.extend((b, base_scope) for b in base_cls.bases)
            elif not (name.endswith("TestCase") or name in NO_TEST_BASES):
                result.notes.append(
                    DiscoveryNote(
                        RUNNER,
                        "unknown_base_class",
                        f"{nodeid}: base class {name!r} is not defined in this module or "
                        "imported from one in the source roots; test methods it may "
                        "contribute are not discovered",
                    )
                )
        return chain

    def walk_class(
        cls: ast.ClassDef, prefix_ids: list[str], nodeid_prefix: str, inherited: Marks
    ) -> None:
        unittest_style = _is_unittest_class(cls)
        if not (unittest_style or _matches(classes, cls.name)) or _has_init(cls):
            # A class pytest's own rules skip, but that defines test methods,
            # is a class some plugin collects (SQLAlchemy's testing plugin
            # collects ``<Name>Test``): report it rather than guess either way.
            if (
                cls.name not in base_names
                and not _has_init(cls)
                and any(
                    _matches(functions, f.name) and not _is_fixture(f)[0]
                    for f in scope_functions(cls.body)
                )
            ):
                symbol = (
                    f"{prefix_ids[-1]}.{cls.name}" if prefix_ids else parsed.member_id(cls.name)
                )
                detail = (
                    f"{nodeid_prefix}::{cls.name}: defines test methods but does not "
                    "match python_classes; a pytest plugin may collect it, and what it "
                    "collects is not a target"
                )
                if uncollected is not None:
                    uncollected.append((symbol, detail))
            return
        class_id = f"{prefix_ids[-1]}.{cls.name}" if prefix_ids else parsed.member_id(cls.name)
        nodeid = f"{nodeid_prefix}::{cls.name}"
        bases = mro(cls, nodeid)
        # Fixture lookup: this class, then its in-module bases, then outer classes.
        class_ids = prefix_ids + [b_id for _, b_id in reversed(bases)] + [class_id]
        class_marks = inherited + _marks_from_expressions(cls.decorator_list)
        class_marks = class_marks + _marks_from_pytestmark(cls.body)
        # ``@patch`` on a class (or on a base, whose patched methods are
        # inherited and patched again) injects into every ``test*`` method.
        class_injected = _injected_patch_count(cls.decorator_list) + sum(
            _injected_patch_count(owner.decorator_list) for owner, _ in bases
        )
        # Methods: own definitions win over inherited ones.
        methods: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = {}
        for owner, owner_id in reversed(bases):
            for f in scope_functions(owner.body):
                methods[f.name] = (f, owner_id)
        for f in scope_functions(cls.body):
            methods[f.name] = (f, class_id)
        setup = [
            f"{owner_id}.{name}"
            for name, (_, owner_id) in methods.items()
            if name in CLASS_SETUP_METHODS
        ]
        extra = setup + [class_id] if bases else setup
        for name, (func, owner_id) in sorted(methods.items()):
            if _is_fixture(func)[0]:
                continue
            if not (_matches(functions, name) or (unittest_style and name.startswith("test"))):
                continue
            marks = class_marks + _marks_from_expressions(func.decorator_list)
            requests = list(_fixture_requests(func, True, marks, class_injected))
            requests += list(marks.usefixtures)
            add(f"{nodeid}::{name}", f"{owner_id}.{name}", class_ids, requests, extra)
        for inner in scope_classes(cls.body):
            walk_class(inner, prefix_ids + [class_id], nodeid, class_marks)

    for cls in scope_classes(parsed.tree.body):
        walk_class(cls, [], parsed.path, module_marks)

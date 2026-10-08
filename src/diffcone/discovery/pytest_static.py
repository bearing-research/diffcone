"""Static pytest discovery.

Reproduces the collection rules below without importing test code. Anything
outside them is reported, never guessed.

Collected:

* files matching ``python_files`` (default ``test_*.py``, ``*_test.py``) under
  the source roots, restricted to ``testpaths`` when configured;
* module-level functions matching ``python_functions`` (default prefix
  ``test``), methods of classes matching ``python_classes`` (default prefix
  ``Test``) that have no ``__init__`` (a TestCase is collected with one),
  nested test classes (those a class inherits included), methods
  inherited from base classes defined in the same module or imported from
  one in the source roots (an ``alias.Class`` base resolves through that
  alias's module; a class a module only imports, as pandas's
  ``tests/extension/base/__init__.py`` re-exports its submodules' classes,
  is followed to the module that defines it), and methods of classes that reach a base ending in
  ``TestCase`` anywhere in that chain, whatever the class is called;
* functions and classes imported into a test module (``from docs_src.app
  import test_read_main``) that match the naming rules, named by the bound
  name, with the defining symbol as entry; an imported class is collected
  as a class defined here (inheritance, class fixtures, marks, xunit
  setup, nested classes), its bases resolved in the module that defines
  it, followed through re-exports to the module defining it; ``from
  <module> import *`` brings in what the module's ``__all__`` lists, or
  every public name it binds, imports included (poetry's sync tests are the
  install tests, star-imported), or both and every string named when
  ``__all__`` is computed, except names this module defines itself;
  a name imported from outside the source roots is reported
  (``imported_test_out_of_scope``), since whether it yields tests is
  unknown, except unittest's names, a test framework's ``*TestCase``
  (``from django.test import TestCase``) and a library's test helper pytest
  never collects (``LIBRARY_NON_TESTS``: ``from fastapi.testclient import
  TestClient``), which yield none; a class
  that defines test methods but does not match ``python_classes`` is
  reported too (``uncollected_test_class``): a plugin may collect it, as
  SQLAlchemy's testing plugin collects ``<Name>Test``;
* tests bound by assignment (``test_alias = test_orig``, a class attribute
  ``test_x = _check``) to a function or class defined there; one bound to
  something not visible there (``TestMachine = Machine.TestCase``) is
  reported (``unmodelled_test_binding``); a function marked
  ``f.__test__ = True``; a unittest ``runTest`` when a TestCase has no
  ``test*`` method; an imported TestCase whatever its bound name;
* ``testpaths`` as pytest applies it: entries that exist (none existing
  falls back to the rootdir), a file named there collected whatever
  ``python_files`` says; a test file pytest would collect outside the source
  roots is reported (``test_file_outside_roots``);
* configuration from ``pytest.toml``, ``.pytest.toml``, ``pytest.ini``,
  ``.pytest.ini``, ``pyproject.toml`` (``[tool.pytest.ini_options]`` or
  ``[tool.pytest]``), ``tox.ini`` or ``setup.cfg`` at the repository root,
  in pytest 9's order; INI values split as a shell would; an ``addopts``
  override of the configuration (``-o``, ``-c``, ``--rootdir``,
  ``--pyargs``) or path is reported (``unmodelled_runner_option``);
* only the conftests pytest loads: in a collected directory, or in one
  above a ``testpaths`` entry;
* a ``conftest.py`` outside the source roots is reported
  (``conftest_outside_roots``) and is an unknown dependency
  (``conftest:<path>``) of every test under it, so those tests are always
  selected.

Lifecycle dependencies attached to each test:

* fixtures requested by parameter name (excluding parameters with defaults,
  ``parametrize`` argnames unless ``indirect``, and arguments injected by
  ``mock.patch`` decorators on the function or, for ``test*`` methods, on
  its class and base classes), by ``@pytest.mark.usefixtures`` on the
  function, class (including enclosing classes and every base class, as
  pytest reads marks along the MRO) or module (``pytestmark``), whether
  written out or stored in a variable (``skip_pyarrow =
  pytest.mark.usefixtures("pyarrow_skip")`` then ``@skip_pyarrow``, also
  through aliases, ``from`` imports and ``module.name``, resolved in the
  module that defines the decorated function or class),
  and transitively by other fixtures, resolved in pytest's order: class
  (its own fixtures and those it inherits from bases in any module in the
  source roots, the nearest definition of an attribute hiding the rest, as
  ``dir(cls)`` sees them), enclosing classes, module, nearest
  ``conftest.py`` outward, then
  ``pytest_plugins`` modules within the source roots (and the plugins those
  declare, as pytest registers them); a fixture requesting
  its own name resolves to the next definition outward; a module offers a
  fixture under every name it binds the fixture to (``box2 = box``, ``from
  pkg.conftest import engine as motor``, a star import), as pytest registers
  it, except that a fixture with an explicit ``name=`` is offered under that
  name only; a name the test's marks parametrize directly is a parameter at
  every depth of the closure, not only among the test's own arguments
  (``set_engine(engine, ext)`` under ``parametrize("engine, ext")``), as
  pytest replaces any fixture of that name and prunes what it requests;
* fixtures requested by literal name through ``request.getfixturevalue``,
  and every fixture visible from the test when the name is not a literal;
* the ini option ``usefixtures``, requested by every test;
* ``autouse`` fixtures visible from the test (``autouse=`` anything but a
  literal false value, since ``autouse=FLAG`` applies whenever the flag is
  set);
* what pytest's own fixtures request (``BUILTIN_REQUESTS``: ``tmp_path``
  requests ``tmp_path_factory``), so an in-scope override of the requested
  name is a dependency of the tests using the builtin;
* in-scope overrides of fixtures that installed plugins request themselves
  (``PLUGIN_REQUESTED``: ``anyio_backend``, which anyio's plugin adds to
  async tests, ``django_db_setup``, ``event_loop_policy``) visible from the
  test, as if autouse, since the plugins cannot be read;
* the test module and its ``pytest_*`` hooks, every ``conftest.py`` on the
  path and each ``pytest_*`` hook function in those conftests; the hooks
  pytest calls for the whole session (any not in ``PATH_SCOPED_HOOKS``:
  ``pytest_collection_modifyitems``, ``pytest_configure``, ...) of every
  conftest, wherever it is; the hooks of the plugin modules the session
  loads (``pytest_plugins`` in a conftest or a test module, ``-p`` in
  addopts, ``pytest11`` entry points of the project or of a sibling package
  in the repository); a hook or setup function bound by import or
  assignment counts as one defined there;
* xunit-style setup/teardown functions and methods when present
  (``setUpModule``, ``asyncSetUp``, Django's ``setUpTestData`` and a
  class-level ``pytest_generate_tests`` included); a ``@staticmethod``
  test keeps its first parameter as a fixture request;
* for inherited tests, the collecting class itself. Bases that resolve
  nowhere are reported; a ``*TestCase`` base is excused only when it comes
  from a test framework (``TESTCASE_FRAMEWORKS``).

Doctests, as pytest collects them: with ``--doctest-modules`` in
``addopts``, every docstring with examples in a collected module (the
module's, its functions', classes', and their methods' and nested classes'),
named ``path::module.Qualified.name`` with the owning symbol as entry and a
``dynamic:<module>`` lifecycle dependency (examples run with the module's
globals; plus one per in-scope module an example imports), the session-wide
hooks, and the fixtures pytest gives a doctest: the module's and its
conftests' autouse fixtures, the ini ``usefixtures`` and
``doctest_namespace`` (which an autouse fixture usually fills); a module
outside the source roots is reported (``test_file_outside_roots``); text files
matching ``--doctest-glob`` (default ``test*.txt``) become targets whose
entry is not a symbol, so they are always selected.

Fixtures are recognised by a decorator whose dotted name ends in ``fixture``
(``@pytest.fixture``, ``@pytest.fixture(name=...)``, ``@fixture``,
``@pytest_asyncio.fixture``), or a call of one binding a module function
(``x = pytest.fixture(scope=...)(f)``, ``x = pytest.fixture(f)``).
Overriding follows nearest-scope-wins. Fixture parametrisation and
``indirect`` parametrisation are not modelled. Fixtures from installed
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
import shlex
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

from diffcone.discovery import DiscoveryNote, DiscoveryOptions, DiscoveryResult
from diffcone.discovery.common import (
    NO_GETFIXTUREVALUE,
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
from diffcone.snapshot import Snapshot, module_name_for

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

# What pytest's own fixtures request (pytest 9): an in-scope override of a
# requested name (pip overrides ``tmp_path_factory``) is what a test using
# the builtin really runs.
BUILTIN_REQUESTS: dict[str, tuple[str, ...]] = {
    "tmp_path": ("tmp_path_factory",),
    "tmpdir": ("tmp_path",),
    "testdir": ("pytester",),
    "pytester": ("tmp_path_factory", "monkeypatch"),
}

# Fixtures that installed plugins request themselves, without the test
# naming them, and that projects override to configure the plugin: anyio's
# plugin adds ``anyio_backend`` to async tests, pytest-django's database
# fixtures request ``django_db_setup``. The plugins cannot be read, so an
# in-scope override of one of these is a dependency of every test that can
# see it, as if autouse. Names tests request directly (``db``, ``client``)
# are not here: those are followed like any request.
PLUGIN_REQUESTED: dict[str, str] = {
    name: dist
    for dist, names in {
        "anyio": ("anyio_backend",),
        "pytest-asyncio": ("event_loop_policy", "event_loop"),
        "pytest-django": (
            "django_db_setup",
            "django_db_blocker",
            "django_db_keepdb",
            "django_db_createdb",
            "django_db_use_migrations",
            "django_db_modify_db_settings",
            "django_db_modify_db_settings_parallel_suffix",
            "django_db_modify_db_settings_tox_suffix",
            "django_db_modify_db_settings_xdist_suffix",
            "django_test_environment",
        ),
        "celery": (
            "celery_config",
            "celery_parameters",
            "celery_enable_logging",
            "celery_includes",
            "celery_worker_pool",
            "celery_worker_parameters",
        ),
        "pytest-playwright": ("browser_type_launch_args", "browser_context_args"),
        "pytest-selenium": (
            "driver_args",
            "driver_kwargs",
            "driver_class",
            "driver_path",
            "chrome_options",
            "firefox_options",
            "capabilities",
            "session_capabilities",
        ),
        "pytest-qt": ("qapp_args", "qapp_cls"),
        "pytest-docker": (
            "docker_compose_file",
            "docker_compose_project_name",
            "docker_cleanup",
            "docker_setup",
        ),
        "pytest-datadir": ("original_datadir",),
    }.items()
    for name in names
}

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
    "asyncSetUp",
    "asyncTearDown",
    "setUpTestData",
    # A class-level ``pytest_generate_tests`` parametrizes the class's tests.
    "pytest_generate_tests",
)
MODULE_SETUP_FUNCTIONS = (
    "setup_module",
    "teardown_module",
    "setup_function",
    "teardown_function",
    "setUpModule",
    "tearDownModule",
)

# Hooks pytest calls through a node's hook proxy, which consults only the
# conftests on that node's path. Any other hook a conftest defines
# (``pytest_collection_modifyitems``, ``pytest_configure``,
# ``pytest_sessionstart``, ...) runs for the whole session once the conftest
# is loaded, so it is a dependency of every test.
PATH_SCOPED_HOOKS = frozenset(
    {
        "pytest_runtest_setup",
        "pytest_runtest_call",
        "pytest_runtest_teardown",
        "pytest_runtest_makereport",
        "pytest_runtest_logreport",
        "pytest_runtest_logstart",
        "pytest_runtest_logfinish",
        "pytest_pyfunc_call",
        "pytest_generate_tests",
        "pytest_make_parametrize_id",
        "pytest_fixture_setup",
        "pytest_fixture_post_finalizer",
        "pytest_collect_file",
        "pytest_collect_directory",
        "pytest_pycollect_makemodule",
        "pytest_pycollect_makeitem",
        "pytest_ignore_collect",
        "pytest_collectstart",
        "pytest_make_collect_report",
        "pytest_itemcollected",
        "pytest_collectreport",
        "pytest_assertrepr_compare",
        "pytest_assertion_pass",
        "pytest_exception_interact",
    }
)


# --------------------------------------------------------------------------- config


# Test frameworks whose ``*TestCase`` bases contribute no tests of their own:
# such a base outside the source roots is not reported as unknown.
TESTCASE_FRAMEWORKS = frozenset(
    {
        "unittest",
        "django",
        "rest_framework",
        "twisted",
        "tornado",
        "asynctest",
        "aiounittest",
        "absl",
        "flask_testing",
        "pyfakefs",
        "aiohttp",
        "IPython",
    }
)


# Classes named like tests that libraries ship for tests to use, by module:
# each defines ``__init__``, so pytest never collects one (it warns and moves
# on), and importing one into a test module (``from fastapi.testclient import
# TestClient``) adds no test.
LIBRARY_NON_TESTS: dict[str, frozenset[str]] = {
    "starlette.testclient": frozenset({"TestClient"}),
    "fastapi.testclient": frozenset({"TestClient"}),
    "litestar.testing": frozenset({"TestClient"}),
    "falcon.testing": frozenset({"TestClient"}),
    "aiohttp.test_utils": frozenset({"TestClient", "TestServer"}),
    "werkzeug.test": frozenset({"TestResponse"}),
}


def _framework_base(
    name: str,
    parts: list[str],
    imports_here: dict[str, tuple[str, str]],
    module_prefixes: dict[str, dict[str, str]],
    owner: Any,
) -> bool:
    """Whether a base that resolves to nothing in scope is a test framework's
    ``TestCase`` (imported from one of TESTCASE_FRAMEWORKS), not a class
    discovery merely cannot see (``SharedTestCase = make_base()``)."""
    if len(parts) > 1:
        prefixes = module_prefixes.get(owner.module, {})
        source = prefixes.get(parts[0], parts[0])
    elif name in imports_here:
        source = imports_here[name][0]
    else:
        return False
    return source.split(".")[0] in TESTCASE_FRAMEWORKS


def _assigned(stmt: ast.stmt) -> tuple[str, ast.expr] | None:
    """``(name, value)`` for ``name = <value>``, else None."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if isinstance(target, ast.Name):
            return target.id, stmt.value
    return None


def _split(value: Any) -> tuple[str, ...]:
    """An option's values: a list as TOML gives it, or an INI string split
    the way pytest splits ``args`` options and ``addopts`` (shell-like, so
    ``--doctest-glob="*.rst"`` loses its quotes)."""
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError:  # an unbalanced quote: pytest would fail; be lenient
            return tuple(value.split())
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return ()


def _toml_table(data: Any, *keys: str) -> dict[str, Any] | None:
    """``data[keys[0]][keys[1]]...`` when every step is a table, else None."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, dict) else None


def _load_toml(raw: bytes) -> dict[str, Any]:
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


# Configuration that switches on an installed plugin's own collection of
# files discovery does not read: an ini key, or an option in addopts or
# after ``--``. Reported (``plugin_collects_files``); a recording settles it.
COLLECTING_PLUGIN_KEYS = {
    "typing_checkers": "pytest-typing (collects type-check cases from .md files)",
    "nb_test_files": "pytest-notebook (collects notebooks)",
    "doctest_plus": "pytest-doctestplus (collects doctests from text files)",
    "doctest_rst": "pytest-doctestplus (collects doctests from .rst files)",
}
COLLECTING_PLUGIN_OPTIONS = {
    "--nbval": "nbval (collects notebooks)",
    "--nbval-lax": "nbval (collects notebooks)",
    "--markdown-docs": "pytest-markdown-docs (collects code blocks from .md files)",
    "--doctest-rst": "pytest-doctestplus (collects doctests from .rst files)",
    "--doctest-plus": "pytest-doctestplus (collects doctests)",
    "--mypy-testing-base": "pytest-mypy-plugins (collects .yml cases)",
}

# pytest options that change what is collected in ways discovery does not
# model: given after ``--``, they make the target list possibly short.
UNMODELLED_COLLECTION_OPTIONS = (
    "-c",
    "--config-file",
    "-o",
    "--override-ini",
    "--rootdir",
    "--pyargs",
)


def read_pytest_config(snapshot: Snapshot, runner_args: tuple[str, ...] = ()) -> dict[str, Any]:
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
        "import_mode": "prepend",  # ``--import-mode`` in addopts
        "collecting_plugins": (),  # configured plugins with collection of their own
        "doctest_globs": ("test*.txt",),  # ``--doctest-glob`` patterns
        "norecursedirs": DEFAULT_NORECURSEDIRS,
        "usefixtures": (),  # the ini option: fixtures every test requests
        "ini_addopts": (),  # ``addopts`` as configured
        # ``--ignore`` / ``--ignore-glob`` in addopts or the run's arguments.
        "ignore": (),
        "ignore_glob": (),
    }
    section: dict[str, Any] | None = None
    files = snapshot.config_files
    # pytest's precedence (pytest 9): pytest.toml, .pytest.toml, pytest.ini,
    # .pytest.ini, pyproject.toml, tox.ini, setup.cfg. The first four are the
    # config file whenever present, even empty.
    for name in ("pytest.toml", ".pytest.toml"):
        if section is None and name in files:
            section = _toml_table(_load_toml(files[name]), "pytest") or {}
            config["source"] = name
    for name in ("pytest.ini", ".pytest.ini"):
        if section is None and name in files:
            section = _ini_section(files[name], "pytest") or {}
            config["source"] = name
    if section is None and "pyproject.toml" in files:
        tool = _toml_table(_load_toml(files["pyproject.toml"]), "tool", "pytest") or {}
        ini_options = tool.get("ini_options")
        if isinstance(ini_options, dict):
            section = ini_options
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
    if section is None and runner_args:
        section = {}
    if section is not None and (section or runner_args):
        for key in (
            "python_files",
            "python_classes",
            "python_functions",
            "testpaths",
            "norecursedirs",
            "usefixtures",
        ):
            if key in section:
                values = _split(section[key])
                if values:
                    config[key] = values
        config["ini_addopts"] = _split(section.get("addopts", ""))
        addopts = config["ini_addopts"] + tuple(runner_args)
        config["collecting_plugins"] = tuple(
            sorted(
                {COLLECTING_PLUGIN_KEYS[k] for k in section if k in COLLECTING_PLUGIN_KEYS}
                | {
                    COLLECTING_PLUGIN_OPTIONS[a.split("=", 1)[0]]
                    for a in addopts
                    if a.split("=", 1)[0] in COLLECTING_PLUGIN_OPTIONS
                }
            )
        )
        config["addopts_plugins"] = tuple(_addopts_plugins(addopts))
        config["doctest_modules"] = "--doctest-modules" in addopts
        modes = _option_values(addopts, "--import-mode")
        if modes:
            config["import_mode"] = modes[-1]
        globs = _option_values(addopts, "--doctest-glob")
        if globs:
            config["doctest_globs"] = tuple(globs)
        config["ignore"] = tuple(
            _normalise_testpath(v) for v in _option_values(addopts, "--ignore")
        )
        config["ignore_glob"] = tuple(
            _normalise_testpath(v) for v in _option_values(addopts, "--ignore-glob")
        )
    config["entry_point_plugins"] = tuple(_entry_point_plugins(files))
    return config


def _entry_point_plugins(files: dict[str, bytes]) -> list[str]:
    """Modules the project registers as pytest plugins (``pytest11`` entry
    points in pyproject.toml or setup.cfg, the root's or a sibling package's
    in the repository, which a development environment installs too).
    pytest loads them for every test session, so their fixtures and hooks
    are visible everywhere."""
    modules: list[str] = []
    for path, raw in sorted(files.items(), key=lambda item: (item[0].count("/"), item[0])):
        name = PurePosixPath(path).name
        if name == "pyproject.toml":
            data = _load_toml(raw)
            for keys in (
                ("project", "entry-points", "pytest11"),
                ("tool", "poetry", "plugins", "pytest11"),
            ):
                entries = _toml_table(data, *keys) or {}
                modules += [str(v).split(":", 1)[0].strip() for v in entries.values()]
        elif name == "setup.cfg":
            section = _ini_section(raw, "options.entry_points") or {}
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


def _ignored(path: str, config: dict[str, Any]) -> bool:
    """Whether ``--ignore`` or ``--ignore-glob`` keeps pytest from collecting
    ``path``. pytest makes both absolute against the invocation directory
    (the repository root here): an ``--ignore`` path is a file or a
    directory, everything under which is skipped; an ``--ignore-glob``
    pattern is matched with ``fnmatch`` against the whole path, so ``*``
    crosses directories."""
    for entry in config.get("ignore", ()):
        if entry and (path == entry or path.startswith(entry + "/")):
            return True
    return any(fnmatch(path, glob) for glob in config.get("ignore_glob", ()) if glob)


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


def _unmodelled_addopts(addopts: tuple[str, ...], paths: set[str]) -> list[str]:
    """Entries of the configured ``addopts`` that change collection in ways
    discovery does not model: an override of the configuration (``-o``,
    ``-c``, ``--rootdir``, ``--pyargs``) or a path, which pytest collects
    from instead of ``testpaths`` (a file named there is collected whatever
    ``python_files`` says)."""
    found: list[str] = []
    for token in addopts:
        option = token.split("=", 1)[0]
        if option in UNMODELLED_COLLECTION_OPTIONS or (
            option.startswith("-o") and option != "-o" and not option.startswith("--")
        ):
            found.append(token)
        elif not token.startswith("-"):
            tp = _normalise_testpath(token.split("::", 1)[0])
            if tp not in ("", ".") and (tp in paths or any(p.startswith(tp + "/") for p in paths)):
                found.append(token)
    return found


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
    """What ``from <module> import *`` binds: ``__all__`` when it is set once
    to a literal list of strings; with no ``__all__``, every name the module
    binds at top level (definitions, imports and assignments alike) that does
    not start with an underscore; with an ``__all__`` built otherwise
    (``base.__all__ + [...]``, ``__all__ += [...]``, ``__all__.extend``),
    both and every string it names: a superset."""
    literal: list[str] | None = None
    dynamic = False
    strings: list[str] = []
    for stmt in iter_scope_statements(tree.body):
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                continue
            value = stmt.value
            if (
                not isinstance(stmt, ast.AugAssign)
                and literal is None
                and isinstance(value, (ast.List, ast.Tuple))
                and all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
                )
            ):
                literal = [e.value for e in value.elts if isinstance(e, ast.Constant)]
                continue
        elif not (
            isinstance(stmt, ast.Expr)
            and any(isinstance(n, ast.Name) and n.id == "__all__" for n in ast.walk(stmt))
        ):
            continue
        dynamic = True
        strings += [
            n.value
            for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
    if literal is not None and not dynamic:
        return literal
    bound: list[str] = []
    for stmt in iter_scope_statements(tree.body):
        if isinstance(stmt, DEF_NODES):
            bound.append(stmt.name)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            bound += [a.asname or a.name.split(".")[0] for a in stmt.names if a.name != "*"]
        elif (assigned := _assigned(stmt)) is not None:
            bound.append(assigned[0])
    public = [n for n in bound if not n.startswith("_")]
    return list(dict.fromkeys([*(literal or []), *strings, *public]))


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


# Hooks that turn files or objects into tests by a plugin's own rules, so the
# target list cannot be the suite (scrapy's docs/conftest.py binds
# ``pytest_collect_file`` to a Sybil instance). ``pytest_generate_tests`` is
# not one: it multiplies a test function that is already a target.
COLLECT_HOOKS = frozenset(
    {"pytest_collect_file", "pytest_collect_directory", "pytest_pycollect_makeitem"}
)


def _matches_python_file(path: str, pattern: str) -> bool:
    """pytest's ``fnmatch_ex``: a pattern without a path separator matches the
    basename; one with a separator matches the path. pytest matches absolute
    paths and so prefixes a relative pattern with ``*/``, which is what makes
    scrapy's ``test_*/__init__.py`` match ``tests/test_settings/__init__.py``;
    the path here is repo-relative, so both forms are tried."""
    if "/" in pattern:
        pattern = pattern.lstrip("./")
        return fnmatch(path, pattern) or fnmatch(path, f"*/{pattern}")
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
    hooks: list[str] = field(default_factory=list)
    # The hooks among them pytest calls for the whole session, not through a
    # node's path (see PATH_SCOPED_HOOKS).
    session_hooks: list[str] = field(default_factory=list)
    # Collection hooks this module binds (as a def or an assignment): they
    # make tests out of files or objects these rules do not model.
    collect_hooks: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    usefixtures: tuple[str, ...] = ()
    setup_functions: list[str] = field(default_factory=list)
    # Attribute name -> (fixture, whether it names itself): what pytest finds
    # bound in the module's namespace. Fixtures defined here start it; names
    # bound to them by alias or import are added by _link_fixtures.
    fixture_attrs: dict[str, tuple[Fixture, bool]] = field(default_factory=dict)
    # Module-level bindings that may bind a fixture, in order: ("alias",
    # name, "", other_name), ("import", name, module, imported_name) and
    # ("star", "", module, "").
    bindings: list[tuple[str, str, str, str]] = field(default_factory=list)


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
            return True, name, _autouse(keyword_value(call, "autouse"))
    return False, None, False


def _autouse(node: ast.expr | None) -> bool:
    """Whether ``autouse=`` may be true: anything but a literal false value
    (``autouse=HAS_BLOCKBUSTER`` is autouse whenever the flag is set)."""
    return node is not None and not (isinstance(node, ast.Constant) and not node.value)


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

    @property
    def supplied(self) -> frozenset[str]:
        """Names whose values ``parametrize`` supplies directly: they replace
        any fixture of that name throughout the test's closure."""
        return self.parametrized - self.indirect


NO_MARKS = Marks()


def _split_argnames(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [n.strip() for n in node.value.replace(",", " ").split() if n.strip()]
    return string_literals([node])


# Expands a decorator or ``pytestmark`` entry that names a stored mark
# (``@skip_pyarrow`` after ``skip_pyarrow = pytest.mark.usefixtures(...)``)
# into the mark expressions it stands for; None or empty when it names none.
MarkExpander = Callable[[ast.expr], "list[ast.expr] | None"]


def _marks_from_expressions(exprs: list[ast.expr], expand: MarkExpander | None = None) -> Marks:
    use: list[str] = []
    parametrized: set[str] = set()
    indirect: set[str] = set()
    if expand is not None:
        exprs = [e for x in exprs for e in (expand(x) or [x])]
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


def _marks_from_pytestmark(body: list[ast.stmt], expand: MarkExpander | None = None) -> Marks:
    exprs: list[ast.expr] = []
    for name, value in scope_assignments(body):
        if name == "pytestmark":
            exprs += list(value.elts) if isinstance(value, (ast.List, ast.Tuple)) else [value]
    return _marks_from_expressions(exprs, expand)


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
    if is_method and any(
        isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list
    ):
        is_method = False  # no ``self`` to drop
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
    skip = set(marks.supplied)
    # hypothesis ``@given``: keyword strategies fill parameters by name,
    # positional strategies fill the *last* parameters (from the right).
    given_positional, given_keywords = _given_arguments(node.decorator_list)
    skip |= given_keywords
    remaining = [p for p in required if p not in skip]
    if given_positional:
        skip |= set(remaining[len(remaining) - given_positional :])
    names = [p for p in required if p != "request" and p not in skip]
    return tuple(dict.fromkeys(names + _getfixturevalue_names(node)))


# A request for any fixture visible from the test: what a
# ``getfixturevalue`` whose argument is not a literal may ask for.
ANY_FIXTURE = "*"


def _getfixturevalue_names(node: ast.AST) -> list[str]:
    """Fixture names requested as ``<request>.getfixturevalue("name")`` with a
    literal, anywhere in the body (nested functions included); ANY_FIXTURE
    for one whose argument is not a literal (``getfixturevalue(name)`` over a
    parametrized list of fixture names)."""
    found: list[str] = []
    if getattr(node, NO_GETFIXTUREVALUE, False):
        return found  # its module's source never says getfixturevalue
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "getfixturevalue"
        ):
            arg = inner.args[0] if inner.args else keyword_value(inner, "argname")
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(arg.value)
            else:
                found.append(ANY_FIXTURE)
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
            fixture = Fixture(name, symbol, autouse, _fixture_requests(func, False))
            facts.fixtures[name] = fixture
            facts.fixture_attrs[func.name] = (fixture, explicit is not None)
        elif func.name.startswith("pytest_"):
            facts.hooks.append(symbol)
            if func.name not in PATH_SCOPED_HOOKS:
                facts.session_hooks.append(symbol)
            if func.name in COLLECT_HOOKS:
                facts.collect_hooks.append(func.name)
        elif func.name in MODULE_SETUP_FUNCTIONS:
            facts.setup_functions.append(symbol)
    # ``pytest_collect_file = Sybil(...).pytest()``: a collection hook bound
    # to a value rather than defined as a function.
    for name, _ in scope_assignments(body):
        if name in COLLECT_HOOKS and name not in facts.collect_hooks:
            facts.collect_hooks.append(name)
    # ``mocker = pytest.fixture(scope="function")(_mocker)``: a fixture made by
    # calling the decorator on an in-module function and binding the result;
    # ``x = pytest.fixture(f, autouse=True)`` is the same in one call.
    functions = {f.name: f for f in scope_functions(body)}
    for name, value in scope_assignments(body):
        if not isinstance(value, ast.Call):
            continue
        parts, call = decorator_chain(value.func if isinstance(value.func, ast.Call) else value)
        if not parts or parts[-1] not in ("fixture", "yield_fixture") or len(value.args) != 1:
            continue
        target = value.args[0]
        if not (isinstance(target, ast.Name) and target.id in functions):
            continue
        func = functions[target.id]
        explicit = keyword_value(call, "name")
        explicit_name = (
            explicit.value
            if isinstance(explicit, ast.Constant) and isinstance(explicit.value, str)
            else None
        )
        names_itself = explicit_name is not None
        fixture_name = explicit_name or name
        autouse = _autouse(keyword_value(call, "autouse"))
        fixture = Fixture(
            fixture_name, parsed.member_id(func.name), autouse, _fixture_requests(func, False)
        )
        facts.fixtures[fixture_name] = fixture
        facts.fixture_attrs[name] = (fixture, names_itself)
    # Names bound to a fixture defined elsewhere (``box2 = box``, ``from
    # pkg.conftest import engine``): pytest registers a fixture under every
    # name the module binds it to (_link_fixtures resolves them).
    for stmt in iter_scope_statements(body):
        if isinstance(stmt, ast.ImportFrom):
            source = _absolute_module(parsed, stmt)
            for alias in stmt.names:
                if alias.name == "*":
                    facts.bindings.append(("star", "", source, ""))
                else:
                    facts.bindings.append(
                        ("import", alias.asname or alias.name, source, alias.name)
                    )
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Name)
        ):
            facts.bindings.append(("alias", stmt.targets[0].id, "", stmt.value.id))
    facts.plugins = _plugins_from_body(body)
    facts.usefixtures = _usefixtures_from_pytestmark(body)
    return facts


def _class_level(owners: list[tuple[ast.ClassDef, str]]) -> dict[str, Fixture]:
    """The fixtures pytest registers for a class, which it reads from
    ``dir(cls)``: ``owners`` is the class and its resolved bases, nearest
    first, wherever they are defined. Each attribute comes from the nearest
    owner defining it, so an override hides the base's fixture (and a
    fixture requesting its own name reaches past both); two attributes
    registering one name resolve to the alphabetically last, as pytest
    registers them in ``dir`` order."""
    attrs: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = {}
    for owner, owner_id in owners:
        for func in scope_functions(owner.body):
            attrs.setdefault(func.name, (func, owner_id))
    level: dict[str, Fixture] = {}
    for attr, (func, owner_id) in sorted(attrs.items()):
        is_fixture, explicit, autouse = _is_fixture(func)
        if is_fixture:
            name = explicit or attr
            level[name] = Fixture(
                name, f"{owner_id}.{attr}", autouse, _fixture_requests(func, True)
            )
    return level


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
        usefixtures: tuple[str, ...] = (),
    ) -> None:
        self.module = module
        self.conftests = conftests
        self.plugins = plugins
        self.options = options
        self.unresolved = unresolved
        self.assumed = assumed  # external fixture name -> requesting tests
        # The ini option ``usefixtures``: requested by every test.
        self.usefixtures = usefixtures

    def plugin_requested(self, name: str) -> bool:
        """Whether an installed plugin requests this name itself
        (``PLUGIN_REQUESTED``): an in-scope override of it is then a
        dependency of every test it is visible to, like an autouse fixture."""
        return self.options.well_known_fixtures and name in PLUGIN_REQUESTED

    def chain(self, class_levels: list[dict[str, Fixture]]) -> list[dict[str, Fixture]]:
        levels = list(class_levels)  # innermost class first
        levels.append(self.module.fixtures)
        levels.extend(c.fixtures for c in self.conftests)
        levels.extend(p.fixtures for p in self.plugins)
        return levels

    def lifecycle(
        self,
        class_levels: list[dict[str, Fixture]],
        requests: list[str],
        supplied: frozenset[str] = frozenset(),
    ) -> list[str]:
        """Fixtures the test's closure reaches. A ``supplied`` name (directly
        parametrized on the test) is a parameter at every depth, as pytest
        replaces the fixture of that name and prunes what it requests."""
        levels = self.chain(class_levels)
        deps: list[str] = []
        seen: set[tuple[str, int]] = set()
        # (name, first level to search): a fixture that requests its own name
        # (``def db(db)``) refers to the next definition outward.
        queue: list[tuple[str, int]] = [(name, 0) for name in [*self.usefixtures, *requests]]
        for level in levels:
            for fixture in level.values():
                if fixture.autouse or self.plugin_requested(fixture.name):
                    queue.append((fixture.name, 0))
        while queue:
            name, start = queue.pop(0)
            if (name, start) in seen or name in supplied:
                continue
            seen.add((name, start))
            if name == ANY_FIXTURE:
                queue.extend((other, 0) for level in levels for other in level)
                continue
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
                queue.extend((req, 0) for req in BUILTIN_REQUESTS.get(name, ()))
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


def _normalise_testpath(entry: str) -> str:
    tp = entry.strip()
    while tp.startswith("./"):
        tp = tp[2:]
    return tp.strip("/")


def _testpath_exists(entry: str, paths: set[str]) -> bool:
    """Whether a ``testpaths`` entry names a file or directory in the tree."""
    tp = _normalise_testpath(entry)
    if tp in ("", "."):
        return True
    if any(ch in tp for ch in "*?["):
        return any(
            fnmatch(p, tp) or any(fnmatch(str(parent), tp) for parent in PurePosixPath(p).parents)
            for p in paths
        )
    return any(p == tp or p.startswith(tp + "/") for p in paths)


def _conftest_loaded(path: str, testpaths: tuple[str, ...], norecursedirs: tuple[str, ...]) -> bool:
    """Whether pytest loads the conftest at ``path``: it is in a directory
    pytest collects (under a ``testpaths`` entry, or anywhere without one),
    or in a directory above an entry (pytest loads those first)."""
    if not _collected_dir(path, norecursedirs):
        return False
    if _under_testpaths(path, testpaths):
        return True
    directory = str(PurePosixPath(path).parent)
    for raw in testpaths:
        tp = _normalise_testpath(raw)
        if any(ch in tp for ch in "*?["):
            return True  # a glob: where it matches is not worth guessing
        if directory == "." or tp.startswith(directory + "/"):
            return True
    return False


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


def _requested_names(trees: Iterable[ast.Module]) -> set[str]:
    """Every name a fixture could be requested by in these modules: function
    parameters, and identifier-like strings (``usefixtures("db")``,
    ``request.getfixturevalue("db")``). A superset, used only to skip
    bindings no test can ask for."""
    names: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.isidentifier():
                    names.add(node.value)
    return names


def _fixture_bound_to(
    module: str, attr: str, module_facts, seen: set[tuple[str, str]]
) -> tuple[Fixture, bool] | None:
    """The fixture ``module``'s namespace binds to ``attr``, with whether it
    names itself, following aliases, ``from`` imports and star imports (the
    last binding of a name wins; a fixture defined in the module wins over
    any binding)."""
    if (module, attr) in seen:
        return None
    seen.add((module, attr))
    facts = module_facts(module)
    if facts is None:
        return None
    if attr in facts.fixture_attrs:
        return facts.fixture_attrs[attr]
    for kind, bound, source, name in reversed(facts.bindings):
        if kind == "star":
            origin = module_facts(source)
            if origin is not None and attr in _star_names(origin.parsed.tree):
                return _fixture_bound_to(source, attr, module_facts, seen)
        elif bound == attr:
            return _fixture_bound_to(
                module if kind == "alias" else source, name, module_facts, seen
            )
    return None


def _function_bound_to(
    module: str, attr: str, module_facts, seen: set[tuple[str, str]]
) -> str | None:
    """The function ``module``'s namespace binds to ``attr``, following
    aliases, ``from`` imports and star imports as _fixture_bound_to does;
    None when it is not a function defined in the source roots."""
    if (module, attr) in seen:
        return None
    seen.add((module, attr))
    facts = module_facts(module)
    if facts is None:
        return None
    if any(f.name == attr for f in scope_functions(facts.parsed.tree.body)):
        return facts.parsed.member_id(attr)
    for kind, bound, source, name in reversed(facts.bindings):
        if kind == "star":
            origin = module_facts(source)
            if origin is not None and attr in _star_names(origin.parsed.tree):
                return _function_bound_to(source, attr, module_facts, seen)
        elif bound == attr:
            return _function_bound_to(
                module if kind == "alias" else source, name, module_facts, seen
            )
    return None


def _definition_bound_to(
    module: str, attr: str, module_facts, seen: set[tuple[str, str]]
) -> tuple[ModuleFacts, ast.AST] | str | None:
    """What ``module``'s namespace binds to ``attr``, following aliases,
    ``from`` imports and star imports: the defining module's facts and the
    ``def``/``class`` node when it is defined in the source roots; the
    module outside the roots it comes from (a string); or None (bound to
    something else, or not bound)."""
    if (module, attr) in seen:
        return None
    seen.add((module, attr))
    facts = module_facts(module)
    if facts is None:
        return module
    node = next(
        (n for n in facts.parsed.tree.body if isinstance(n, DEF_NODES) and n.name == attr), None
    )
    if node is not None:
        return facts, node
    for kind, bound, source, name in reversed(facts.bindings):
        if kind == "star":
            origin = module_facts(source)
            if origin is None or attr in _star_names(origin.parsed.tree):
                found = _definition_bound_to(source, attr, module_facts, seen)
                if found is not None:
                    return found
        elif bound == attr:
            return _definition_bound_to(
                module if kind == "alias" else source, name, module_facts, seen
            )
    return None


def _link_bound_hooks(facts: ModuleFacts, module_facts) -> None:
    """Hooks and xunit setup functions the module binds without defining
    them (``from tests.common import pytest_generate_tests``, ``setup_module
    = _setup``): pytest finds them in the namespace all the same."""
    defined = {f.name for f in scope_functions(facts.parsed.tree.body)}
    for kind, bound, _, _ in facts.bindings:
        if kind == "star" or bound in defined:
            continue
        if not (bound.startswith("pytest_") or bound in MODULE_SETUP_FUNCTIONS):
            continue
        symbol = _function_bound_to(facts.parsed.module, bound, module_facts, set())
        if symbol is None:
            continue
        if bound in MODULE_SETUP_FUNCTIONS:
            facts.setup_functions.append(symbol)
        else:
            facts.hooks.append(symbol)
            if bound not in PATH_SCOPED_HOOKS:
                facts.session_hooks.append(symbol)
            if bound in COLLECT_HOOKS and bound not in facts.collect_hooks:
                facts.collect_hooks.append(bound)
    # Star imports: any hook or setup name the origin offers.
    for kind, _, source, _ in facts.bindings:
        if kind != "star":
            continue
        origin = module_facts(source)
        for attr in _star_names(origin.parsed.tree) if origin is not None else ():
            if attr in defined or not (
                attr.startswith("pytest_") or attr in MODULE_SETUP_FUNCTIONS
            ):
                continue
            symbol = _function_bound_to(source, attr, module_facts, set())
            if symbol is None:
                continue
            if attr in MODULE_SETUP_FUNCTIONS:
                facts.setup_functions.append(symbol)
            else:
                facts.hooks.append(symbol)
                if attr not in PATH_SCOPED_HOOKS:
                    facts.session_hooks.append(symbol)


def _link_fixtures(facts: ModuleFacts, module_facts, requested: set[str]) -> None:
    """Register the fixtures this module binds to names of its own: pytest
    finds a fixture under every name bound to it in the module's namespace
    (``box2 = box``, ``from pkg.conftest import engine as motor``), except
    that a fixture with an explicit ``name=`` is found under that name only.
    Only names in ``requested`` are followed: any other cannot be asked for,
    and following it would parse every module a test imports from."""
    for kind, bound, source, _ in facts.bindings:
        if kind == "star":
            origin = module_facts(source)
            attrs = [n for n in _star_names(origin.parsed.tree) if n in requested] if origin else []
        else:
            attrs = [bound] if bound in requested else []
        for attr in attrs:
            if attr in facts.fixture_attrs:
                continue
            found = _fixture_bound_to(facts.parsed.module, attr, module_facts, set())
            if found is None:
                continue
            fixture, names_itself = found
            facts.fixture_attrs[attr] = found
            registered = fixture.name if names_itself else attr
            facts.fixtures.setdefault(registered, replace(fixture, name=registered))


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
        elif (known := imported.setdefault(base, set())) is not None:
            known.update(names)
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


# A module's scope for base-class lookups: (parsed module, its classes, imported
# class names -> (module, name)).
_Scope = tuple[Any, dict[str, ast.ClassDef], dict[str, tuple[str, str]]]


def discover_pytest(
    snapshot: Snapshot, index: SourceIndex, options: DiscoveryOptions
) -> DiscoveryResult:
    result = DiscoveryResult(runner=RUNNER)
    config = read_pytest_config(snapshot, options.runner_args)
    for plugin in config["collecting_plugins"]:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "plugin_collects_files",
                f"the configuration turns on {plugin}: what it collects is not a target",
            )
        )
    for arg in options.runner_args:
        option = arg.split("=", 1)[0]
        if option in UNMODELLED_COLLECTION_OPTIONS or (
            option.startswith("-o") and option != "-o" and not option.startswith("--")
        ):
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "unmodelled_runner_option",
                    f"the run passes {arg!r} to pytest, which changes what it collects in a way "
                    "discovery does not model",
                )
            )
    every_path = {*snapshot.python_paths, *snapshot.files, *snapshot.other_files}
    for arg in _unmodelled_addopts(tuple(config["ini_addopts"]), every_path):
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unmodelled_runner_option",
                f"{config['source']}: addopts passes {arg!r} to pytest, which changes what it "
                "collects in a way discovery does not model",
            )
        )
    result.config = {k: (list(v) if isinstance(v, tuple) else v) for k, v in config.items()}
    python_files = tuple(config["python_files"])
    # ``testpaths`` as pytest uses it: entries that exist (when none does,
    # pytest collects from the rootdir), and files named there are collected
    # whatever ``python_files`` says (they are initial paths).
    testpaths = tuple(tp for tp in config["testpaths"] if _testpath_exists(tp, every_path))
    named_files = {_normalise_testpath(tp) for tp in testpaths if tp.endswith(".py")}

    test_paths = [
        p
        for p in snapshot.files
        if (any(_matches_python_file(p, pat) for pat in python_files) or p in named_files)
        and _under_testpaths(p, testpaths)
        and not _ignored(p, config)
    ]
    for path in snapshot.python_paths:
        if (
            path not in snapshot.files
            and any(_matches_python_file(path, pat) for pat in python_files)
            and _under_testpaths(path, testpaths)
            and _collected_dir(path, tuple(config["norecursedirs"]))
            and not _ignored(path, config)
        ):
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "test_file_outside_roots",
                    f"{path}: pytest collects it, but it is outside the source roots, so its "
                    "tests are not targets; add a source root that contains it (for a src "
                    "layout: --source-root src --source-root .)",
                    path,
                )
            )
    norecurse = tuple(config["norecursedirs"])
    # Only the conftests pytest loads: those on the way to a testpaths entry
    # and those in directories it collects. One elsewhere (a sibling
    # package's own test suite) is never imported.
    conftest_paths = [
        p
        for p in snapshot.files
        if PurePosixPath(p).name == "conftest.py"
        and _conftest_loaded(p, testpaths, norecurse)
        and not _ignored(str(PurePosixPath(p).parent), config)
    ]
    # A conftest outside the source roots is not read: its fixtures, hooks
    # and import-time code are invisible, so every test under it depends on
    # it as an unknown (selected, with this note saying why).
    outside_conftests = sorted(
        p
        for p in snapshot.python_paths
        if PurePosixPath(p).name == "conftest.py"
        and p not in snapshot.files
        and _conftest_loaded(p, testpaths, norecurse)
    )

    def outside_conftests_of(path: str) -> list[str]:
        """Unknown dependencies on the conftests outside the roots that
        apply to ``path``."""
        directories = {str(d) for d in PurePosixPath(path).parents}
        return [
            f"conftest:{c}"
            for c in outside_conftests
            if str(PurePosixPath(c).parent) in directories
        ]

    for path in outside_conftests:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "conftest_outside_roots",
                f"{path}: pytest loads it, but it is outside the source roots, so what it "
                "defines is unknown and every test under it is always selected; add a source "
                "root that contains it (for a src layout: --source-root src --source-root .)",
                path,
            )
        )
    parsed, failed = parse_modules(snapshot, sorted(set(test_paths + conftest_paths)))
    for path in failed:
        # Two reasons, and they need different answers: a file that does not
        # parse is also an analysis error (the plan degrades and selects
        # everything), while one that cannot be named is dropped silently --
        # pytest imports it by its basename, so its tests are collected and
        # are not targets (pytest-asyncio's ``docs/how-to-guides``).
        nameable = module_name_for(path, snapshot.source_roots) is not None
        detail = (
            f"{path}: did not parse"
            if nameable
            else (
                f"{path}: cannot be named from any source root (a directory component is not a "
                "Python identifier), so pytest collects it and it is not a target; name it with "
                "a DIR=PREFIX source root"
            )
        )
        result.notes.append(DiscoveryNote(RUNNER, "unparsed_file", detail, path))
    facts_by_path = {pm.path: _collect_facts(pm) for pm in parsed}
    facts_by_module = {f.parsed.module: f for f in facts_by_path.values()}

    def module_facts(name: str) -> ModuleFacts | None:
        if name in facts_by_module:
            return facts_by_module[name]
        if name in index.modules:
            pm, _ = parse_modules(snapshot, [index.symbols[name].path])
            if pm:
                facts_by_module[name] = _collect_facts(pm[0])
                return facts_by_module[name]
        return None

    # Names bound to fixtures defined elsewhere. Plugin modules are linked
    # where plugin_facts finds them, with their own requests added.
    requested = _requested_names(f.parsed.tree for f in facts_by_path.values())
    linked: set[str] = set()

    def link(facts: ModuleFacts) -> ModuleFacts:
        if facts.parsed.module not in linked:
            linked.add(facts.parsed.module)
            requested.update(_requested_names([facts.parsed.tree]))
            _link_fixtures(facts, module_facts, requested)
            _link_bound_hooks(facts, module_facts)
        return facts

    for facts in list(facts_by_path.values()):
        link(facts)

    def plugin_facts(
        declared: list[str],
        where: str,
        kind: str = "pytest_plugins",
        seen: set[str] | None = None,
    ) -> list[ModuleFacts]:
        """The plugin modules ``declared`` loads, and those they declare in
        turn: pytest registers each module plugin, then its own
        ``pytest_plugins`` (poetry's conftest loads a package whose
        ``__init__`` lists the modules holding its autouse fixtures)."""
        seen = set() if seen is None else seen
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
            if facts.parsed.module in seen:
                continue
            seen.add(facts.parsed.module)
            found.append(link(facts))
            found.extend(link(sub) for sub in _reexported_facts(facts, module_facts))
            if facts.plugins:
                found.extend(plugin_facts(facts.plugins, facts.parsed.path, seen=seen))
        return found

    global_plugins: list[ModuleFacts] = []
    if config["entry_point_plugins"]:
        global_plugins.extend(
            plugin_facts(list(config["entry_point_plugins"]), "pyproject/setup.cfg", "pytest11")
        )
    if config["addopts_plugins"]:
        global_plugins.extend(plugin_facts(list(config["addopts_plugins"]), "addopts", "-p"))
    # ``pytest_plugins`` in a conftest or a test module registers the plugin
    # for the session once pytest imports the file, so its fixtures and
    # hooks reach every test, not only those beside the declaration.
    plugins_seen: set[str] = set()
    for path in sorted(conftest_paths) + sorted(test_paths):
        facts = facts_by_path.get(path)
        if facts is not None and facts.plugins:
            global_plugins.extend(plugin_facts(facts.plugins, path, seen=plugins_seen))
    plugin_hooks = [h for p in global_plugins for h in p.hooks]
    ini_usefixtures = tuple(config["usefixtures"])
    # Hooks of conftests off a test's path that pytest calls for the whole
    # session (``pytest_collection_modifyitems`` in ``tests/a/conftest.py``
    # sees, and may reorder, skip or mark, the items of ``tests/b``).
    plugin_hooks += [
        h
        for path in sorted(conftest_paths)
        if path in facts_by_path
        for h in facts_by_path[path].session_hooks
    ]

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
        resolver = _Resolver(
            facts, conftests, global_plugins, options, unresolved, assumed, ini_usefixtures
        )
        module_deps = [facts.parsed.module]
        module_deps += outside_conftests_of(path)
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
            result.notes.append(
                DiscoveryNote(RUNNER, "uncollected_test_class", detail, detail.split("::", 1)[0])
            )

    # Collection hooks of conftests and test modules, and of the plugin
    # modules the session loads.
    hook_owners = {f.parsed.path: f for f in facts_by_path.values()}
    for plugin in global_plugins:
        hook_owners.setdefault(plugin.parsed.path, plugin)
    for _, facts in sorted(hook_owners.items()):
        for hook in facts.collect_hooks:
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "plugin_collects_files",
                    f"{facts.parsed.path}: binds {hook}, which makes tests out of files or "
                    "objects by its own rules; what it collects is not a target",
                    facts.parsed.path,
                )
            )

    def doctest_deps(pm: ParsedModule, conftests: list[ModuleFacts]) -> list[str]:
        """What a doctest item runs besides its examples: pytest picks up the
        module's and the conftests' autouse fixtures for it, provides
        ``doctest_namespace`` (which an autouse fixture usually fills, as
        pandas's root conftest does), applies the ini ``usefixtures``, and
        calls the session-wide hooks."""
        facts = facts_by_path.get(pm.path) or link(_collect_facts(pm))
        resolver = _Resolver(
            facts, conftests, global_plugins, options, unresolved, assumed, ini_usefixtures
        )
        return (
            plugin_hooks
            + outside_conftests_of(pm.path)
            + resolver.lifecycle([], ["doctest_namespace"])
        )

    _collect_doctests(
        result, snapshot, index, {**config, "testpaths": testpaths}, facts_by_path, doctest_deps
    )

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
    fixture_deps: Callable[[ParsedModule, list[ModuleFacts]], list[str]] | None = None,
) -> None:
    """Doctest targets (see the module docstring)."""
    testpaths = tuple(config["testpaths"])
    norecurse = tuple(config["norecursedirs"])
    parser = doctest.DocTestParser()
    globs = tuple(config["doctest_globs"])
    for path in sorted(snapshot.other_files):
        # A file a glob matches but the snapshot does not read (only .txt,
        # .rst and .md are read) holds doctests that are not targets.
        if (
            path not in snapshot.text_files
            and any(fnmatch(PurePosixPath(path).name, g) for g in globs)
            and _under_testpaths(path, testpaths)
            and _collected_dir(path, norecurse)
            and not _ignored(path, config)
        ):
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "unparsed_file",
                    f"{path}: a --doctest-glob matches it, but only .txt, .rst and .md files "
                    "are read for doctests, so its examples are not targets",
                    path,
                )
            )
    for path, content in sorted(snapshot.text_files.items()):
        name = PurePosixPath(path).name
        if not (
            _under_testpaths(path, testpaths)
            and _collected_dir(path, norecurse)
            and not _ignored(path, config)
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
        and not _ignored(p, config)
        and PurePosixPath(p).name not in ("setup.py", "__main__.py")
    ]
    for path in snapshot.python_paths:
        if (
            path not in snapshot.files
            and _under_testpaths(path, testpaths)
            and _collected_dir(path, norecurse)
            and not _ignored(path, config)
            and PurePosixPath(path).name not in ("setup.py", "__main__.py", "conftest.py")
        ):
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "test_file_outside_roots",
                    f"{path}: --doctest-modules collects its docstrings, but it is outside the "
                    "source roots, so they are not targets; add a source root that contains it",
                    path,
                )
            )
    parsed, failed = parse_modules(snapshot, sorted(paths))
    for path in failed:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unparsed_file",
                f"{path}: --doctest-modules collects its docstrings, but it did not parse or "
                "cannot be named from any source root, so they are not targets",
                path,
            )
        )
    every_path = {*snapshot.python_paths, *snapshot.files}
    for pm in parsed:
        if _assigns_name(pm.tree.body, "__test__"):
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "unmodelled_test_binding",
                    f"{pm.path}: defines __test__, whose doctests pytest collects; they are "
                    "not targets",
                    pm.path,
                )
            )
        # The name pytest gives the module (doctest items are named after it):
        # by its packages in the default import modes, from the rootdir with
        # importlib.
        module_name = (
            pm.module
            if config["import_mode"] == "importlib"
            else _package_module_name(pm.path, every_path)
        )
        conftests = _conftest_chain(pm.path, facts_by_path)
        base_deps = [c.parsed.module for c in conftests] + [h for c in conftests for h in c.hooks]
        fixtures_added = False
        for qualname, symbol, docstring in _docstrings(pm):
            try:
                examples = parser.get_examples(docstring)
            except ValueError:
                examples = None  # pytest reports it as a failing item
            if examples == []:
                continue
            if not fixtures_added:
                # Once per module, and only for one with examples.
                fixtures_added = True
                if fixture_deps is not None:
                    base_deps += fixture_deps(pm, conftests)
            deps = [*base_deps, f"dynamic:{pm.module}"]
            if examples is None:
                deps.append("doctest:unparsed")
            else:
                for module in _example_imports(examples):
                    if module is None:
                        deps.append("doctest:unparsed")
                    elif module in index.modules:
                        deps.append(f"dynamic:{module}")
            name = module_name if not qualname else f"{module_name}.{qualname}"
            result.targets.append(
                Target(RUNNER, f"{pm.path}::{name}", symbol, tuple(sorted(set(deps))))
            )


def _assigns_name(body: list[ast.stmt], name: str) -> bool:
    return any(
        isinstance(stmt, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(t, ast.Name) and t.id == name
            for t in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
        )
        for stmt in iter_scope_statements(body)
    )


def _package_module_name(path: str, paths: set[str]) -> str:
    """The name pytest's ``prepend``/``append`` import modes give a module:
    its directories count while each holds an ``__init__.py``."""
    pure = PurePosixPath(path)
    parts = [] if pure.name == "__init__.py" else [pure.stem]
    directory = pure.parent
    while str(directory) not in (".", "") and str(directory / "__init__.py") in paths:
        parts.insert(0, directory.name)
        directory = directory.parent
    return ".".join(parts) or pure.parent.name


def _docstrings(pm: ParsedModule) -> list[tuple[str, str, str]]:
    """(qualified name, symbol id, docstring) of every object doctest's finder
    visits: the module, its functions and classes, and recursively their
    methods and nested classes."""
    found: list[tuple[str, str, str]] = []
    doc = ast.get_docstring(pm.tree, clean=False)
    if doc:
        found.append(("", pm.module, doc))

    def walk(body: list[ast.stmt], prefix: str, container: str | None) -> None:
        # Definitions under ``if``/``try`` are attributes like any other.
        for node in iter_scope_statements(body):
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
    module_classes = {c.name: c for c in scope_classes(parsed.tree.body)}
    # Names used as a base anywhere in the module: such a class contributes
    # its methods through its subclasses, so it is not an uncollected class.
    base_names = {
        (decorator_chain(base)[0] or [""])[-1]
        for node in ast.walk(parsed.tree)
        if isinstance(node, ast.ClassDef)
        for base in node.bases
    }

    def add(
        nodeid: str,
        entry: str,
        class_levels: list[dict[str, Fixture]],
        requests: list[str],
        extra: list[str],
        marks: Marks,
    ):
        if entry not in index.symbols:
            result.notes.append(
                DiscoveryNote(RUNNER, "missing_symbol", f"{nodeid}: {entry} is not in the index")
            )
        deps = module_deps + extra + resolver.lifecycle(class_levels, requests, marks.supplied)
        result.targets.append(Target(RUNNER, nodeid, entry, tuple(sorted(set(deps)))))

    # Module scopes reached through base classes: name -> (parsed, classes,
    # imported class names). None for a module outside the source roots.
    scopes: dict[str, _Scope | None] = {}
    # Per module: alias -> module it names, for a dotted base ``alias.Class``.
    module_prefixes: dict[str, dict[str, str]] = {}

    def scope_for(module: str) -> _Scope | None:
        """None for a module outside the source roots, or one whose scope is
        being built (an import cycle)."""
        if module in scopes:
            return scopes[module]
        scopes[module] = None  # guards import cycles while this one is built
        facts = module_facts(module) if module_facts is not None else None
        if facts is None:
            return None
        modules: dict[str, str] = {}
        classes = {c.name: c for c in scope_classes(facts.parsed.tree.body)}
        imported: dict[str, tuple[str, str]] = {}
        for stmt in iter_scope_statements(facts.parsed.tree.body):
            if isinstance(stmt, ast.Import):
                # ``import tests.queues as t``: ``t.LifoDiskQueueTest`` is that
                # module's class, recorded under the alias as a module prefix.
                for alias in stmt.names:
                    modules[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
                continue
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
                    modules.setdefault(alias.asname or alias.name, f"{src}.{alias.name}")
        module_prefixes[module] = modules
        scope = (facts.parsed, classes, imported)
        scopes[module] = scope
        return scope

    own_scope = (parsed, module_classes, (scope_for(parsed.module) or (None, {}, {}))[2])
    # Base class id -> the module defining it, whose names its decorators use.
    defined_in: dict[str, str] = {}
    stored: dict[tuple[str, str], list[ast.expr]] = {}

    def stored_mark(module: str, name: str) -> list[ast.expr]:
        """The mark expressions ``module`` binds to ``name`` at module level
        (``skip_pyarrow = pytest.mark.usefixtures("pyarrow_skip")``, or a
        list of marks), following aliases and ``from`` imports; empty when
        it binds none that can be seen. Only names used as marks are
        followed, so no imported library is parsed for this."""
        key = (module, name)
        if key in stored:
            return stored[key]
        stored[key] = []  # guards cycles
        scope = scope_for(module)
        if scope is None:
            return []
        found: list[ast.expr] = []
        values = [v for n, v in scope_assignments(scope[0].tree.body) if n == name]
        if values:
            # Every binding counts: which one is live is not tracked.
            expand = expander(module)
            for value in values:
                elts = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
                for expr in elts:
                    found += expand(expr) or [expr]
        elif name in scope[2]:
            found = stored_mark(*scope[2][name])
        stored[key] = found
        return found

    def expander(module: str) -> MarkExpander:
        """Resolves ``name`` and ``alias.name`` mark entries in ``module``."""

        def expand(expr: ast.expr) -> list[ast.expr] | None:
            if isinstance(expr, ast.Name):
                return stored_mark(module, expr.id)
            parts, call = decorator_chain(expr)
            if call is None and len(parts) == 2:
                target = module_prefixes.get(module, {}).get(parts[0])
                if target is not None:
                    return stored_mark(target, parts[1])
            return None

        return expand

    own_marks = expander(parsed.module)
    module_marks = _marks_from_pytestmark(parsed.tree.body, own_marks)

    def class_in(module: str, name: str) -> tuple[ast.ClassDef, str, Any] | None:
        """The class ``name`` names in ``module``: defined there, or imported
        there from a module in the source roots (pandas's
        ``tests/extension/base/__init__.py`` re-exports the classes of its
        submodules), followed to the module that defines it."""
        visited: set[tuple[str, str]] = set()
        scope = scope_for(module)
        while scope is not None and (module, name) not in visited:
            visited.add((module, name))
            if name in scope[1]:
                return scope[1][name], scope[0].member_id(name), scope
            if name not in scope[2]:
                return None
            module, name = scope[2][name]
            scope = scope_for(module)
        return None

    def mro(
        cls: ast.ClassDef,
        nodeid: str,
        quiet: bool = False,
        scope: tuple[Any, dict[str, ast.ClassDef], dict[str, tuple[str, str]]] | None = None,
    ) -> list[tuple[ast.ClassDef, str]]:
        """Base classes, nearest first, with their symbol ids. A base defined
        in another module in the source roots is followed too (networkx's
        ``TestDiGraph(BaseGraphTester)``), and its own bases resolve in the
        module that defines it, not in this one."""
        chain: list[tuple[ast.ClassDef, str]] = []
        seen: set[tuple[str, str]] = set()
        start = scope or own_scope
        queue = [(base, start) for base in cls.bases]
        while queue:
            base, (owner, classes_here, imports_here) = queue.pop(0)
            parts, _ = decorator_chain(base)
            name = parts[-1] if parts else ""
            if name in ("object", "") or (owner.module, name) in seen:
                continue
            seen.add((owner.module, name))
            found = None
            if name in classes_here and not (owner.module == start[0].module and name == cls.name):
                found = (
                    classes_here[name],
                    owner.member_id(name),
                    (owner, classes_here, imports_here),
                )
            elif name in imports_here:
                found = class_in(*imports_here[name])
            elif len(parts) > 1:
                # ``t.LifoDiskQueueTest``: the prefix names a module.
                prefixes = module_prefixes.get(owner.module, {})
                head = ".".join(parts[:-1])
                source = prefixes.get(parts[0], parts[0])
                if len(parts) > 2:
                    source = f"{source}.{'.'.join(parts[1:-1])}" if parts[0] in prefixes else head
                found = class_in(source, name)
            if found is not None:
                base_cls, base_id, base_scope = found
                if used_as_base is not None:
                    used_as_base.add(base_id)
                defined_in[base_id] = base_scope[0].module
                chain.append((base_cls, base_id))
                queue.extend((b, base_scope) for b in base_cls.bases)
            elif not (
                quiet
                or name in NO_TEST_BASES
                or (
                    name.endswith("TestCase")
                    and _framework_base(name, parts, imports_here, module_prefixes, owner)
                )
            ):
                result.notes.append(
                    DiscoveryNote(
                        RUNNER,
                        "unknown_base_class",
                        f"{nodeid}: base class {name!r} is not defined in this module or "
                        "imported from one in the source roots; test methods it may "
                        "contribute are not discovered",
                        parsed.path,
                    )
                )
        return chain

    module_funcs = {f.name: f for f in scope_functions(parsed.tree.body)}

    def unmodelled(nodeid: str, value: str) -> None:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unmodelled_test_binding",
                f"{nodeid} is bound to {value}, which discovery cannot follow; pytest "
                "collects it if it is a test function or class, and it is not a target",
                parsed.path,
            )
        )

    def walk_class(
        cls: ast.ClassDef,
        prefix_ids: list[str],
        nodeid_prefix: str,
        inherited: Marks,
        outer: list[dict[str, Fixture]] | None = None,
        name: str | None = None,
        class_id: str | None = None,
        scope: _Scope | None = None,
        imported: bool = False,
    ) -> None:
        """Targets of a class collected here as ``name`` (its own name, or the
        one a test module imports it under, with ``class_id`` and ``scope``
        where it is defined); ``outer`` are the enclosing classes' fixture
        levels, innermost first. An ``imported`` class pytest's rules skip
        is not reported: it is collected where it is defined, if anywhere."""
        name = name or cls.name
        # pytest's unittest plugin collects a TestCase subclass whatever it is
        # called, and the base that brings TestCase in may be several classes
        # and modules away (DRF's ``XffSpoofingTests(XffTestingBase)``).
        unittest_style = _is_unittest_class(cls) or any(
            _is_unittest_class(base) for base, _ in mro(cls, nodeid_prefix, quiet=True, scope=scope)
        )
        # A class with ``__init__`` is skipped, except a TestCase: the
        # unittest plugin collects it all the same.
        if not (unittest_style or _matches(classes, name)) or (
            _has_init(cls) and not unittest_style
        ):
            # A class pytest's own rules skip, but that defines test methods,
            # is a class some plugin collects (SQLAlchemy's testing plugin
            # collects ``<Name>Test``): report it rather than guess either way.
            if (
                not imported
                and cls.name not in base_names
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
        if class_id is None:
            class_id = f"{prefix_ids[-1]}.{cls.name}" if prefix_ids else parsed.member_id(cls.name)
        nodeid = f"{nodeid_prefix}::{name}"
        bases = mro(cls, nodeid, scope=scope)
        # Fixture lookup: this class with what it inherits, then outer classes.
        class_levels = [_class_level([(cls, class_id), *bases]), *(outer or [])]
        # Marks: the enclosing scopes', the bases' (pytest reads marks along
        # the MRO), then the class's own.
        home = (scope or own_scope)[0].module
        class_marks = inherited
        for owner, owner_id in [*reversed(bases), (cls, class_id)]:
            expand = expander(defined_in.get(owner_id, home))
            class_marks = class_marks + _marks_from_expressions(owner.decorator_list, expand)
            class_marks = class_marks + _marks_from_pytestmark(owner.body, expand)
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
        # Test methods bound by assignment in a class body (``test_b =
        # test_a``, ``test_x = _check``): pytest collects any attribute whose
        # name matches and whose value is a function. Entry: the function.
        entries: dict[str, str] = {}
        for owner, owner_id in [*reversed(bases), (cls, class_id)]:
            owner_defs = {f.name: f for f in scope_functions(owner.body)}
            same_module = defined_in.get(owner_id, home) == parsed.module
            for stmt in owner.body:
                assigned = _assigned(stmt)
                if assigned is None:
                    continue
                bound, value = assigned
                if not (
                    _matches(functions, bound) or (unittest_style and bound.startswith("test"))
                ):
                    continue
                if isinstance(value, ast.Name) and value.id in owner_defs:
                    methods[bound] = (owner_defs[value.id], owner_id)
                    entries[bound] = f"{owner_id}.{value.id}"
                elif isinstance(value, ast.Name) and same_module and value.id in module_funcs:
                    methods[bound] = (module_funcs[value.id], parsed.module)
                    entries[bound] = parsed.member_id(value.id)
                elif isinstance(value, (ast.Name, ast.Attribute)):
                    unmodelled(f"{nodeid}::{bound}", ast.unparse(value))
        setup = [
            f"{owner_id}.{method}"
            for method, (_, owner_id) in methods.items()
            if method in CLASS_SETUP_METHODS
        ]
        extra = setup + [class_id] if bases else setup
        collected = [
            method
            for method, (func, _) in methods.items()
            if not _is_fixture(func)[0]
            and (_matches(functions, method) or (unittest_style and method.startswith("test")))
        ]
        if unittest_style and not any(m.startswith("test") for m in collected):
            # unittest: a TestCase with no ``test*`` method runs ``runTest``.
            if "runTest" in methods:
                collected.append("runTest")
        for method in sorted(collected):
            func, owner_id = methods[method]
            expand = expander(defined_in.get(owner_id, home))
            marks = class_marks + _marks_from_expressions(func.decorator_list, expand)
            requests = list(_fixture_requests(func, True, marks, class_injected))
            requests += list(marks.usefixtures)
            entry = entries.get(method, f"{owner_id}.{method}")
            add(f"{nodeid}::{method}", entry, class_levels, requests, extra, marks)
        # Nested classes, inherited ones too (pytest collects a class's
        # attributes along its MRO), the nearest definition of a name winning.
        inners: dict[str, tuple[ast.ClassDef, str, _Scope | None]] = {}
        for owner, owner_id in reversed(bases):
            owner_scope = scope_for(defined_in.get(owner_id, home))
            for inner in scope_classes(owner.body):
                inners[inner.name] = (inner, owner_id, owner_scope)
        for inner in scope_classes(cls.body):
            inners[inner.name] = (inner, class_id, scope)
        for inner, owner_id, inner_scope in inners.values():
            walk_class(
                inner,
                prefix_ids + [owner_id],
                nodeid,
                class_marks,
                class_levels,
                scope=inner_scope,
                imported=imported or owner_id != class_id,
            )

    def module_test(func: ast.FunctionDef | ast.AsyncFunctionDef, bound: str) -> None:
        marks = module_marks + _marks_from_expressions(func.decorator_list, own_marks)
        requests = list(_fixture_requests(func, False, marks)) + list(marks.usefixtures)
        add(f"{parsed.path}::{bound}", parsed.member_id(func.name), [], requests, [], marks)

    # ``f.__test__ = True``: pytest collects ``f`` whatever it is called.
    marked_tests: set[str] = set()
    for stmt in parsed.tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "__test__"
                and isinstance(target.value, ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is True
            ):
                marked_tests.add(target.value.id)
    for func in scope_functions(parsed.tree.body):
        if not (_matches(functions, func.name) or func.name in marked_tests):
            continue
        if _is_fixture(func)[0]:
            continue
        module_test(func, func.name)

    # Tests bound by assignment (``test_alias = test_orig``): pytest collects
    # any module attribute whose name matches and whose value is a function
    # or class. A value bound to something not visible here (an attribute,
    # an imported name: Hypothesis's ``TestMachine = Machine.TestCase``) is
    # reported; data (``test_data = [...]``, a call's result) is not a test.
    module_classes_here = {c.name: c for c in scope_classes(parsed.tree.body)}
    module_defined = {n.name for n in parsed.tree.body if isinstance(n, DEF_NODES)}
    for stmt in parsed.tree.body:
        assigned = _assigned(stmt)
        if assigned is None or assigned[0] in module_defined:
            continue
        bound, value = assigned
        is_function = _matches(functions, bound)
        is_class = _matches(classes, bound)
        if not (is_function or is_class):
            continue
        if isinstance(value, ast.Name) and value.id in module_funcs and is_function:
            if not _is_fixture(module_funcs[value.id])[0]:
                module_test(module_funcs[value.id], bound)
        elif isinstance(value, ast.Name) and value.id in module_classes_here:
            target = module_classes_here[value.id]
            walk_class(
                target,
                [],
                parsed.path,
                module_marks,
                name=bound,
                class_id=parsed.member_id(target.name),
            )
        elif isinstance(value, (ast.Name, ast.Attribute)):
            unmodelled(f"{parsed.path}::{bound}", ast.unparse(value))

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
                        parsed.path,
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
            # Followed through re-exports (``tests/base.py`` importing the
            # class from ``tests/impl.py``) to the module defining it.
            found = (
                _definition_bound_to(source, alias.name, module_facts, set())
                if module_facts is not None
                else source
            )
            origin, node = found if isinstance(found, tuple) else (None, None)
            if not (is_function or is_class):
                # Still a test when it is a unittest TestCase (collected
                # whatever its name): walk any in-scope class it names.
                if not isinstance(node, ast.ClassDef):
                    continue
            nodeid = f"{parsed.path}::{bound}"
            if node is None or origin is None:
                # Data, or outside the source roots: there, whether pytest
                # collects anything from it is unknown, so it is reported,
                # not guessed; except
                # unittest's names and a test framework's ``*TestCase``
                # (``from django.test import TestCase``), which yield none.
                outside = found if isinstance(found, str) else None
                if outside is not None and not (
                    outside.split(".")[0] == "unittest"
                    or (
                        outside.split(".")[0] in TESTCASE_FRAMEWORKS
                        and alias.name.endswith("TestCase")
                    )
                    or alias.name in LIBRARY_NON_TESTS.get(outside, ())
                ):
                    result.notes.append(
                        DiscoveryNote(
                            RUNNER,
                            "imported_test_out_of_scope",
                            f"{nodeid}: imported from {outside}, outside the source roots; "
                            "any tests pytest collects from it are not targets",
                            parsed.path,
                        )
                    )
                continue
            assert isinstance(node, DEF_NODES)
            entry = origin.parsed.member_id(node.name)
            if isinstance(node, ast.ClassDef):
                # The class is collected here as if defined here, with
                # everything it inherits (urllib3's test_pyopenssl.py imports
                # TestHTTPS_TLSv1, whose tests are almost all defined on its
                # bases), its bases resolved where it is defined.
                origin_scope = scope_for(origin.parsed.module) or (
                    origin.parsed,
                    {c.name: c for c in scope_classes(origin.parsed.tree.body)},
                    {},
                )
                walk_class(
                    node,
                    [],
                    parsed.path,
                    module_marks,
                    name=bound,
                    class_id=entry,
                    scope=origin_scope,
                    imported=True,
                )
            elif is_function and not _is_fixture(node)[0]:
                marks = module_marks + _marks_from_expressions(
                    node.decorator_list, expander(origin.parsed.module)
                )
                requests = list(_fixture_requests(node, False, marks)) + list(marks.usefixtures)
                add(nodeid, entry, [], requests, [origin.parsed.module], marks)

    for cls in scope_classes(parsed.tree.body):
        walk_class(cls, [], parsed.path, module_marks)

"""Static pytest discovery.

Reproduces the collection rules below without importing test code. Anything
outside them is reported, never guessed.

Collected:

* files matching ``python_files`` (default ``test_*.py``, ``*_test.py``) under
  the source roots that pytest's walk from an initial path reaches (_reaches);
* module-level functions matching ``python_functions`` (default prefix
  ``test``), methods of classes matching ``python_classes`` (default prefix
  ``Test``) that have no constructor, as pytest's ``hasinit``/``hasnew``
  decide it (an ``__init__`` or ``__new__`` the class defines or inherits
  from a base discovery resolves, or one ``@dataclass`` or attrs' ``@attr.s``
  and ``@define`` generate unless ``init=False``; a base it cannot resolve is
  taken to have none; a TestCase is collected with one),
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
  it, followed through re-exports to the module defining it (a
  definition under ``if``/``try`` there counts); one the defining module
  binds to what discovery cannot follow (an attribute, a call's result,
  nothing it can find) is reported (``unmodelled_test_binding``), while
  data (a literal, a container, a module) is no test; ``from
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
  ``test_x = _check``, under ``if``/``try`` too) to a function or class
  defined there; one bound to something not visible there (``TestMachine =
  Machine.TestCase``) or to a value that may hold code (a call's result:
  ``test_one = make(1)``, ``functools.partial(_check, 1)``, ``TestGen =
  _make()``; anything but evident data, _may_bind_test: not a literal,
  arithmetic, or what a builtin or a standard-library module such as
  ``os``, ``pathlib`` or ``json`` returns, an instance of a plain class
  the module defines, an item or method result of such data) is reported
  (``unmodelled_test_binding``), and so is a test bound dynamically
  (_dynamic_bindings: ``globals()[f"test_{n}"] = ...``, ``locals()[...]`` in
  a class body, ``setattr(TestA, "test_x", f)``, ``TestA.test_x = f``,
  unpacking, pytest-bdd's ``scenarios(...)``); a function
  marked ``f.__test__ = True``; a unittest ``runTest`` when a TestCase has no
  ``test*`` method; an imported TestCase whatever its bound name;
* initial paths as pytest decides them: the paths among its arguments
  (``.`` is the rootdir), else ``testpaths`` expanded as ``glob`` expands
  it (entries that exist; none falls back to the rootdir), made prefix-free
  as pytest makes them unless ``--keep-duplicates``; a file named there is
  collected whatever ``python_files`` says; a test file pytest would collect
  outside the source roots, or kept out of the analysis by ``diffcone.toml``'s
  ``[index] exclude``, is reported (``test_file_outside_roots``);
* below an initial path, never at or above one, what pytest's own
  ``pytest_ignore_collect`` skips: ``--ignore``, ``--ignore-glob``,
  ``norecursedirs`` (each directory below the initial path) and
  ``__pycache__``; a conftest's ``pytest_ignore_collect`` that may return
  False (any ``return`` other than True or None, or one bound by import)
  overrides them below its directory, so they are not applied there, and
  one in a plugin module is reported (``plugin_collects_files``);
  ``collect_ignore`` and ``collect_ignore_glob`` are not applied, so what
  they name stays a target (over-collected, never missed);
* pytest's arguments, in its order: configuration from ``pytest.toml``,
  ``.pytest.toml``, ``pytest.ini``, ``.pytest.ini``, ``pyproject.toml``
  (``[tool.pytest.ini_options]`` or ``[tool.pytest]``), ``tox.ini`` or
  ``setup.cfg`` at the repository root, in pytest 9's order (INI values
  split as a shell would; the first of them that does not parse, which
  pytest stops on, is an ``unparsable_config`` note, and the defaults
  stand in for it), its ``addopts``, then ``PYTEST_ADDOPTS``, the
  arguments written into the run's command and those after ``--``
  (DiscoveryOptions; the plugins ``PYTEST_PLUGINS`` names load like ``-p``
  ones); ``-o key=value`` overrides the configuration as pytest applies it
  (an ``addopts`` override on the command line replaces the configured
  one); an option's value written apart (``--ignore tests/slow``) is no
  path; ``-c``, ``--rootdir`` and ``--pyargs``, a node id, a path after an
  option that may take it as its value, a path that names nothing in the
  tree and a command whose pytest arguments cannot be found are reported
  (``unmodelled_runner_option``); pytest's rootdir and configuration file
  are determined as pytest does (pytest_rootdir: ``--rootdir``, ``-c``,
  else the configuration file nearest above the arguments' common
  ancestor, then ``setup.py``, then each argument's own ancestors, then a
  bare ``pyproject.toml``, else the repository) and recorded with the
  ``--junit-prefix`` in the config the plan carries (``rootdir``,
  ``configfile``, ``junit_prefix``: ``check`` maps JUnit names through
  them); a configuration file below the repository root is one discovery
  did not read, so it is reported (``unmodelled_runner_option``);
* only the conftests pytest loads: at startup, those of the initial paths,
  of every directory above one and of the ``test*`` directories directly in
  one, whatever ignores say; while collecting, those of the directories its
  walk reaches;
* a ``conftest.py`` outside the source roots is reported
  (``conftest_outside_roots``; an excluded one too) and is an unknown dependency
  (``conftest:<path>``) of every test under it, so those tests are always
  selected; a plugin module in the repository the source roots do not name
  is reported (``plugin_outside_roots``; an excluded one too) and is an
  unknown dependency (``plugin:<module>``) of every test. A note on an
  excluded file (``[index] exclude``) names the exclusion, a base class
  imported from one included (``unknown_base_class``).

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
  module that defines the decorated function or class; ``pytestmark``
  however the list is built: ``+``, ``+=``, ``.append``, ``.extend``,
  or imported by name; a call of a stored mark, ``use =
  pytest.mark.usefixtures`` then ``@use("db")``, is that mark's call); a
  ``usefixtures`` argument that is not a literal (``usefixtures(NAME)``,
  ``usefixtures(*NAMES)``), and a decorator or ``pytestmark`` entry naming
  or calling a function or class in the source roots whose code mentions
  ``usefixtures`` (``@needs_db``, ``@uses("db")``, ``pytestmark = marks()``:
  what it calls in turn is not followed), may name any fixture visible from
  the test, so every one of them is a dependency;
  and transitively by other fixtures, resolved in pytest's order: class
  (its own fixtures and those it inherits from bases in any module in the
  source roots, the nearest definition of an attribute hiding the rest, as
  ``dir(cls)`` sees them), enclosing classes, module, nearest
  ``conftest.py`` outward, then
  ``pytest_plugins`` modules within the source roots (and the plugins those
  declare, as pytest registers them; when several plugins define a name,
  each definition counts, since which registered last is not tracked;
  ``pytest_plugins`` is read however it is built: a string, which pytest
  splits at commas, lists and tuples, ``+``, ``+=``, ``.append``,
  ``.extend``, ``list(...)``, a module-level name bound to those, or
  imported from another module; one that cannot be read, such as a call,
  a comprehension or a name from outside the roots, is reported
  (``unreadable_plugins``) and is an unknown dependency
  (``plugin:<path>:pytest_plugins``) of every test); a
  fixture requesting
  its own name resolves to the next definition outward; a module offers a
  fixture under every name it binds the fixture to (``box2 = box``, ``from
  pkg.conftest import engine as motor``, ``engine = pkg.conftest.engine``,
  a star import at any depth), as pytest registers
  it, except that a fixture with an explicit ``name=`` is offered under that
  name only; a name the test's marks parametrize directly is a parameter at
  every depth of the closure, not only among the test's own arguments
  (``set_engine(engine, ext)`` under ``parametrize("engine, ext")``), as
  pytest replaces any fixture of that name and prunes what it requests;
* fixtures requested by literal name through ``request.getfixturevalue``,
  and every fixture visible from the test when the name is not a literal;
  what a test or fixture requests through ``request`` handed to a helper,
  read in that helper with the parameter it lands in (a function defined
  in the caller, one its module binds or imports from the source roots, a
  method ``self.name`` or ``super().name`` finds on the class; four calls
  deep), and every fixture visible from the test when ``request`` reaches
  what discovery cannot read (a callee outside the source roots or one it
  cannot find; an alias of ``getfixturevalue``, ``getattr(request,
  "getfixturevalue")`` without a literal name; ``request`` stored, returned
  or put in a container: _RequestFlow); attribute uses (``request.param``,
  ``request.node``, ``request.applymarker(...)``) request nothing;
* fixtures named by pytest-lazy-fixtures' ``lf("name")`` or
  pytest-lazy-fixture's ``lazy_fixture("name")``: anywhere in a test module,
  requested by each of its tests; in a fixture's decorator
  (``params=[lf("db")]``), by the fixture (a name that is not a literal
  stands for every fixture visible from the test);
* every fixture of the session's plugin modules (``pytest11`` entry points,
  ``-p`` in addopts, ``PYTEST_PLUGINS``, ``pytest_plugins``) for a test
  requesting ``pytester`` or ``testdir``: the inline session it runs loads
  the project's registered plugins, and its test code requests their
  fixtures by names only the inline text holds (what that text imports is
  the indexer's: it reads ``makepyfile``, ``makeconftest``, ``runpytest
  -p`` and the rest, indexer/references.py);
* the ini option ``usefixtures``, requested by every test, and what the
  session's conftests and plugin modules add to it or to the tests' own
  requests in code (``config.addinivalue_line("usefixtures", "db")``,
  ``item.fixturenames.append("db")``, ``.extend``, ``.insert``, ``+=``;
  which tests a hook adds a name to is not read, so every test requests
  it; a name that is not a literal stands for every fixture visible from
  the test: _session_requests);
* ``autouse`` fixtures visible from the test (``autouse=`` anything but a
  literal false value, since ``autouse=FLAG`` applies whenever the flag is
  set), those a conftest, test module or plugin binds by import included
  (a binding is followed when its module, or one it imports, may define
  one: _fixture_reach);
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
  loads (``pytest_plugins`` in a conftest or a test module, a literal
  ``pluginmanager.import_plugin("pkg.plug")`` anywhere in one, ``-p`` in
  addopts, ``PYTEST_PLUGINS``, ``pytest11`` entry points of the project or
  of a sibling package in the repository), and those plugin modules
  themselves, whose import-time code runs for every test; a hook or setup
  function bound by import (a star import included) or assignment counts
  as one defined there (_add_bound), and the module-level
  ``pytestmark``, ``pytest_plugins``, ``collect_ignore`` and
  ``collect_ignore_glob`` variables of the test module and its conftests;
  a collection hook (``COLLECT_HOOKS``) is reported
  (``plugin_collects_files``) whatever it is bound to, one imported from
  outside the roots included, and so is a star import from outside the
  roots in a conftest or a plugin module, which may bind one;
  every ``pytest_*`` method of an in-scope class (a plugin object pytest
  may register: ``config.pluginmanager.register(Obj())``); and the code
  pytest runs while collecting (import-time code of conftests, test
  modules and what they import; collection hooks such as a sub-directory's
  ``pytest_generate_tests``) that writes process-global state such as
  ``os.environ`` (discovery/session_state.py);
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
``doctest_namespace`` (which an autouse fixture usually fills; each module in
the source roots a fixture of that closure stores into it,
``doctest_namespace["pkg"] = pkg``, ``.update(pd=pd)``, ``.setdefault``, is a
``dynamic:<module>`` dependency, as a module an example imports is:
_namespace_modules); a module outside the source roots is reported
(``test_file_outside_roots``). A docstring the module sets when it is
imported is read when discovery can work out its text (``f.__doc__ =
"..."``, ``+=``, a class or module body's ``__doc__ = "..."``, another
object's docstring of the same scope, ``out_edges.__doc__ =
edges.__doc__``, and at module level a string expression of literals and
module-level strings: ``+``, ``%``, f-strings and string methods such as
``format`` and ``replace``, _static_text); any other is computed (a name
bound elsewhere, ``DOC``, a call, a decorator in the source roots that
assigns ``__doc__`` anything but a literal without examples or another
docstring: pandas' ``@doc``, ``@Appender``; what a decorator calls in turn,
and decorators from outside the source roots, are not read): an object
whose written docstring has examples is a target with the unknown
dependency ``doctest:computed``, so it is always selected; one without, and
a docstring set on an object discovery cannot name (a loop's variable), is
reported (``unmodelled_test_binding``), since whether pytest collects a
doctest from it is unknown; text files
matching ``--doctest-glob`` (default ``test*.txt``; a glob with a ``/``
matches the path, as ``fnmatch_ex`` does), and a ``.txt``/``.rst`` file
named as an initial path whatever the globs say, become targets whose entry
is not a symbol, so they are always selected.

Fixtures are recognised by a decorator whose dotted name's last part ends
in ``fixture`` (``@pytest.fixture``, ``@pytest.fixture(name=...)``,
``@fixture``, ``@yield_fixture``, ``@pytest_asyncio.fixture``,
pytest-twisted's ``@async_fixture`` and ``@async_yield_fixture``,
``@pytest_trio.trio_fixture``; ``name=`` and ``autouse=`` read as
pytest's), also under a name the module binds it to (``from pytest import
fixture as fx``, ``fx = pytest.fixture``: common.fixture_aliases; an alias
imported from another module is not followed), or a call of one binding a module function (``x =
pytest.fixture(scope=...)(f)``, ``x = pytest.fixture(f)``). Overriding
follows nearest-scope-wins. Fixture parametrisation (``params=``) is not
modelled: a fixture is one dependency whatever its parameters.
``indirect`` parametrisation is: an argname handed to a fixture stays a
request, and ``indirect=`` (by keyword, or as the third positional
argument) that is anything but a literal false value or a literal list of
names hands every argname over; unless it is ``True``, only maybe: such an
argname that names no fixture anywhere is a parameter, since pytest fails
a test whose indirect argname names none. Fixtures from installed plugins
cannot be seen: a name found at no in-scope level that was declared
external, or that a well-known plugin provides
(``WELL_KNOWN_PLUGIN_FIXTURES``: ``mocker`` from pytest-mock,
``httpx_mock`` from pytest-httpx, ...), is assumed to come from that plugin
and reported in an ``external_fixture`` note with its request count. The
table never shadows a definition discovery sees but cannot read: when a
level the lookup searches binds the name to what may be a fixture (a
function under a decorator it does not recognise, a call's result, an
attribute of a value; _unread_fixture, _ClassFixtures.unread), the name is
an unknown fixture, and so is a maybe-indirect argname bound that way. An
unknown fixture becomes the lifecycle dependency ``fixture:<name>``
(an ``unresolved_fixture`` note), so the planner selects its users.
"""

from __future__ import annotations

import ast
import configparser
import doctest
import re
import shlex
import tomllib
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch, fnmatchcase
from pathlib import PurePosixPath
from typing import Any, NamedTuple, TypeVar

from diffcone.discovery import DiscoveryNote, DiscoveryOptions, DiscoveryResult
from diffcone.discovery.common import (
    NO_GETFIXTUREVALUE,
    ParsedModule,
    decorator_chain,
    keyword_value,
    names_fixture_alias,
    parse_modules,
    scope_assignments,
    scope_classes,
    scope_functions,
    string_literals,
)
from diffcone.discovery.session_state import (
    COLLECTION_HOOKS,
    plugin_object_hooks,
    state_writing_roots,
)
from diffcone.indexer import DEF_NODES, FUNC_NODES, iter_scope_statements
from diffcone.manifest import Target
from diffcone.model import (
    DYNAMIC_DEPENDENCY,
    EdgeKind,
    SourceIndex,
    SymbolKind,
)
from diffcone.snapshot import PYTEST_CONFIGS, Snapshot, excluded_files, excluded_modules

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
    return _parse_toml(raw)[0]


def _parse_toml(raw: bytes) -> tuple[dict[str, Any], str | None]:
    """The TOML document, or an empty one and why it does not parse."""
    try:
        return tomllib.loads(raw.decode("utf-8")), None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return {}, str(exc)


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
# model: given anywhere pytest reads options, they make the target list
# possibly short. (``-o``/``--override-ini`` is modelled: _ini_overrides.)
UNMODELLED_COLLECTION_OPTIONS = (
    "-c",
    "--config-file",
    "--rootdir",
    "--pyargs",
)


def _ini_overrides(tokens: tuple[str, ...]) -> dict[str, str]:
    """``-o key=value`` / ``-okey=value`` / ``--override-ini[=]key=value``
    entries, in order, the last of a key winning (pytest's
    ``parse_override_ini``)."""
    found: dict[str, str] = {}
    for i, token in enumerate(tokens):
        value = None
        if token in ("-o", "--override-ini") and i + 1 < len(tokens):
            value = tokens[i + 1]
        elif token.startswith("--override-ini="):
            value = token.split("=", 1)[1]
        elif token.startswith("-o") and not token.startswith("--") and len(token) > 2:
            value = token[2:]
        if value is not None and "=" in value:
            key, setting = value.split("=", 1)
            found[key.strip()] = setting
    return found


def read_pytest_config(snapshot: Snapshot, runner_args: tuple[str, ...] = ()) -> dict[str, Any]:
    """Return python_files/classes/functions/testpaths and where they came
    from. ``runner_args`` are the pytest arguments besides the configured
    ``addopts`` (``PYTEST_ADDOPTS``, the command's, those after ``--``):
    pytest reads ``addopts`` first, then those. ``-o key=value`` among them
    overrides the configuration as pytest applies it: an override of
    ``addopts`` on the command line replaces the configured value, and the
    overrides in the resulting ``addopts`` and on the command line then
    apply to every other key (pytest goes one level deep)."""
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

    def unparsable(name: str, error: str) -> dict[str, Any]:
        # pytest stops on a configuration file it cannot parse, wherever it
        # is in the order; one that pytest's own parser reads and this one
        # does not may set anything (testpaths, python_files, ...).
        config["source"] = f"{name} (unparsable, defaults used)"
        config["error"] = f"{name}: {error}"
        return {}

    # pytest's precedence (pytest 9): pytest.toml, .pytest.toml, pytest.ini,
    # .pytest.ini, pyproject.toml, tox.ini, setup.cfg. The first four are the
    # config file whenever present, even empty.
    for name in ("pytest.toml", ".pytest.toml"):
        if section is None and name in files:
            data, error = _parse_toml(files[name])
            section = _toml_table(data, "pytest") or {}
            config["source"] = name
            if error is not None:
                section = unparsable(name, error)
    for name in ("pytest.ini", ".pytest.ini"):
        if section is None and name in files:
            found, error = _parse_ini_section(files[name], "pytest")
            section = found or {}
            config["source"] = name
            if error is not None:
                section = unparsable(name, error)
    if section is None and "pyproject.toml" in files:
        data, error = _parse_toml(files["pyproject.toml"])
        tool = _toml_table(data, "tool", "pytest") or {}
        ini_options = tool.get("ini_options")
        if error is not None:
            section = unparsable("pyproject.toml", error)
        elif isinstance(ini_options, dict):
            section = ini_options
            config["source"] = "pyproject.toml"
        elif tool:
            # pytest >= 9 native TOML table: [tool.pytest] with real TOML values.
            section = {k: v for k, v in tool.items() if k != "ini_options"}
            config["source"] = "pyproject.toml [tool.pytest]"
    for name, table in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        if section is None and name in files:
            section, error = _parse_ini_section(files[name], table)
            if error is not None:
                section = unparsable(name, error)
            elif section is not None:
                config["source"] = name
    if section is None and runner_args:
        section = {}
    if section is not None and (section or runner_args):
        section = dict(section)
        cli = tuple(runner_args)
        overrides = _ini_overrides(cli)
        if "addopts" in overrides:
            section["addopts"] = overrides["addopts"]
        for key, value in _ini_overrides(_split(section.get("addopts", "")) + cli).items():
            if key != "addopts":
                section[key] = value
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
# pytest's fixtures that run a pytest session of their own (``pytester``,
# the legacy ``testdir``): a test using one depends on every fixture the
# project's registered plugins define (``_Resolver.lifecycle``).
PYTESTER_FIXTURES = frozenset({"pytester", "testdir"})


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


def _reaches(path: str, config: dict[str, Any], is_dir: bool = False) -> bool:
    """Whether pytest's walk from an initial path (``config["initial"]``:
    the paths among its arguments, else ``testpaths``, else the rootdir)
    reaches ``path``. An initial path itself is always collected; below one,
    pytest asks ``pytest_ignore_collect`` about every directory and file it
    walks to, and its own implementation skips ``__pycache__``, an
    ``--ignore`` entry, an ``--ignore-glob`` match (``fnmatch``, ``*``
    crossing directories; both made absolute against the invocation
    directory, the repository root here) and a directory matching
    ``norecursedirs``. Neither ignores nor ``norecursedirs`` apply to an
    initial path or the directories above it. A conftest's own
    ``pytest_ignore_collect`` that may return False overrides all of them
    for what lies below its directory (``config["unignore_dirs"]``: the hook
    is firstresult, and conftests come first)."""
    ignores = {e for e in config.get("ignore", ()) if e}
    globs = [g for g in config.get("ignore_glob", ()) if g]
    norecurse = tuple(config.get("norecursedirs", ()))
    unignore = config.get("unignore_dirs") or ()
    initial_paths = [_normalise_testpath(i) for i in config.get("initial") or (".",)]

    def skipped(current: str, name: str, directory: bool) -> bool:
        if name == "__pycache__":
            return True
        if any(d == "" or current.startswith(d + "/") for d in unignore):
            return False
        # pytest asks about a directory only when it is neither an initial
        # path nor above one (``isinitpath(path, with_parents=True)``).
        if directory and any(i == current or i.startswith(current + "/") for i in initial_paths):
            return False
        if current in ignores or any(fnmatch(current, g) for g in globs):
            return True
        return directory and any(_matches_python_file(current, p) for p in norecurse)

    for initial in config.get("initial") or (".",):
        base = _normalise_testpath(initial)
        if base == ".":
            base = ""
        if path == base:
            return True  # named as an initial path: always collected
        if base and not path.startswith(base + "/"):
            continue
        parts = (path[len(base) + 1 :] if base else path).split("/")
        current = base
        for i, part in enumerate(parts):
            current = f"{current}/{part}" if current else part
            if skipped(current, part, is_dir or i < len(parts) - 1):
                break
        else:
            return True
    return False


def _prefix_free(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Initial paths as pytest keeps them (``normalize_collection_arguments``):
    one inside another, or repeated, is dropped; order is kept."""
    normal = ["" if p in (".", "") else p for p in (_normalise_testpath(p) for p in paths)]

    def inside(path: str, other: str) -> bool:
        return other == "" or path.startswith(other + "/")

    kept: list[str] = []
    for i, path in enumerate(normal):
        if path in normal[:i]:
            continue
        if any(inside(path, other) for other in normal if other != path):
            continue
        kept.append(paths[i])
    return tuple(kept)


def _excluded(path: str, snapshot: Snapshot) -> bool:
    """Whether ``diffcone.toml``'s ``[index] exclude`` keeps ``path`` out of
    the analysis (it is read as an other file then, not outside the roots)."""
    return any(fnmatchcase(path, pattern) for pattern in snapshot.info.excluded)


def _unread_because(path: str, snapshot: Snapshot) -> str:
    if _excluded(path, snapshot):
        return "diffcone.toml's [index] exclude keeps it out of the analysis"
    return "it is outside the source roots"


def _unread_remedy(path: str, snapshot: Snapshot) -> str:
    if _excluded(path, snapshot):
        return "narrow the exclude pattern that matches it"
    return (
        "add a source root that contains it (for a src layout: --source-root src --source-root .)"
    )


def _parent_dir(path: str) -> str:
    """The repository-relative directory holding ``path``; "" at the root."""
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _startup_conftest_dirs(initial: Iterable[str], directories: set[str]) -> set[str]:
    """Directories whose ``conftest.py`` pytest imports at startup, before
    collecting anything (``_set_initial_conftests``): for each initial path,
    its directory (a file's parent) and every directory above it, and for a
    directory also each ``test*`` directory directly in it. These load
    whatever ``--ignore`` or ``norecursedirs`` say, so their session hooks
    and import-time code apply to the run."""
    found: set[str] = {""}
    for entry in initial:
        anchor = _normalise_testpath(entry)
        anchor = "" if anchor == "." else anchor
        is_dir = anchor == "" or anchor in directories
        here = anchor if is_dir else _parent_dir(anchor)
        while here:
            found.add(here)
            here = _parent_dir(here)
        if is_dir:
            found.update(
                d
                for d in directories
                if _parent_dir(d) == anchor and PurePosixPath(d).name.startswith("test")
            )
    return found


def _directories(paths: Iterable[str]) -> set[str]:
    """Every directory holding one of ``paths``, at any depth (not "")."""
    found: set[str] = set()
    for path in paths:
        here = _parent_dir(path)
        while here and here not in found:
            found.add(here)
            here = _parent_dir(here)
    return found


def _glob_match(pattern: list[str], parts: list[str]) -> bool:
    """``glob``'s matching of a path's components (``recursive=True``):
    ``**`` alone is any number of directories, other wildcards stay within a
    component, and a hidden name needs a pattern that starts with a dot."""
    if not pattern:
        return not parts
    head = pattern[0]
    if head == "**":
        return any(
            _glob_match(pattern[1:], parts[i:])
            for i in range(len(parts) + 1)
            if not any(p.startswith(".") for p in parts[:i])
        )
    if not parts:
        return False
    if parts[0].startswith(".") and not head.startswith("."):
        return False
    return fnmatch(parts[0], head) and _glob_match(pattern[1:], parts[1:])


def _expand_testpaths(entries: Iterable[str], paths: set[str], directories: set[str]) -> list[str]:
    """``testpaths`` as pytest expands it (``glob.iglob(entry,
    recursive=True)`` against the rootdir): each entry that names a file or
    directory, and what a wildcard entry matches, in order."""
    found: list[str] = []
    for raw in entries:
        tp = _normalise_testpath(raw)
        if tp in ("", "."):
            found.append(".")
        elif not any(ch in tp for ch in "*?["):
            if tp in paths or tp in directories:
                found.append(tp)
        else:
            pattern = tp.split("/")
            found.extend(
                sorted(p for p in paths | directories if _glob_match(pattern, p.split("/")))
            )
    return list(dict.fromkeys(found))


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


# pytest options (and common plugins') that take no value, so a path right
# after one is a positional argument, not the option's value.
FLAG_OPTIONS = frozenset(
    {
        "-v",
        "-vv",
        "-vvv",
        "-q",
        "-qq",
        "-x",
        "-s",
        "-l",
        "-ra",
        "-rA",
        "-rf",
        "-rs",
        "-rx",
        "--verbose",
        "--quiet",
        "--exitfirst",
        "--showlocals",
        "--lf",
        "--last-failed",
        "--ff",
        "--failed-first",
        "--nf",
        "--new-first",
        "--sw",
        "--stepwise",
        "--strict-markers",
        "--strict-config",
        "--strict",
        "--no-header",
        "--no-summary",
        "--collect-only",
        "--co",
        "--doctest-modules",
        "--runxfail",
        "--disable-warnings",
        "--disable-pytest-warnings",
        "--setup-show",
        "--full-trace",
        "--cache-clear",
        "--trace",
        "--pdb",
        "--no-cov",
        "--keep-duplicates",
        "--collect-in-virtualenv",
        "--noconftest",
        "--disable-plugin-autoload",
        "--doctest-continue-on-failure",
        "--doctest-ignore-import-errors",
        "--pyargs",
    }
)


# pytest options (and common plugins') that always take a value, so the
# token after one written apart (``--ignore tests/slow``) is that value, not
# a path to collect.
VALUE_OPTIONS = frozenset(
    {
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--doctest-glob",
        "--doctest-report",
        "--import-mode",
        "-p",
        "-k",
        "-m",
        "-c",
        "--config-file",
        "-o",
        "--override-ini",
        "--rootdir",
        "--confcutdir",
        "--basetemp",
        "--junitxml",
        "--junit-xml",
        "--junit-prefix",
        "--durations",
        "--durations-min",
        "--maxfail",
        "--tb",
        "--capture",
        "--assert",
        "--color",
        "--code-highlight",
        "-W",
        "--pythonwarnings",
        "-r",
        "--log-level",
        "--log-format",
        "--log-date-format",
        "--log-file",
        "--log-file-level",
        "--log-file-format",
        "--log-cli-level",
        "--log-cli-format",
        "--report-log",
        "-n",
        "--numprocesses",
        "--dist",
        "--maxprocesses",
        "--max-worker-restart",
        "--timeout",
        "--reruns",
        "--reruns-delay",
        "--randomly-seed",
        "--cov-report",
        "--cov-config",
        "--cov-fail-under",
        "--cov-context",
    }
)


def _argument_paths(
    args: tuple[str, ...], paths: set[str]
) -> tuple[list[str], list[str], list[str]]:
    """Positional paths among pytest's arguments (``addopts`` included:
    pytest prepends it to the command line), which pytest collects from
    instead of ``testpaths`` (a file named there whatever ``python_files``
    says), and tokens that name a path but follow an option that may take a
    value (``--cov src``), which cannot be told apart. A token counts as a
    path only when it names a file or directory of the tree (``.`` is the
    rootdir) and stands first, after a ``--opt=value`` token, after a known
    flag (``FLAG_OPTIONS``), after the value of an option that always takes
    one (``VALUE_OPTIONS``: ``--ignore tests/slow``) or after another
    positional. Also returned: tokens in a positional place that look like a
    path but name nothing in the tree (an absolute path), which leave
    ``testpaths`` alone."""
    found: list[str] = []
    ambiguous: list[str] = []
    unknown: list[str] = []
    previous: str | None = None
    for token in args:
        if token.startswith("-") and token != "-":
            previous = token
            continue
        if previous in VALUE_OPTIONS:
            previous = token  # the option's value
            continue
        path = _normalise_testpath(token.split("::", 1)[0].replace("\\", "/"))
        positional = (
            previous is None
            or "=" in previous
            or previous in FLAG_OPTIONS
            or not previous.startswith("-")
        )
        if path in ("", ".") and "::" not in token:
            if positional:
                found.append(".")  # the rootdir: everything
            else:
                ambiguous.append(token)
            previous = token
            continue
        names_path = path not in ("", ".") and (
            path in paths or any(p.startswith(path + "/") for p in paths)
        )
        if names_path and "::" in token:
            ambiguous.append(token)  # a node id: narrower than its file
        elif names_path and positional:
            found.append(path)
        elif names_path:
            ambiguous.append(token)
        elif positional and (
            "/" in token
            or "\\" in token
            or token.endswith(".py")
            or PurePosixPath(token).is_absolute()
        ):
            unknown.append(token)
        previous = token
    return found, ambiguous, unknown


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


def _star_exports(facts: ModuleFacts, module_facts, seen: set[str] | None = None) -> list[str]:
    """What ``from <facts' module> import *`` binds, a module's own star
    imports included: without a literal ``__all__``, the public names it
    gets from ``from x import *`` count too, at any depth (a conftest
    star-importing ``helpers.a``, which star-imports ``helpers.b``)."""
    top = seen is None
    if top and facts.star_exports is not None:
        return facts.star_exports
    names, exact = _star_names_exact(facts.parsed.tree)
    if not exact:
        seen = set() if seen is None else seen
        seen.add(facts.parsed.module)
        for kind, _, source, _ in facts.bindings:
            if kind != "star" or source in seen:
                continue
            origin = module_facts(source)
            if origin is not None:
                names = names + [
                    n for n in _star_exports(origin, module_facts, seen) if not n.startswith("_")
                ]
        names = list(dict.fromkeys(names))
    if top:
        facts.star_exports = names
    return names


def _star_names_exact(tree: ast.Module) -> tuple[list[str], bool]:
    """What ``from <module> import *`` binds, and whether that is exactly a
    literal ``__all__``: ``__all__`` when it is set once to a literal list of
    strings; with no ``__all__``, every name the module binds at top level
    (definitions, imports and assignments alike) that does not start with an
    underscore; with an ``__all__`` built otherwise (``base.__all__ +
    [...]``, ``__all__ += [...]``, ``__all__.extend``), both and every string
    it names: a superset. The names the module itself star-imports are not
    here (_star_exports adds them)."""
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
        return literal, True
    bound: list[str] = []
    for stmt in iter_scope_statements(tree.body):
        if isinstance(stmt, DEF_NODES):
            bound.append(stmt.name)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            bound += [a.asname or a.name.split(".")[0] for a in stmt.names if a.name != "*"]
        elif (assigned := _assigned(stmt)) is not None:
            bound.append(assigned[0])
    public = [n for n in bound if not n.startswith("_")]
    return list(dict.fromkeys([*(literal or []), *strings, *public])), False


def _ini_section(raw: bytes, name: str) -> dict[str, Any] | None:
    return _parse_ini_section(raw, name)[0]


def _parse_ini_section(raw: bytes, name: str) -> tuple[dict[str, Any] | None, str | None]:
    """The INI file's section ``name`` (None when it has none), or None and
    why the file does not parse."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(raw.decode("utf-8"))
    except (configparser.Error, UnicodeDecodeError) as exc:
        return None, str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    if not parser.has_section(name):
        return None, None
    return dict(parser.items(name)), None


# Hooks that turn files or objects into tests by a plugin's own rules, so the
# target list cannot be the suite (scrapy's docs/conftest.py binds
# ``pytest_collect_file`` to a Sybil instance). ``pytest_generate_tests`` is
# not one: it multiplies a test function that is already a target. (Not to be
# confused with session_state.COLLECTION_HOOKS, every path-scoped hook pytest
# calls while collecting, whose import-time effects reach every test.)
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
# variable symbols, every test in the module, or under the conftest, depends
# on them. (``collect_ignore`` and ``collect_ignore_glob`` are not applied:
# the files they name stay targets, which only over-collects.)
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
    # The fixture's function and the module it is read in, for what it
    # requests in ways its parameters do not say (_RequestFlow), and for
    # what it stores into ``doctest_namespace`` (_namespace_modules).
    node: ast.FunctionDef | ast.AsyncFunctionDef | None = field(
        default=None, compare=False, repr=False
    )
    module: str = field(default="", compare=False, repr=False)


@dataclass
class ModuleFacts:
    parsed: ParsedModule
    fixtures: dict[str, Fixture] = field(default_factory=dict)
    hooks: list[str] = field(default_factory=list)
    # The hooks among them pytest calls for the whole session, not through a
    # node's path (see PATH_SCOPED_HOOKS).
    session_hooks: list[str] = field(default_factory=list)
    # Collection hooks this module binds (defined, assigned or imported,
    # whatever they are bound to): they make tests out of files or objects
    # these rules do not model.
    collect_hooks: list[str] = field(default_factory=list)
    # The plugin modules it names (``pytest_plugins``, also bound by import,
    # and ``import_plugin``), and what in ``pytest_plugins`` could not be
    # read (empty when it all reads): _declared_plugins. ``declared_plugins``
    # is what its own ``pytest_plugins`` statements name, None without one.
    plugins: list[str] = field(default_factory=list)
    unreadable_plugins: str = ""
    declared_plugins: list[str] | None = None
    setup_functions: list[str] = field(default_factory=list)
    # Attribute name -> (fixture, whether it names itself): what pytest finds
    # bound in the module's namespace. Fixtures defined here start it; names
    # bound to them by alias or import are added by _link_fixtures.
    fixture_attrs: dict[str, tuple[Fixture, bool]] = field(default_factory=dict)
    # Module-level bindings that may bind a fixture, in order: ("alias",
    # name, "", other_name), ("import", name, module, imported_name) and
    # ("star", "", module, ""). ``name = pkg.mod.attr`` through an imported
    # module is an "import" of ``attr`` from ``pkg.mod``.
    bindings: list[tuple[str, str, str, str]] = field(default_factory=list)
    # Module-level names bound to what ``bindings`` cannot follow: the
    # value's source when it may be a function or class (an attribute, a
    # subscript, a name bound with others, a call's result: _may_hold_code),
    # "" when it is data (a literal, a container, a module).
    opaque: dict[str, str] = field(default_factory=dict)
    # Of the names bound by assignment, those whose value may be a fixture
    # discovery cannot read (a call, an attribute of a value, a subscript:
    # _may_hold_code); a literal or a container is none.
    unread_values: set[str] = field(default_factory=set)
    # What ``from <module> import *`` binds (_star_exports), once computed.
    star_exports: list[str] | None = None
    # Whether the module binds a ``pytest_ignore_collect`` that may return
    # False, which collects what ``--ignore``/``norecursedirs`` would skip.
    unignores: bool = False
    # Modules outside the source roots it star-imports (_link_bound_hooks):
    # what they bind, a collection hook among it, is unknown.
    unknown_stars: list[str] = field(default_factory=list)
    # Fixtures its code adds to what tests request, in a hook or anywhere
    # (``item.fixturenames.append("db")``,
    # ``config.addinivalue_line("usefixtures", "db")``): _session_requests.
    session_requests: list[str] = field(default_factory=list)


def _fixture_decorator(parts: list[str]) -> bool:
    """Whether a decorator's dotted name makes a fixture: its last part ends
    in ``fixture`` (``pytest.fixture``, the deprecated ``yield_fixture``,
    pytest-asyncio's ``fixture``, pytest-twisted's ``async_fixture`` and
    ``async_yield_fixture``, pytest-trio's ``trio_fixture``). They take
    pytest's ``name``, ``scope`` and ``autouse`` keywords, or none."""
    return bool(parts) and parts[-1].endswith("fixture")


def _explicit_name(call: ast.Call | None) -> str | None:
    """A fixture decorator's literal ``name=``."""
    node = keyword_value(call, "name")
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _makes_fixture(dec: ast.expr) -> tuple[bool, ast.Call | None]:
    """Whether a decorator (or the callee of ``x = pytest.fixture(f)``) is a
    fixture decorator, by name (_fixture_decorator) or through an alias its
    module binds to one (``from pytest import fixture as fx``:
    common.fixture_aliases), and its call."""
    parts, call = decorator_chain(dec)
    return _fixture_decorator(parts) or names_fixture_alias(dec), call


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, str | None, bool]:
    """(is_fixture, explicit name, autouse)."""
    for dec in node.decorator_list:
        makes, call = _makes_fixture(dec)
        if makes:
            return True, _explicit_name(call), _autouse(keyword_value(call, "autouse"))
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
    # Of those, the ones only maybe handed to a fixture (``indirect=FLAG``):
    # one that names no fixture anywhere is a parameter (pytest would fail
    # the test otherwise), not an unknown fixture.
    uncertain: frozenset[str] = frozenset()

    def __add__(self, other: Marks) -> Marks:
        return Marks(
            self.usefixtures + other.usefixtures,
            self.parametrized | other.parametrized,
            self.indirect | other.indirect,
            self.uncertain | other.uncertain,
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
    uncertain: set[str] = set()
    if expand is not None:
        exprs = [e for x in exprs for e in (expand(x) or [x])]
    for expr in exprs:
        parts, call = decorator_chain(expr)
        if call is None or len(parts) < 2 or parts[-2] != "mark":
            continue
        if parts[-1] == "usefixtures":
            use.extend(_usefixtures_names(call))
        elif parts[-1] == "parametrize" and call.args:
            names = _split_argnames(call.args[0])
            parametrized.update(names)
            handed, maybe = _indirect_names(call, names)
            indirect.update(handed)
            uncertain.update(handed if maybe else ())
    return Marks(tuple(use), frozenset(parametrized), frozenset(indirect), frozenset(uncertain))


def _usefixtures_names(call: ast.Call) -> list[str]:
    """The fixtures a ``usefixtures`` mark names: its literal strings (in
    lists and tuples too), and ANY_FIXTURE for an argument that is not one
    (``usefixtures(NAME)``, ``usefixtures(*NAMES)``), which may name any
    fixture visible from the test."""
    names: list[str] = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.append(arg.value)
        elif isinstance(arg, (ast.List, ast.Tuple)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in arg.elts
        ):
            names.extend(string_literals(list(arg.elts)))
        else:
            names.append(ANY_FIXTURE)
    if call.keywords:
        names.append(ANY_FIXTURE)  # ``**kwargs``: whatever it holds
    return names


def _indirect_names(call: ast.Call, names: list[str]) -> tuple[list[str], bool]:
    """The argnames a ``parametrize`` call hands to fixtures (``indirect=``,
    by keyword or as the third positional argument), and whether that is
    only maybe: none for no value or a literal false one, those a literal
    list names, every argname for ``True``, and every argname maybe for
    anything else (a variable, a list built otherwise, a value that may
    come through ``*args`` or ``**kwargs``), since a name taken for a
    fixture is followed and one taken for a parameter is pruned."""
    node = keyword_value(call, "indirect")
    unknown = any(k.arg is None for k in call.keywords) or any(
        isinstance(a, ast.Starred) for a in call.args[:3]
    )
    if node is None and len(call.args) > 2:
        node = call.args[2]
    if node is None:
        return (list(names), True) if unknown else ([], False)
    if isinstance(node, ast.Constant) and not node.value:
        return [], False
    if isinstance(node, ast.Constant) and node.value is True:
        return list(names), False
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return string_literals(list(node.elts)), False
    return list(names), True


def _mark_entries(value: ast.expr) -> list[ast.expr]:
    """The mark expressions a ``pytestmark`` value, or a variable storing
    marks, holds: the elements of a list or tuple, both sides of ``+``, or
    the value itself. ``pytestmark`` named in its own value (``pytestmark +
    [...]``) adds nothing."""
    if isinstance(value, (ast.List, ast.Tuple)):
        return [e for elt in value.elts for e in _mark_entries(elt)]
    if isinstance(value, ast.Starred):
        return _mark_entries(value.value)
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _mark_entries(value.left) + _mark_entries(value.right)
    if isinstance(value, ast.Name) and value.id == "pytestmark":
        return []
    return [value]


# The list methods that add to a list, with the position of the first
# argument that is an element (``insert(i, x)``).
_LIST_ADDERS = {"append": 0, "extend": 0, "insert": 1, "__iadd__": 0}


def _scope_values(body: list[ast.stmt], name: str) -> list[ast.expr]:
    """Every expression a scope's statements put into the list ``name``:
    assigned (``name = [...]``, also to an item or a slice), added (``name
    += [...]``) or passed to a list method that adds (``name.append(x)``,
    ``.extend``, ``.insert``). Unpacking into ``name`` (``name, x = ...``)
    yields the whole value."""
    values: list[ast.expr] = []

    def named(target: ast.expr) -> bool:
        if isinstance(target, ast.Subscript):
            target = target.value
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(named(t) for t in target.elts)
        if isinstance(target, ast.Starred):
            return named(target.value)
        return isinstance(target, ast.Name) and target.id == name

    for stmt in iter_scope_statements(body):
        if isinstance(stmt, ast.Assign):
            if any(named(t) for t in stmt.targets):
                values.append(stmt.value)
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            if stmt.value is not None and named(stmt.target):
                values.append(stmt.value)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == name
                and func.attr in _LIST_ADDERS
            ):
                values += stmt.value.args[_LIST_ADDERS[func.attr] :]
    return values


def _marks_from_pytestmark(body: list[ast.stmt], expand: MarkExpander | None = None) -> Marks:
    """The marks a module or class body applies through ``pytestmark``,
    however it builds the list (_scope_values: every addition counts)."""
    exprs = [e for value in _scope_values(body, "pytestmark") for e in _mark_entries(value)]
    return _marks_from_expressions(exprs, expand)


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


def _imported_plugins(parsed: ParsedModule) -> list[str]:
    """Modules registered by name at run time anywhere in the module
    (``config.pluginmanager.import_plugin("tests.envplug")`` in a hook):
    pytest registers them for the session as it does ``pytest_plugins``. A
    name that is not a literal is not followed."""
    found: list[str] = []
    if b"import_plugin" not in parsed.source:
        return found  # the tree cannot hold the call
    for node in ast.walk(parsed.tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_plugin"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value not in found
        ):
            found.append(node.args[0].value)
    return found


def _declared_plugins(body: list[ast.stmt]) -> tuple[list[str], str]:
    """The modules a module's ``pytest_plugins`` names, however it builds
    the list (_scope_values: assigned, ``+=``, ``.append``, ``.extend``):
    literal strings (a string is a comma-separated list, as pytest splits
    it), lists, tuples, ``+``, ``list(...)`` and module-level names bound
    to those (every binding of a name counts). Also the first part that
    cannot be read that way (a call, a comprehension, an imported name), so
    the caller can treat the list as unknown; empty when it all reads."""
    assigned: dict[str, list[ast.expr]] = {}
    for name, value in scope_assignments(body):
        assigned.setdefault(name, []).append(value)
    found: list[str] = []
    unreadable: list[str] = []

    def read(expr: ast.expr, depth: int = 0) -> None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            found.extend(p.strip() for p in expr.value.split(",") if p.strip())
        elif isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            for elt in expr.elts:
                read(elt, depth)
        elif isinstance(expr, ast.Starred):
            read(expr.value, depth)
        elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            read(expr.left, depth)
            read(expr.right, depth)
        elif isinstance(expr, ast.Name) and expr.id == "pytest_plugins":
            pass  # what it held already
        elif isinstance(expr, ast.Name) and expr.id in assigned and depth < 8:
            for value in assigned[expr.id]:
                read(value, depth + 1)
        elif (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id in ("list", "tuple")
            and len(expr.args) == 1
            and not expr.keywords
        ):
            read(expr.args[0], depth)
        else:
            unreadable.append(ast.unparse(expr))

    for value in _scope_values(body, "pytest_plugins"):
        read(value)
    return list(dict.fromkeys(found)), (unreadable[0] if unreadable else "")


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


def _add_bound(
    facts: ModuleFacts, name: str, symbol: str | None, defined_here: bool = False
) -> None:
    """Register what the module's namespace binds to ``name``, as pytest
    finds it there: a ``pytest_*`` hook or an xunit setup function, defined
    here (``defined_here``) or bound by assignment, import or star import.
    ``symbol`` is the function, None when it is not one in the source roots
    (a value, or a function from outside them). A collection hook counts
    whatever it is bound to; a ``pytest_ignore_collect`` whose code is not
    here may return False."""
    if name in COLLECT_HOOKS and name not in facts.collect_hooks:
        facts.collect_hooks.append(name)
    if name == "pytest_ignore_collect" and not defined_here:
        facts.unignores = True
    if symbol is None:
        return
    if name in MODULE_SETUP_FUNCTIONS:
        facts.setup_functions.append(symbol)
    elif name.startswith("pytest_"):
        facts.hooks.append(symbol)
        if name not in PATH_SCOPED_HOOKS:
            facts.session_hooks.append(symbol)


def _collect_facts(parsed: ParsedModule) -> ModuleFacts:
    facts = ModuleFacts(parsed=parsed)
    body = parsed.tree.body
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for func in scope_functions(body):
        functions[func.name] = func
        is_fixture, explicit, autouse = _is_fixture(func)
        symbol = parsed.member_id(func.name)
        if is_fixture:
            name = explicit or func.name
            fixture = Fixture(
                name, symbol, autouse, _fixture_requests(func, False), func, parsed.module
            )
            facts.fixtures[name] = fixture
            facts.fixture_attrs[func.name] = (fixture, explicit is not None)
        elif func.name.startswith("pytest_") or func.name in MODULE_SETUP_FUNCTIONS:
            _add_bound(facts, func.name, symbol, defined_here=True)
    for name, value in scope_assignments(body):
        # ``pytest_collect_file = Sybil(...).pytest()``: a collection hook
        # bound to a value rather than defined as a function.
        if name in COLLECT_HOOKS:
            _add_bound(facts, name, None)
            continue
        # ``mocker = pytest.fixture(scope="function")(_mocker)``: a fixture
        # made by calling the decorator on an in-module function and binding
        # the result; ``x = pytest.fixture(f, autouse=True)`` is the same in
        # one call.
        if not isinstance(value, ast.Call):
            continue
        makes, call = _makes_fixture(value.func if isinstance(value.func, ast.Call) else value)
        if not makes or len(value.args) != 1:
            continue
        target = value.args[0]
        if not (isinstance(target, ast.Name) and target.id in functions):
            continue
        func = functions[target.id]
        explicit_name = _explicit_name(call)
        fixture_name = explicit_name or name
        autouse = _autouse(keyword_value(call, "autouse"))
        fixture = Fixture(
            fixture_name,
            parsed.member_id(func.name),
            autouse,
            _fixture_requests(func, False),
            func,
            parsed.module,
        )
        facts.fixtures[fixture_name] = fixture
        facts.fixture_attrs[name] = (fixture, explicit_name is not None)
    # Names bound to a fixture defined elsewhere (``box2 = box``, ``from
    # pkg.conftest import engine``, ``auto = pkg.fx.auto``): pytest registers
    # a fixture under every name the module binds it to (_link_fixtures
    # resolves them).
    modules: dict[str, str] = {}  # name -> the module it binds
    origins = _import_origins(body)
    for stmt in iter_scope_statements(body):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    modules[alias.asname] = alias.name
                else:
                    modules[alias.name.split(".")[0]] = alias.name.split(".")[0]
                facts.opaque[alias.asname or alias.name.split(".")[0]] = ""
        elif isinstance(stmt, ast.ImportFrom):
            source = parsed.import_source(stmt)
            for alias in stmt.names:
                if alias.name == "*":
                    facts.bindings.append(("star", "", source, ""))
                else:
                    facts.bindings.append(
                        ("import", alias.asname or alias.name, source, alias.name)
                    )
                    modules[alias.asname or alias.name] = f"{source}.{alias.name}"
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Name)
        ):
            facts.bindings.append(("alias", stmt.targets[0].id, "", stmt.value.id))
        elif (
            (assigned := _assigned(stmt)) is not None
            and isinstance(assigned[1], ast.Attribute)
            and len(parts := decorator_chain(assigned[1])[0]) >= 2
            and parts[0] in modules
        ):
            source = ".".join([modules[parts[0]], *parts[1:-1]])
            facts.bindings.append(("import", assigned[0], source, parts[-1]))
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            value = stmt.value
            # A call's result may be a test too (``test_one = make(1)``).
            text = _shown(value) if _may_bind_test(value, origins) else ""
            for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                if isinstance(target, ast.Name):
                    facts.opaque[target.id] = text
                if _may_hold_code(value):
                    facts.unread_values.update(_target_names(target))
    declared, facts.unreadable_plugins = _declared_plugins(body)
    if _scope_values(body, "pytest_plugins"):
        facts.declared_plugins = declared
    facts.plugins = declared + _imported_plugins(parsed)
    facts.unignores = facts.unignores or _may_unignore(body)
    facts.session_requests = _session_requests(parsed)
    return facts


def _added_names(node: ast.expr) -> list[str]:
    """The fixture names an added value holds: a literal string (split at
    whitespace), the strings of a literal list or tuple, ``f.__name__`` of a
    plain name (the function's name), else ANY_FIXTURE."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split()
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [n for elt in node.elts for n in _added_names(elt)]
    if isinstance(node, ast.Starred):
        return _added_names(node.value)
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Name)
    ):
        return [node.value.id]
    return [ANY_FIXTURE]


def _session_requests(parsed: ParsedModule) -> list[str]:
    """Fixtures a conftest or plugin module adds to the tests' requests in
    code (a hook, usually): ``<item>.fixturenames.append(name)`` (also
    ``extend``, ``insert``, ``+=``), which pytest's fixture setup reads, and
    ``config.addinivalue_line("usefixtures", name)``, which extends the ini
    option. Which tests a hook adds them to is not read: every test the
    module reaches requests them. A name that is not a literal
    (_added_names), or an ``addinivalue_line`` whose option is not one, is
    ANY_FIXTURE."""
    source = parsed.source
    if b"fixturenames" not in source and b"addinivalue_line" not in source:
        return []
    found: list[str] = []
    for node in ast.walk(parsed.tree):
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            target = node.target
            if isinstance(target, ast.Attribute) and target.attr == "fixturenames":
                found += _added_names(node.value)
            continue
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        func = node.func
        if (
            isinstance(func.value, ast.Attribute)
            and func.value.attr == "fixturenames"
            and func.attr in _LIST_ADDERS
        ):
            found += [n for a in node.args[_LIST_ADDERS[func.attr] :] for n in _added_names(a)]
        elif func.attr == "addinivalue_line" and node.args:
            option = node.args[0]
            if isinstance(option, ast.Constant) and option.value != "usefixtures":
                continue
            if not isinstance(option, ast.Constant) or len(node.args) < 2:
                found.append(ANY_FIXTURE)
            else:
                found += _added_names(node.args[1])
    return list(dict.fromkeys(found))


# Values that are data, never a fixture: literals, containers and
# comprehensions, f-strings, lambdas, comparisons.
_DATA_VALUES = (
    ast.Constant,
    ast.JoinedStr,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Lambda,
    ast.Compare,
)


def _may_hold_code(value: ast.expr) -> bool:
    """Whether a value bound to a name may be a fixture: anything but data
    (_DATA_VALUES), a call's result included (``client =
    make_fixture(_client)``)."""
    return not isinstance(value, _DATA_VALUES)


# Standard-library modules whose functions and classes return data, never a
# function or class pytest would collect: a test name bound to what they
# return (``test_dir = os.path.dirname(__file__)``, ``test_cases =
# json.load(f)``) binds no test. Modules that make callables (functools,
# operator, types, unittest.mock, collections' namedtuple) are not here.
_DATA_MODULES = frozenset(
    {
        "base64", "binascii", "calendar", "codecs", "csv", "datetime", "decimal",
        "fnmatch", "fractions", "glob", "gzip", "hashlib", "html", "io", "ipaddress",
        "json", "locale", "math", "ntpath", "os", "pathlib", "platform", "posixpath",
        "pprint", "random", "re", "secrets", "shutil", "socket", "statistics",
        "string", "struct", "subprocess", "sys", "sysconfig", "tempfile",
        "textwrap", "time", "unicodedata", "urllib", "uuid", "zlib",
    }
)  # fmt: skip
# Builtins whose result is data, never a function or class (a container's
# items may be: ``max``, ``min`` and ``next`` are not here).
_DATA_BUILTINS = frozenset(
    {
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "chr",
        "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset",
        "hash", "hex", "id", "input", "int", "isinstance", "issubclass", "iter",
        "len", "list", "map", "oct", "open", "ord", "pow", "range", "repr",
        "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
    }
)  # fmt: skip
# _import_origins: a module-level class with no bases, keywords, decorators
# or ``__new__``, whose call makes an instance (no function or class); and
# any other name the module defines (no builtin, whatever its name).
_PLAIN_CLASS = "<plain class>"
_DEFINED = "<defined>"


def _import_origins(body: list[ast.stmt]) -> dict[str, str]:
    """What each name a module imports names, as a dotted path (``from
    pathlib import Path`` binds ``Path`` to ``pathlib.Path``), and the
    functions and classes it defines (_PLAIN_CLASS, _DEFINED)."""
    origins: dict[str, str] = {}
    for stmt in iter_scope_statements(body):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".")[0]
                origins[bound] = alias.name if alias.asname else bound
        elif isinstance(stmt, ast.ImportFrom) and not stmt.level and stmt.module:
            for alias in stmt.names:
                if alias.name != "*":
                    origins[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"
        elif isinstance(stmt, ast.ClassDef):
            plain = not (stmt.bases or stmt.keywords or stmt.decorator_list) and not any(
                f.name == "__new__" for f in scope_functions(stmt.body)
            )
            origins[stmt.name] = _PLAIN_CLASS if plain else _DEFINED
        elif isinstance(stmt, FUNC_NODES):
            origins[stmt.name] = _DEFINED
    return origins


def _scalar(value: ast.expr, origins: dict[str, str]) -> bool:
    """Whether a value is data whose items and method results are data too:
    a string or number, what a standard-library module of _DATA_MODULES
    returns (``json.load(f)``, ``Path(__file__)``), arithmetic on such
    values, an item or a method result of one (``''.join(parts)``,
    ``(Path(__file__) / "..").resolve()``). Not a container, whose items may
    be functions, nor an object's attribute, which may be a bound method
    pytest collects (through ``__func__``)."""
    if isinstance(value, (ast.Constant, ast.JoinedStr)):
        return True
    if isinstance(value, ast.BinOp):
        return _scalar(value.left, origins) and _scalar(value.right, origins)
    if isinstance(value, ast.Subscript):
        return _scalar(value.value, origins)
    if isinstance(value, ast.Call):
        func = value.func
        parts, _ = decorator_chain(func)
        if parts and parts[0] in origins:
            return origins[parts[0]].split(".")[0] in _DATA_MODULES
        return isinstance(func, ast.Attribute) and _scalar(func.value, origins)
    return False


def _evidently_data(value: ast.expr, origins: dict[str, str]) -> bool:
    """Whether a value is data for certain, never a function or class
    pytest would collect: a literal, a container or a comprehension
    (_DATA_VALUES), arithmetic, a scalar (_scalar), a string's attribute,
    what a builtin of _DATA_BUILTINS returns, or an instance of a plain
    class the module defines (pip's ``TestPyPI = PackageIndex(...)``). A
    lambda is a function pytest collects under a test's name."""
    if isinstance(value, ast.Lambda):
        return False
    if isinstance(value, _DATA_VALUES + (ast.BinOp, ast.UnaryOp)) or _scalar(value, origins):
        return True
    if isinstance(value, ast.Attribute):
        return isinstance(value.value, (ast.Constant, ast.JoinedStr))
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        origin = origins.get(value.func.id)
        return origin == _PLAIN_CLASS or (origin is None and value.func.id in _DATA_BUILTINS)
    return False


def _may_bind_test(value: ast.expr, origins: dict[str, str]) -> bool:
    """Whether a value bound to a test's name may be a test discovery cannot
    follow: anything not evidently data (_evidently_data), a lambda
    included."""
    return not _evidently_data(value, origins)


def _target_names(target: ast.expr) -> list[str]:
    """The names an assignment target binds (``a, b = ...`` binds both)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for elt in target.elts for n in _target_names(elt)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _unread_decorated(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A function under a decorator, none of which discovery recognises as
    making a fixture: what the decorators return may be one (a plugin's own
    fixture decorator), whose requests discovery cannot read."""
    return bool(func.decorator_list) and not _is_fixture(func)[0]


def _may_unignore(body: list[ast.stmt]) -> bool:
    """Whether a module binds a ``pytest_ignore_collect`` that may return
    False (a non-None result other than True stops pytest's own hook, which
    applies ``--ignore``, ``--ignore-glob`` and ``norecursedirs``): one whose
    every ``return`` is ``True``, ``None`` or bare does not; one bound by
    import or assignment, whose code is not here, may."""
    for stmt in iter_scope_statements(body):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name != "pytest_ignore_collect":
                continue
            for node in ast.walk(stmt):
                if isinstance(node, ast.Return) and not (
                    node.value is None
                    or (
                        isinstance(node.value, ast.Constant)
                        and (node.value.value is True or node.value.value is None)
                    )
                ):
                    return True
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name) == "pytest_ignore_collect" for a in stmt.names):
                return True
            if isinstance(stmt, ast.ImportFrom) and any(a.name == "*" for a in stmt.names):
                continue  # a star import: what it binds is checked where it is followed
        elif any(
            isinstance(t, ast.Name) and t.id == "pytest_ignore_collect"
            for t in getattr(stmt, "targets", [getattr(stmt, "target", None)])
        ):
            return True
    return False


class _ClassFixtures(dict[str, Fixture]):
    """A class's fixtures (_class_level), and in ``unread`` the attributes
    that may be fixtures discovery cannot read: a method under decorators
    none of which it recognises, a name bound to a value that may hold code
    (_may_hold_code)."""

    unread: frozenset[str] = frozenset()


def _class_level(
    owners: list[tuple[ast.ClassDef, str]], module_of: Callable[[str], str] = lambda _: ""
) -> _ClassFixtures:
    """The fixtures pytest registers for a class, which it reads from
    ``dir(cls)``: ``owners`` is the class and its resolved bases, nearest
    first, wherever they are defined. Each attribute comes from the nearest
    owner defining it, so an override hides the base's fixture (and a
    fixture requesting its own name reaches past both); two attributes
    registering one name resolve to the alphabetically last, as pytest
    registers them in ``dir`` order."""
    attrs: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = {}
    unread: set[str] = set()
    for owner, owner_id in owners:
        for func in scope_functions(owner.body):
            attrs.setdefault(func.name, (func, owner_id))
        for stmt in iter_scope_statements(owner.body):
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                if _may_hold_code(stmt.value):
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    unread.update(n for t in targets for n in _target_names(t))
    level = _ClassFixtures()
    for attr, (func, owner_id) in sorted(attrs.items()):
        is_fixture, explicit, autouse = _is_fixture(func)
        if is_fixture:
            name = explicit or attr
            level[name] = Fixture(
                name,
                f"{owner_id}.{attr}",
                autouse,
                _fixture_requests(func, True),
                func,
                module_of(owner_id),
            )
        elif _unread_decorated(func):
            unread.add(attr)
    level.unread = frozenset(unread)
    return level


# --------------------------------------------------------------------------- requests


# Builtins that only look at a value: handing ``request`` to one requests
# nothing (``getattr`` is read apart).
_INSPECTING_BUILTINS = frozenset(
    {
        "bool",
        "callable",
        "dir",
        "hasattr",
        "hash",
        "id",
        "isinstance",
        "len",
        "print",
        "repr",
        "str",
        "type",
    }
)
# How deep _RequestFlow follows ``request`` from function to function.
_FLOW_DEPTH = 4
# pytest-lazy-fixtures' ``lf`` and pytest-lazy-fixture's ``lazy_fixture``,
# which request a fixture from a ``parametrize`` value.
_LAZY_FIXTURE_CALLS = frozenset({"lf", "lazy_fixture"})

_Function = ast.FunctionDef | ast.AsyncFunctionDef
# The methods ``self.name(...)`` may call: name -> (the function, the module
# that defines it).
_Methods = dict[str, tuple[_Function, str]]


def _parameters(func: _Function) -> list[str]:
    args = func.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
    return names


def _lazy_fixture_names(nodes: Iterable[ast.AST]) -> list[str]:
    """Fixtures requested through ``lf("name")`` (pytest-lazy-fixtures;
    ``lf("name.attr")`` requests ``name``) or ``lazy_fixture("name")``
    (pytest-lazy-fixture, also a list of names) anywhere in ``nodes``;
    ANY_FIXTURE for one whose argument is not a literal."""
    found: list[str] = []
    for root in nodes:
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            parts, _ = decorator_chain(node.func)
            if not parts or parts[-1] not in _LAZY_FIXTURE_CALLS:
                continue
            arg = node.args[0] if node.args else None
            literals = [arg] if not isinstance(arg, (ast.List, ast.Tuple)) else arg.elts
            for item in literals:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    found.append(item.value.split(".", 1)[0])
                else:
                    found.append(ANY_FIXTURE)
    return found


def _may_lazy_request(source: bytes) -> bool:
    """Whether a module's source can request a lazy fixture (it imports
    pytest-lazy-fixtures or pytest-lazy-fixture)."""
    return b"lazy_fixture" in source or b"lazyfixture" in source


def _is_static(func: _Function) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in func.decorator_list)


# Where ``request`` stands in a function and requests nothing: a test, a
# comparison, a formatted value, an expression statement.
_INERT_PARENTS = (
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.If,
    ast.IfExp,
    ast.While,
    ast.Assert,
    ast.FormattedValue,
    ast.Expr,
)


class _RequestFlow:
    """Fixtures a test or fixture requests through ``request`` in ways its
    own ``request.getfixturevalue("name")`` calls do not say: ``request``
    handed to a helper (``fixture_value(request, "db")``,
    ``self.check(request)``, ``super().test_x(request)``), whose code is read
    in turn with the parameter it lands in (a function defined in the
    caller, one bound in its module or by import within the source roots, a
    method found on the class or its bases; ``_FLOW_DEPTH`` calls deep); an
    alias of ``request.getfixturevalue`` or ``getattr(request,
    "getfixturevalue")``; ``request`` stored, returned or put in a
    container. What discovery cannot read beyond that (a callee outside the
    source roots or one it cannot find) may request any fixture visible
    from the test: ANY_FIXTURE. Attribute uses (``request.param``,
    ``request.node``, ``request.applymarker(...)``) request nothing;
    ``getfixturevalue`` with a literal is _getfixturevalue_names's."""

    def __init__(self, module_facts: Callable[[str], ModuleFacts | None], scopes: _Scopes):
        self.module_facts = module_facts
        self.scopes = scopes
        self._cache: dict[tuple[int, frozenset[str]], list[str]] = {}
        self._lazy: dict[str, list[str]] = {}

    def fixture(self, fixture: Fixture) -> list[str]:
        """What a fixture requests beyond its parameters: through
        ``request`` (requests), and lazy fixtures in its decorator
        (``params=[lf("db")]``)."""
        node = fixture.node
        if node is None:
            return []
        found = self.requests(node, fixture.module)
        facts = self.module_facts(fixture.module) if fixture.module else None
        if facts is None or _may_lazy_request(facts.parsed.source):
            found = found + _lazy_fixture_names(node.decorator_list)
        return found

    def lazy(self, facts: ModuleFacts) -> list[str]:
        """Lazy fixtures named anywhere in a test module, requested by each
        of its tests (which ``parametrize`` holds them is not tracked)."""
        module = facts.parsed.module
        if module not in self._lazy:
            self._lazy[module] = (
                list(dict.fromkeys(_lazy_fixture_names([facts.parsed.tree])))
                if _may_lazy_request(facts.parsed.source)
                else []
            )
        return self._lazy[module]

    def requests(
        self,
        func: _Function,
        module: str,
        holders: frozenset[str] | None = None,
        methods: _Methods | None = None,
        inherited: _Methods | None = None,
        depth: int = 0,
    ) -> list[str]:
        """What ``func`` (defined in ``module``) requests through the names
        in ``holders``, its ``request`` parameter when None; ``methods``
        and ``inherited`` are what ``self.name`` and ``super().name`` find
        on its class."""
        if holders is None:
            holders = frozenset({"request"} & set(_parameters(func)))
        if not holders:
            return []
        key = (id(func), holders)
        if key in self._cache:
            return self._cache[key]
        self._cache[key] = []  # a cycle requests nothing more
        if depth > _FLOW_DEPTH:
            found = [ANY_FIXTURE]
        else:
            found = self._read(func, module, set(holders), methods, inherited, depth)
        self._cache[key] = list(dict.fromkeys(found))
        return self._cache[key]

    def _read(
        self,
        func: _Function,
        module: str,
        holders: set[str],
        methods: _Methods | None,
        inherited: _Methods | None,
        depth: int,
    ) -> list[str]:
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(func):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        # A name bound to the request object (``req = request``) holds it.
        aliases = [
            (node.value.id, node.targets)
            for node in ast.walk(func)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
        ]
        changed = True
        while changed:
            changed = False
            for source, targets in aliases:
                if source in holders:
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id not in holders:
                            holders.add(target.id)
                            changed = True
        found: list[str] = []

        def handed(call: ast.Call, index: int | None, keyword: str | None) -> list[str]:
            return self._handed(
                call, index, keyword, parents, func, module, methods, inherited, depth
            )

        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Name) and node.id in holders and isinstance(node.ctx, ast.Load)
            ):
                continue
            here: ast.AST = node
            parent = parents.get(id(node))
            if isinstance(parent, ast.Starred):
                here, parent = parent, parents.get(id(parent))
            if isinstance(parent, ast.Attribute):
                if parent.attr == "getfixturevalue":
                    call = parents.get(id(parent))
                    if not (isinstance(call, ast.Call) and call.func is parent):
                        found.append(ANY_FIXTURE)  # ``get = request.getfixturevalue``
                continue  # ``request.param``, ``request.node``, ...
            if isinstance(parent, ast.keyword):
                call = parents.get(id(parent))
                if isinstance(call, ast.Call):
                    found += handed(call, None, parent.arg)
                continue
            if isinstance(parent, ast.Call) and parent.func is not here:
                index = next((i for i, a in enumerate(parent.args) if a is here), None)
                if index is not None:
                    found += handed(parent, None if here is not node else index, None)
                continue
            if isinstance(parent, _INERT_PARENTS):
                continue
            if (
                isinstance(parent, ast.Assign)
                and parent.value is node
                and all(isinstance(t, ast.Name) for t in parent.targets)
            ):
                continue  # an alias, a holder itself
            # Returned, yielded, stored or put in a container: read where
            # discovery cannot follow it.
            found.append(ANY_FIXTURE)
        return found

    def _handed(
        self,
        call: ast.Call,
        index: int | None,
        keyword: str | None,
        parents: dict[int, ast.AST],
        func: _Function,
        module: str,
        methods: _Methods | None,
        inherited: _Methods | None,
        depth: int,
    ) -> list[str]:
        """What a callee handed ``request`` (as positional ``index``, as
        ``keyword``, or through ``*args``/``**kwargs`` when both are None)
        requests with it."""
        target = call.func
        if isinstance(target, ast.Name) and target.id in _INSPECTING_BUILTINS:
            return []
        if isinstance(target, ast.Name) and target.id == "getattr":
            name = call.args[1] if len(call.args) > 1 else None
            if isinstance(name, ast.Constant) and name.value != "getfixturevalue":
                return []  # ``getattr(request, "param", None)``
            outer = parents.get(id(call))
            if (
                isinstance(name, ast.Constant)
                and isinstance(outer, ast.Call)
                and outer.func is call
            ):
                # ``getattr(request, "getfixturevalue")("db")``
                spelled = ast.Call(
                    func=ast.Attribute(value=ast.Name("_"), attr="getfixturevalue"),
                    args=outer.args,
                    keywords=outer.keywords,
                )
                return _getfixturevalue_names(spelled)
            return [ANY_FIXTURE]
        found = self._callee(target, func, module, methods, inherited)
        if found is None:
            return [ANY_FIXTURE]
        callee, callee_module, bound, own = found
        params = [a.arg for a in (*callee.args.posonlyargs, *callee.args.args)]
        if bound:
            params = params[1:]
        landed: set[str] = set()
        if keyword is not None:
            if keyword in params or keyword in [a.arg for a in callee.args.kwonlyargs]:
                landed.add(keyword)
            elif callee.args.kwarg is not None:
                landed.add(callee.args.kwarg.arg)
        elif index is not None and index < len(params):
            landed.add(params[index])
        elif index is not None and callee.args.vararg is not None:
            landed.add(callee.args.vararg.arg)
        elif index is None:
            landed.update(_parameters(callee))  # through ``*args`` or ``**kwargs``
        if not landed:
            return []  # an argument it does not take: pytest fails the call
        own_methods, own_inherited = own
        return _getfixturevalue_names(callee) + self.requests(
            callee, callee_module, frozenset(landed), own_methods, own_inherited, depth + 1
        )

    def _callee(
        self,
        target: ast.expr,
        func: _Function,
        module: str,
        methods: _Methods | None,
        inherited: _Methods | None,
    ) -> tuple[_Function, str, bool, tuple[_Methods | None, _Methods | None]] | None:
        """(the function, its module, whether it is bound so that its first
        parameter is taken, what its own ``self`` and ``super()`` find) for
        a callee discovery can read; None for one it cannot."""
        if isinstance(target, ast.Name):
            for node in ast.walk(func):  # a function defined in the caller
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == target.id
                    and node is not func
                ):
                    return node, module, False, (methods, inherited)
            found = _definition_bound_to(module, target.id, self.module_facts) if module else None
            if isinstance(found, tuple) and isinstance(found[1], FUNC_NODES):
                return found[1], found[0].parsed.module, False, (None, None)
            return None
        if not isinstance(target, ast.Attribute):
            return None
        receiver = target.value
        if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
            if methods is not None and target.attr in methods:
                callee, owner = methods[target.attr]
                return callee, owner, not _is_static(callee), (methods, inherited)
            return None
        if (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "super"
        ):
            if inherited is not None and target.attr in inherited:
                callee, owner = inherited[target.attr]
                # Its own ``super()`` reaches further up: not tracked, so
                # what it finds there is unknown.
                return callee, owner, not _is_static(callee), (methods, None)
            return None
        parts, call = decorator_chain(target)
        scope = self.scopes.scope_for(module) if module else None
        if call is None and len(parts) > 1 and scope is not None and parts[0] in scope.prefixes:
            found = _definition_bound_to(_base_source(parts, scope), parts[-1], self.module_facts)
            if isinstance(found, tuple) and isinstance(found[1], FUNC_NODES):
                return found[1], found[0].parsed.module, False, (None, None)
        return None


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
        usefixtures: tuple[str, ...],
        module_facts: Callable[[str], ModuleFacts | None],
        unread: Counter[str],
        flow: _RequestFlow,
    ) -> None:
        self.module = module
        self.conftests = conftests
        self.plugins = plugins
        self.options = options
        self.unresolved = unresolved
        self.assumed = assumed  # external fixture name -> requesting tests
        # The ini option ``usefixtures`` and what hooks add: requested by
        # every test.
        self.usefixtures = usefixtures
        self.module_facts = module_facts
        # Fixture name -> requesting tests, for names a level binds to what
        # discovery cannot read (shadowed).
        self.unread = unread
        # What fixtures request beyond their parameters.
        self.flow = flow
        self._shadowed: dict[str, bool] = {}

    def shadowed(self, class_levels: list[dict[str, Fixture]], name: str) -> bool:
        """Whether a level the lookup searches (classes, module, conftests,
        plugins) binds ``name`` to what may be a fixture discovery cannot
        read: a function under a decorator it does not recognise, a value
        that may hold code, through aliases and imports within the source
        roots (_unread_fixture). Such a name is not missing: it may be that
        level's fixture, with requests nobody can see."""
        if any(name in getattr(level, "unread", ()) for level in class_levels):
            return True
        if name not in self._shadowed:
            self._shadowed[name] = any(
                _unread_fixture(facts.parsed.module, name, self.module_facts)
                for facts in (self.module, *self.conftests, *self.plugins)
            )
        return self._shadowed[name]

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
        uncertain: frozenset[str] = frozenset(),
        reached: list[Fixture] | None = None,
    ) -> list[str]:
        """Fixtures the test's closure reaches (each also added to
        ``reached`` when given). A ``supplied`` name (directly
        parametrized on the test) is a parameter at every depth, as pytest
        replaces the fixture of that name and prunes what it requests. An
        ``uncertain`` name (maybe handed to a fixture: ``indirect=FLAG``) is
        followed when a fixture of that name is found, and is a parameter
        when none is, since pytest fails a test whose indirect argname names
        no fixture."""
        levels = self.chain(class_levels)
        first_plugin = len(levels) - len(self.plugins)
        deps: list[str] = []
        seen: set[tuple[str, int]] = set()
        # (name, first level to search): a fixture that requests its own name
        # (``def db(db)``) refers to the next definition outward.
        queue: deque[tuple[str, int]] = deque((name, 0) for name in [*self.usefixtures, *requests])
        for level in levels:
            for fixture in level.values():
                if fixture.autouse or self.plugin_requested(fixture.name):
                    queue.append((fixture.name, 0))
        while queue:
            name, start = queue.popleft()
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
                matches = [found]
                if level_index >= first_plugin:
                    # Among plugins the one registered last wins, and the
                    # order pytest registers them in (``-p``, entry points,
                    # PYTEST_PLUGINS, then as files declaring them load) is
                    # not tracked: every plugin's definition counts.
                    matches = [
                        (i, lvl[name])
                        for i, lvl in enumerate(levels)
                        if i >= level_index and name in lvl
                    ]
                for level_index, fixture in matches:
                    deps.append(fixture.symbol)
                    if reached is not None:
                        reached.append(fixture)
                    for req in [*fixture.requests, *self.flow.fixture(fixture)]:
                        queue.append((req, level_index + 1 if req == name else 0))
            elif name in BUILTIN_FIXTURES:
                queue.extend((req, 0) for req in BUILTIN_REQUESTS.get(name, ()))
                if name in PYTESTER_FIXTURES:
                    # The test runs a pytest session of its own, with the
                    # project's registered plugins loaded: whatever they
                    # define may run in it (the inline test code requests
                    # fixtures by name the indexer cannot see), so every
                    # plugin fixture is a dependency; the plugin modules and
                    # hooks are dependencies of every test already. What the
                    # inline code imports is the indexer's (pass 2 reads
                    # pytester's texts: indexer/references.py).
                    queue.extend(
                        (other, first_plugin) for level in levels[first_plugin:] for other in level
                    )
            elif name in self.options.external_fixtures:
                self.assumed[name] += 1
            elif self.shadowed(class_levels, name):
                # Bound in scope to what discovery cannot read: maybe the
                # fixture pytest finds, not a plugin's (pytest-twisted's
                # ``@async_yield_fixture def client(self, site)``), and
                # maybe what an indirect argname is handed to.
                self.unread[name] += 1
                deps.append(f"fixture:{name}")
            elif self.options.well_known_fixtures and name in WELL_KNOWN_PLUGIN_FIXTURES:
                self.assumed[name] += 1
            elif name in uncertain:
                continue  # maybe indirect, and no fixture: a parameter
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


# Class decorators that give a class an ``__init__`` (unless ``init=False``).
_INIT_DECORATORS = frozenset(
    {
        ("dataclass",),
        ("dataclasses", "dataclass"),
        ("attr", "s"),
        ("attr", "attrs"),
        ("attr", "define"),
        ("attr", "frozen"),
        ("attr", "mutable"),
        ("attrs", "define"),
        ("attrs", "frozen"),
        ("attrs", "mutable"),
    }
)


def _has_init(cls: ast.ClassDef) -> bool:
    """Whether the class itself gives pytest a constructor, which makes it
    refuse to collect the class (``hasinit``/``hasnew``): it defines
    ``__init__`` or ``__new__``, or a decorator generates ``__init__``
    (``@dataclass``, attrs' ``@attr.s`` and ``@define``, unless
    ``init=False``)."""
    if any(f.name in ("__init__", "__new__") for f in scope_functions(cls.body)):
        return True
    for dec in cls.decorator_list:
        parts, call = decorator_chain(dec)
        if tuple(parts) in _INIT_DECORATORS:
            init = keyword_value(call, "init")
            if not (isinstance(init, ast.Constant) and not init.value):
                return True
    return False


def _normalise_testpath(entry: str) -> str:
    tp = entry.strip()
    while tp.startswith("./"):
        tp = tp[2:]
    return tp.strip("/")


def _conftest_loaded(path: str, config: dict[str, Any], startup: set[str]) -> bool:
    """Whether pytest imports the conftest at ``path``: at startup, in a
    directory _startup_conftest_dirs names (whatever ignores say), or while
    collecting, in a directory its walk reaches."""
    directory = _parent_dir(path)
    return directory in startup or _reaches(directory, config, is_dir=True)


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


# What a binding walk finds in the module that binds the name itself.
_Found = TypeVar("_Found")


def _bound_to(
    module: str,
    attr: str,
    module_facts: Callable[[str], ModuleFacts | None],
    local: Callable[[ModuleFacts, str], _Found | None],
    outside: Callable[[str], _Found | None] = lambda _: None,
    fallback: Callable[[ModuleFacts, str], _Found | None] = lambda _f, _a: None,
    past_stars: bool = False,
    seen: set[tuple[str, str]] | None = None,
) -> _Found | None:
    """What ``module``'s namespace binds to ``attr``, following aliases,
    ``from`` imports and star imports at any depth, the last binding of a
    name winning: ``local(facts, attr)`` in a module (what it defines wins
    over any binding), else the module its binding leads to, else
    ``fallback(facts, attr)`` (what a binding the walk cannot follow holds).
    A module outside the source roots gives ``outside(module)``; a star
    import from one may bind anything, so it counts when ``outside`` gives
    something. A star import whose module offers ``attr`` but binds nothing
    ``local`` recognises ends the walk, unless ``past_stars``: then it gives
    way to an earlier binding (what a star import offers can be a superset,
    _star_exports; a walk for tests may over-collect, one for fixtures must
    not, since a fixture found where there is none hides the real one)."""
    seen = set() if seen is None else seen
    if (module, attr) in seen:
        return None
    seen.add((module, attr))
    facts = module_facts(module)
    if facts is None:
        return outside(module)
    found = local(facts, attr)
    if found is not None:
        return found
    for kind, bound, source, name in reversed(facts.bindings):
        if kind == "alias" or kind == "import":
            if bound == attr:
                target = module if kind == "alias" else source
                return _bound_to(
                    target, name, module_facts, local, outside, fallback, past_stars, seen
                )
            continue
        origin = module_facts(source)
        if origin is None:
            found = outside(source)
        elif attr in _star_exports(origin, module_facts):
            found = _bound_to(
                source, attr, module_facts, local, outside, fallback, past_stars, seen
            )
            if found is None and not past_stars:
                return None
        if found is not None:
            return found
    return fallback(facts, attr)


def _fixture_bound_to(module: str, attr: str, module_facts) -> tuple[Fixture, bool] | None:
    """The fixture ``module``'s namespace binds to ``attr``, with whether it
    names itself (_bound_to)."""
    return _bound_to(module, attr, module_facts, lambda facts, a: facts.fixture_attrs.get(a))


def _function_bound_to(module: str, attr: str, module_facts) -> str | None:
    """The function ``module``'s namespace binds to ``attr`` (_bound_to);
    None when it is not a function defined in the source roots."""

    def local(facts: ModuleFacts, name: str) -> str | None:
        if any(f.name == name for f in scope_functions(facts.parsed.tree.body)):
            return facts.parsed.member_id(name)
        return None

    return _bound_to(module, attr, module_facts, local)


def _unread_fixture(module: str, attr: str, module_facts) -> bool:
    """Whether ``module``'s namespace binds ``attr`` (_bound_to) to what may
    be a fixture discovery cannot read: a function under decorators none of
    which it recognises (_unread_decorated), or a value that may hold code
    (``unread_values``). A fixture, a plain function or class, data, and a
    name from outside the source roots are not: what an import from
    outside binds is outside code, as an installed plugin's fixture is."""

    def local(facts: ModuleFacts, name: str) -> bool | None:
        if name in facts.fixture_attrs:
            return False
        if name in facts.unread_values:  # ``client = wrap(client)`` too
            return True
        node = None
        for stmt in iter_scope_statements(facts.parsed.tree.body):
            if isinstance(stmt, DEF_NODES) and stmt.name == name:
                node = stmt  # the last definition, as at run time
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _unread_decorated(node)
        return False if node is not None else None

    return bool(_bound_to(module, attr, module_facts, local, past_stars=True))


@dataclass(frozen=True)
class _Unfollowed:
    """A name bound to a value a binding walk cannot follow: ``value`` is
    its source when it may be a test (an attribute, a subscript, a call's
    result), empty when it is data (a literal, a container, a module)."""

    value: str


def _definition_bound_to(
    module: str, attr: str, module_facts
) -> tuple[ModuleFacts, ast.AST] | str | _Unfollowed | None:
    """What ``module``'s namespace binds to ``attr`` (_bound_to): the
    defining module's facts and the ``def``/``class`` node when it is
    defined in the source roots (under ``if``/``try`` too); the module
    outside the roots it comes from (a string); an _Unfollowed value; or
    None when no binding of it is found."""

    def local(facts: ModuleFacts, name: str) -> tuple[ModuleFacts, ast.AST] | None:
        node = None
        for stmt in iter_scope_statements(facts.parsed.tree.body):
            if isinstance(stmt, DEF_NODES) and stmt.name == name:
                node = stmt  # the last definition, as at run time
        return (facts, node) if node is not None else None

    def fallback(facts: ModuleFacts, name: str) -> _Unfollowed | None:
        value = facts.opaque.get(name)
        if value is None and name in facts.parsed.submodules:
            value = ""  # ``from pkg import submodule``: a module
        return _Unfollowed(value) if value is not None else None

    return _bound_to(module, attr, module_facts, local, lambda m: m, fallback, past_stars=True)


def _link_bound_hooks(facts: ModuleFacts, module_facts) -> None:
    """Hooks and xunit setup functions the module binds without defining
    them (``from tests.common import pytest_generate_tests``, ``setup_module
    = _setup``, a star import): pytest finds them in the namespace all the
    same (_add_bound). A star import from outside the source roots may bind
    any of them: recorded in ``unknown_stars``."""
    defined = {f.name for f in scope_functions(facts.parsed.tree.body)}
    names: list[str] = []
    for kind, bound, source, _ in facts.bindings:
        if kind != "star":
            names.append(bound)
            continue
        origin = module_facts(source)
        if origin is None:
            facts.unknown_stars.append(source)
        else:
            names += _star_exports(origin, module_facts)
    for name in dict.fromkeys(names):
        if name in defined or not (name.startswith("pytest_") or name in MODULE_SETUP_FUNCTIONS):
            continue
        if name == "pytest_plugins":
            _link_bound_plugins(facts, module_facts)
            continue
        _add_bound(facts, name, _function_bound_to(facts.parsed.module, name, module_facts))


def _link_bound_plugins(facts: ModuleFacts, module_facts) -> None:
    """A ``pytest_plugins`` the module binds by import (``from
    tests.shared import pytest_plugins``, a star import): pytest reads the
    module's attribute, so the plugins it names are this module's too. One
    that cannot be followed (from outside the source roots) is unknown."""

    def local(other: ModuleFacts, _: str) -> tuple[list[str], str] | None:
        if other.declared_plugins is None:
            return None
        return other.declared_plugins, other.unreadable_plugins

    found = _bound_to(
        facts.parsed.module,
        "pytest_plugins",
        module_facts,
        local,
        outside=lambda module: ([], f"pytest_plugins imported from {module}"),
    )
    if facts.declared_plugins is not None and found is None:
        return  # its own statements, read already
    plugins, unreadable = found or ([], "pytest_plugins bound to a value discovery cannot read")
    facts.plugins = list(dict.fromkeys([*facts.plugins, *plugins]))
    facts.unreadable_plugins = facts.unreadable_plugins or unreadable


def _link_fixtures(
    facts: ModuleFacts, module_facts, requested: set[str], fixture_reach: set[str]
) -> None:
    """Register the fixtures this module binds to names of its own: pytest
    finds a fixture under every name bound to it in the module's namespace
    (``box2 = box``, ``from pkg.conftest import engine as motor``, ``auto =
    helpers.fx.auto``, a star import at any depth), except that a fixture with
    an explicit ``name=`` is found under that name only. A binding is
    followed when its name is in ``requested``, or when it comes from a
    module in ``fixture_reach`` (one that may define an autouse fixture or
    one with an explicit ``name=``, or imports one that does,
    _fixture_reach): an autouse fixture applies though nobody asks for it by
    name, and one named explicitly is asked for under a name the binding
    does not have. Any other cannot be asked for, and following it would
    parse every module a test imports from."""
    local = {bound: (kind, source, name) for kind, bound, source, name in facts.bindings}

    def may_bind_fixture(kind: str, source: str, name: str, depth: int = 0) -> bool:
        if kind in ("import", "star"):
            return source in fixture_reach
        if name in facts.fixture_attrs:  # an alias of a fixture defined here
            return True
        if any(k == "star" and s in fixture_reach for k, _, s, _ in facts.bindings):
            return True
        previous = local.get(name)
        return previous is not None and depth < 8 and may_bind_fixture(*previous, depth + 1)

    for kind, bound, source, name in facts.bindings:
        follow = may_bind_fixture(kind, source, name)
        if kind == "star":
            origin = module_facts(source) if follow or requested else None
            attrs = (
                [n for n in _star_exports(origin, module_facts) if follow or n in requested]
                if origin is not None
                else []
            )
        else:
            attrs = [bound] if follow or bound in requested else []
        for attr in attrs:
            if attr in facts.fixture_attrs:
                continue
            found = _fixture_bound_to(facts.parsed.module, attr, module_facts)
            if found is None:
                continue
            fixture, names_itself = found
            facts.fixture_attrs[attr] = found
            registered = fixture.name if names_itself else attr
            facts.fixtures.setdefault(registered, replace(fixture, name=registered))


def _fixture_reach(snapshot: Snapshot, index: SourceIndex) -> set[str]:
    """Modules through which a name can be bound to a fixture pytest finds
    under a name no test writes: an autouse fixture, which nobody requests,
    or one with an explicit ``name=``, requested by that name but bound
    under another (pydantic's ``from .test_types_typeddict import
    fixture_typed_dict``, registered as ``TypedDict``). Those whose source
    says ``fixture`` and ``autouse`` or ``name`` (every such fixture is
    declared with those words), and every module importing one of them,
    directly or not (a binding chain ``from a import x`` -> ``from b import
    x`` passes only through modules that import the next one).
    _link_fixtures follows a binding whose name no test requests only from
    these, so a test suite whose modules import half the package does not
    parse it all."""

    def marks(source: bytes) -> bool:
        return b"fixture" in source and (b"autouse" in source or b"name" in source)

    marked = {
        module
        for module in index.modules
        if (symbol := index.symbols.get(module)) is not None
        and marks(snapshot.files.get(symbol.path, b""))
    }
    if not marked:
        return set()

    def module_of(symbol_id: str) -> str:
        symbol = index.symbols.get(symbol_id)
        return symbol.module if symbol is not None else symbol_id

    importers: dict[str, set[str]] = {}
    for edge in index.edges:
        if edge.kind in (EdgeKind.IMPORTS, EdgeKind.IMPORTS_NAME):
            importers.setdefault(module_of(edge.target), set()).add(module_of(edge.source))
    reach = set(marked)
    queue = list(marked)
    while queue:
        for importer in importers.get(queue.pop(), ()):
            if importer not in reach:
                reach.add(importer)
                queue.append(importer)
    return reach


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
        base = facts.parsed.import_source(stmt)
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


# A ``usefixtures`` mark whose arguments are unknown: what a decorator that
# may apply one with any argument stands for (_Scopes.expander).
UNKNOWN_USEFIXTURES: ast.expr = ast.parse("pytest.mark.usefixtures(*_)", mode="eval").body


def _called_mark(mark: ast.expr, call: ast.Call) -> ast.expr:
    """``@use("db")`` with ``use`` storing ``mark``: a mark not yet called
    (``pytest.mark.usefixtures``) is called with the arguments; a called one
    gets them added, as calling a mark decorator with arguments does."""
    parts, inner = decorator_chain(mark)
    if not (len(parts) >= 2 and parts[-2] == "mark"):
        return mark
    func = inner.func if inner is not None else mark
    args = [*(inner.args if inner is not None else []), *call.args]
    keywords = [*(inner.keywords if inner is not None else []), *call.keywords]
    return ast.Call(func=func, args=args, keywords=keywords)


def _inert_docstring(value: ast.expr) -> bool:
    """Whether a value stored as a docstring holds no example discovery
    cannot see: a literal without ``>>>``, None, or another object's
    docstring (``wrapper.__doc__ = func.__doc__``, whose examples are read
    where that docstring is written)."""
    if isinstance(value, ast.Constant):
        return not (isinstance(value.value, str) and ">>>" in value.value)
    return isinstance(value, ast.Attribute) and value.attr == "__doc__"


def _writes_docstring(node: ast.AST) -> bool:
    """Whether a function or class (a decorator) may set a docstring
    discovery cannot read: its code assigns ``__doc__`` (``fn.__doc__ =
    text``, ``+=``, ``setattr(fn, "__doc__", text)``) anything but an inert
    value (_inert_docstring), as pandas' ``@doc`` and ``@Appender`` do.
    What it calls elsewhere is not followed."""
    for inner in ast.walk(node):
        if isinstance(inner, (ast.Assign, ast.AugAssign)):
            targets = inner.targets if isinstance(inner, ast.Assign) else [inner.target]
            if any(isinstance(t, ast.Attribute) and t.attr == "__doc__" for t in targets):
                appended = isinstance(inner, ast.AugAssign)
                if not _inert_docstring(inner.value) or (
                    appended and not isinstance(inner.value, ast.Constant)
                ):
                    return True
        elif (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "setattr"
            and len(inner.args) == 3
            and isinstance(inner.args[1], ast.Constant)
            and inner.args[1].value == "__doc__"
            and not _inert_docstring(inner.args[2])
        ):
            return True
    return False


def _applies_marks(node: ast.AST) -> bool:
    """Whether a function or class may apply a ``usefixtures`` mark: its
    code mentions ``usefixtures`` (``pytest.mark.usefixtures(name)``,
    ``getattr(pytest.mark, "usefixtures")``). What it calls elsewhere is
    not followed."""
    return any(
        (isinstance(n, ast.Attribute) and n.attr == "usefixtures")
        or (isinstance(n, ast.Constant) and n.value == "usefixtures")
        for n in ast.walk(node)
    )


class _Scope(NamedTuple):
    """A module's scope for base-class and stored-mark lookups."""

    parsed: ParsedModule
    classes: dict[str, ast.ClassDef]  # defined there (under ``if``/``try`` too)
    imported: dict[str, tuple[str, str]]  # imported class name -> (module, name)
    prefixes: dict[str, str]  # alias -> the module it names (``alias.Class``)


class _Scopes:
    """Module scopes and the marks modules store in variables, built on
    demand and shared by every test module. A value reached through an
    import cycle depends on where the walk entered the cycle (the module
    being built counts as having none), and so does one built from such a
    value: those are kept for the current test module only (``begin``), as
    they would be found walking that module alone."""

    def __init__(self, module_facts: Callable[[str], ModuleFacts | None]) -> None:
        self.module_facts = module_facts
        # Base class id -> the module defining it, whose names its
        # decorators use.
        self.defined_in: dict[str, str] = {}
        self._scopes: dict[str, _Scope | None] = {}
        self._marks: dict[tuple[str, str], list[ast.expr]] = {}
        self._local_scopes: dict[str, _Scope | None] = {}
        self._local_marks: dict[tuple[str, str], list[ast.expr]] = {}
        self._building: list[tuple[str, str]] = []
        self._tainted: set[tuple[str, str]] = set()
        self._applies: dict[tuple[str, str, str], bool] = {}

    def begin(self) -> None:
        """Start a test module: forget what depended on the last one's walk."""
        self._local_scopes.clear()
        self._local_marks.clear()
        self._tainted.clear()

    def _cached(self, key, shared, local, compute, in_cycle):
        if key in shared:
            return shared[key]
        if key in local or key in self._building:
            # A value that depends on the walk taints whatever is being built.
            self._tainted.update(self._building)
            return local[key] if key in local else in_cycle
        self._building.append(key)
        try:
            value = compute()
        finally:
            self._building.pop()
        (local if key in self._tainted else shared)[key] = value
        return value

    def scope_for(self, module: str) -> _Scope | None:
        """None for a module outside the source roots, or one whose scope is
        being built (an import cycle)."""
        return self._cached(
            ("scope", module),
            self._scopes,
            self._local_scopes,
            lambda: self._build_scope(module),
            None,
        )

    def _build_scope(self, module: str) -> _Scope | None:
        facts = self.module_facts(module)
        if facts is None:
            return None
        prefixes: dict[str, str] = {}
        classes = {c.name: c for c in scope_classes(facts.parsed.tree.body)}
        imported: dict[str, tuple[str, str]] = {}
        for stmt in iter_scope_statements(facts.parsed.tree.body):
            if isinstance(stmt, ast.Import):
                # ``import tests.queues as t``: ``t.LifoDiskQueueTest`` is that
                # module's class, recorded under the alias as a module prefix.
                for alias in stmt.names:
                    prefixes[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
                continue
            if not isinstance(stmt, ast.ImportFrom):
                continue
            src = facts.parsed.import_source(stmt)
            for alias in stmt.names:
                if alias.name == "*":
                    sub_scope = self.scope_for(src)
                    for name in sub_scope.classes if sub_scope else ():
                        imported.setdefault(name, (src, name))
                else:
                    imported[alias.asname or alias.name] = (src, alias.name)
                    prefixes.setdefault(alias.asname or alias.name, f"{src}.{alias.name}")
        return _Scope(facts.parsed, classes, imported, prefixes)

    def stored_mark(self, module: str, name: str) -> list[ast.expr]:
        """The mark expressions ``module`` binds to ``name`` at module level
        (``skip_pyarrow = pytest.mark.usefixtures("pyarrow_skip")``, or a
        list of marks however it is built), following aliases and ``from``
        imports; empty when it binds none that can be seen. Only names used
        as marks are followed, so no imported library is parsed for this."""
        return self._cached(
            ("mark", module, name),
            self._marks,
            self._local_marks,
            lambda: self._build_mark(module, name),
            [],
        )

    def _build_mark(self, module: str, name: str) -> list[ast.expr]:
        scope = self.scope_for(module)
        if scope is None:
            return []
        values = _scope_values(scope.parsed.tree.body, name)
        if values:
            # Every binding counts: which one is live is not tracked.
            expand = self.expander(module)
            return [e for value in values for x in _mark_entries(value) for e in expand(x) or [x]]
        if name in scope.imported:
            return self.stored_mark(*scope.imported[name])
        return []

    def expander(self, module: str) -> MarkExpander:
        """Resolves ``name`` and ``alias.name`` mark entries in ``module``; a
        call of a stored mark (``use = pytest.mark.usefixtures`` then
        ``@use("db")``) becomes that mark's call; and a decorator or entry
        that names, or calls, a function or class in the source roots that
        mentions ``usefixtures`` (``@needs_db``, ``@uses("db")``,
        ``pytestmark = marks()``: _applies_marks) may apply it with any
        argument, so it expands to an unknown ``usefixtures``
        (UNKNOWN_USEFIXTURES)."""

        def stored(expr: ast.expr) -> list[ast.expr] | None:
            if isinstance(expr, ast.Name):
                return self.stored_mark(module, expr.id)
            parts, call = decorator_chain(expr)
            if call is None and len(parts) == 2:
                scope = self.scope_for(module)
                target = scope.prefixes.get(parts[0]) if scope is not None else None
                if target is not None:
                    return self.stored_mark(target, parts[1])
            return None

        def expand(expr: ast.expr) -> list[ast.expr] | None:
            parts, call = decorator_chain(expr)
            if len(parts) >= 2 and parts[-2] == "mark":
                return None  # a mark as written
            if call is None:
                found = stored(expr)
                if found:
                    return found
                return [UNKNOWN_USEFIXTURES] if self.applies_marks(module, parts) else None
            if isinstance(call.func, (ast.Name, ast.Attribute)):
                marks = stored(call.func)
                if marks:
                    return [_called_mark(mark, call) for mark in marks]
                if self.applies_marks(module, parts):
                    return [UNKNOWN_USEFIXTURES]
            return None

        return expand

    def applies_marks(self, module: str, parts: list[str]) -> bool:
        """Whether the dotted name ``parts`` names, in ``module``, a
        function or class in the source roots whose code mentions
        ``usefixtures`` (_applies_marks); cached."""
        return self._decides(module, parts, "marks", _applies_marks)

    def writes_docstring(self, module: str, parts: list[str]) -> bool:
        """Whether the dotted name ``parts`` names, in ``module``, a
        function or class in the source roots that may set a docstring
        discovery cannot read (_writes_docstring); cached."""
        return self._decides(module, parts, "doc", _writes_docstring)

    def _decides(
        self, module: str, parts: list[str], what: str, decide: Callable[[ast.AST], bool]
    ) -> bool:
        if not parts:
            return False
        key = (module, ".".join(parts), what)
        if key not in self._applies:
            found: object = None
            if len(parts) == 1:
                found = _definition_bound_to(module, parts[0], self.module_facts)
            else:
                scope = self.scope_for(module)
                if scope is not None and parts[0] in scope.prefixes:
                    found = _definition_bound_to(
                        _base_source(parts, scope), parts[-1], self.module_facts
                    )
            self._applies[key] = isinstance(found, tuple) and decide(found[1])
        return self._applies[key]

    def class_in(self, module: str, name: str) -> tuple[ast.ClassDef, str, _Scope] | None:
        """The class ``name`` names in ``module``: defined there, or imported
        there from a module in the source roots (pandas's
        ``tests/extension/base/__init__.py`` re-exports the classes of its
        submodules), followed to the module that defines it."""
        visited: set[tuple[str, str]] = set()
        scope = self.scope_for(module)
        while scope is not None and (module, name) not in visited:
            visited.add((module, name))
            if name in scope.classes:
                return scope.classes[name], scope.parsed.member_id(name), scope
            if name not in scope.imported:
                return None
            module, name = scope.imported[name]
            scope = self.scope_for(module)
        return None


def _framework_base(parts: list[str], scope: _Scope) -> bool:
    """Whether a base that resolves to nothing in scope is a test framework's
    ``TestCase`` (imported from one of TESTCASE_FRAMEWORKS), not a class
    discovery merely cannot see (``SharedTestCase = make_base()``)."""
    if len(parts) > 1:
        source = _base_source(parts, scope)
    elif parts[-1] in scope.imported:
        source = scope.imported[parts[-1]][0]
    else:
        return False
    return source.split(".")[0] in TESTCASE_FRAMEWORKS


def _base_source(parts: list[str], scope: _Scope) -> str:
    """The module a dotted base (``t.LifoDiskQueueTest``,
    ``pkg.mod.Base``) is looked up in: the prefix through the module an
    import binds it to, else as written."""
    head = ".".join(parts[:-1])
    if parts[0] not in scope.prefixes:
        return head
    source = scope.prefixes[parts[0]]
    return f"{source}.{'.'.join(parts[1:-1])}" if len(parts) > 2 else source


def _is_pytest_config(path: str, config_files: dict[str, bytes]) -> bool:
    """Whether pytest takes the file at ``path`` (one of PYTEST_CONFIGS by
    name) as its configuration file: ``pytest.ini``, ``.pytest.ini``,
    ``pytest.toml`` and ``.pytest.toml`` always; ``pyproject.toml`` with a
    ``[tool.pytest]`` table, ``tox.ini`` with ``[pytest]``, ``setup.cfg``
    with ``[tool:pytest]``. One whose content the snapshot did not read
    (deeper than the snapshot lists nested configuration) is taken to be
    one."""
    name = path.rpartition("/")[2]
    if name in ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml"):
        return True
    raw = config_files.get(path)
    if raw is None:
        return True
    if name == "pyproject.toml":
        return _toml_table(_parse_toml(raw)[0], "tool", "pytest") is not None
    section = "pytest" if name == "tox.ini" else "tool:pytest"
    return _parse_ini_section(raw, section)[0] is not None


def _parents(directory: str) -> list[str]:
    """``directory`` and every directory above it, the repository (``""``)
    last."""
    found = []
    while True:
        found.append(directory)
        if not directory:
            return found
        directory = directory.rpartition("/")[0]


def pytest_rootdir(
    tokens: tuple[str, ...],
    arg_paths: list[str],
    present: set[str],
    config_files: dict[str, bytes],
) -> tuple[str, str | None]:
    """(rootdir, configuration file) as pytest determines them
    (``_pytest.config.findpaths.determine_setup``), repository-relative
    (``""`` is the repository, which the run starts in): ``--rootdir``
    wins; ``-c``/``--config-file`` names the file and its directory;
    otherwise the common ancestor of the paths among the arguments (the
    repository when there are none) and its parents are searched for a
    configuration file (_is_pytest_config, in PYTEST_CONFIGS' order per
    directory), then for a ``setup.py``, then each argument's own
    directory and parents; a ``pyproject.toml`` without a pytest table
    found on the way is the fallback, and failing all that the rootdir is
    the repository. ``present`` is every path the snapshot lists."""
    rootdir_arg = (_option_values(tokens, "--rootdir") or [None])[-1]
    inifile = (_option_values(tokens, "-c") + _option_values(tokens, "--config-file") or [None])[-1]
    rootdir: str | None = None
    configfile: str | None = None
    directories = []
    for path in arg_paths:
        path = _normalise_testpath(path)
        if path in ("", "."):
            directories.append("")
        elif path in present:  # a file: its directory
            directories.append(path.rpartition("/")[0])
        else:
            directories.append(path)
    directories = list(dict.fromkeys(directories))

    def locate(starts: list[str]) -> tuple[str | None, str | None]:
        bare_pyproject: str | None = None
        for start in starts:
            for base in _parents(start):
                for name in PYTEST_CONFIGS:
                    candidate = f"{base}/{name}" if base else name
                    if candidate not in present:
                        continue
                    if _is_pytest_config(candidate, config_files):
                        return base, candidate
                    if name == "pyproject.toml" and bare_pyproject is None:
                        bare_pyproject = candidate
        if bare_pyproject is not None:
            return bare_pyproject.rpartition("/")[0], bare_pyproject
        return None, None

    if inifile is not None:
        configfile = _normalise_testpath(inifile.replace("\\", "/"))
        if rootdir_arg is None:
            rootdir = configfile.rpartition("/")[0]
    else:
        ancestor = _common_ancestor(directories) if directories else ""
        rootdir, configfile = locate([ancestor])
        if rootdir is None and rootdir_arg is None:
            for base in _parents(ancestor):
                if (f"{base}/setup.py" if base else "setup.py") in present:
                    rootdir = base
                    break
            else:
                if directories != [ancestor]:
                    rootdir, configfile = locate(directories)
                if rootdir is None:
                    rootdir = ""  # the common ancestor of the run's directory and ``ancestor``
    if rootdir_arg is not None:
        rootdir = _normalise_testpath(rootdir_arg.replace("\\", "/"))
        rootdir = "" if rootdir == "." else rootdir
    return rootdir or "", configfile


def _common_ancestor(directories: list[str]) -> str:
    """The deepest directory holding every one of ``directories``."""
    parts = [d.split("/") if d else [] for d in directories]
    common: list[str] = []
    for segment in zip(*parts, strict=False):
        if all(s == segment[0] for s in segment):
            common.append(segment[0])
        else:
            break
    return "/".join(common)


def _runner_arguments(
    config: dict[str, Any],
    options: DiscoveryOptions,
    every_path: set[str],
    result: DiscoveryResult,
    config_files: dict[str, bytes] | None = None,
) -> tuple[tuple[str, ...], list[str], list[str]]:
    """Every argument pytest reads, in its order (the configured addopts,
    PYTEST_ADDOPTS, the command's own, then those after ``--``), the paths
    among them pytest collects from, and the tokens that may name a path but
    cannot be told apart (_argument_paths). What discovery cannot model in
    the configuration or the arguments is reported; what the run reads
    besides the configuration file goes into ``config["run_arguments"]``,
    pytest's rootdir and configuration file (pytest_rootdir) into
    ``config["rootdir"]`` and ``config["configfile"]``, and the
    ``--junit-prefix`` into ``config["junit_prefix"]``: ``check`` maps JUnit
    names back to targets through them. A configuration file below the
    repository root is one discovery did not read (it reads the root's),
    so what it sets is unknown: reported (``unmodelled_runner_option``)."""
    if "error" in config:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unparsable_config",
                f"the pytest configuration could not be read ({config['error']}), so what it "
                "sets (testpaths, python_files, addopts, ...) is not known; pytest's defaults "
                "are used, and tests they do not collect are not targets",
            )
        )
    for plugin in config["collecting_plugins"]:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "plugin_collects_files",
                f"the configuration turns on {plugin}: what it collects is not a target",
            )
        )
    if options.command_problem:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unmodelled_runner_option",
                f"{options.command_problem}, so which arguments it passes to pytest is not "
                "known; what they collect is not modelled",
            )
        )
    # Each argument with where it was written, for the notes.
    sourced = [
        *((t, f"{config['source']}: addopts") for t in config["ini_addopts"]),
        *((t, "PYTEST_ADDOPTS") for t in options.env_addopts),
        *((t, "the command") for t in options.command_args),
        *((t, "the run") for t in options.runner_args),
    ]
    where: dict[str, str] = {}
    for token, origin in sourced:
        where.setdefault(token, origin)
    tokens = tuple(token for token, _ in sourced)
    for token in tokens:
        if token.split("=", 1)[0] in UNMODELLED_COLLECTION_OPTIONS:
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "unmodelled_runner_option",
                    f"{where[token]} passes {token!r} to pytest, which changes what it collects "
                    "in a way discovery does not model",
                )
            )
    arg_paths, ambiguous, unknown = _argument_paths(tokens, every_path)
    # Where pytest's rootdir is: from the paths it was given (a node id's
    # file among them), the configuration files present and the arguments.
    present = every_path | set(config_files or ())
    given = [*arg_paths, *(a.split("::", 1)[0] for a in ambiguous if "::" in a)]
    rootdir, configfile = pytest_rootdir(tokens, given, present, config_files or {})
    config["rootdir"] = rootdir
    config["configfile"] = configfile
    prefixes = _option_values(tokens, "--junit-prefix")
    config["junit_prefix"] = prefixes[-1] if prefixes else ""
    if (
        configfile is not None
        and "/" in configfile
        and _is_pytest_config(configfile, config_files or {})
        and not (_option_values(tokens, "-c") or _option_values(tokens, "--config-file"))
    ):
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unmodelled_runner_option",
                f"pytest's configuration file is {configfile} (its rootdir {rootdir!r}), below "
                "the repository root; discovery reads only the configuration at the root, so "
                "what it sets (testpaths, python_files, addopts, ...) is not known",
            )
        )
    if unknown:
        # A path the tree does not hold as written (absolute, another
        # spelling): what it collects is unknown, so ``testpaths`` stays.
        arg_paths = []
    for arg in [*ambiguous, *unknown]:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unmodelled_runner_option",
                f"{where[arg]} passes {arg!r} to pytest: a node id, a path after an option that "
                "may take it as its value, or a path discovery cannot match in the tree; which "
                "tests that collects is not modelled",
            )
        )
    config["run_arguments"] = {
        name: list(values)
        for name, values in (
            ("PYTEST_ADDOPTS", options.env_addopts),
            ("PYTEST_PLUGINS", options.env_plugins),
            ("command", options.command_args),
            ("after --", options.runner_args),
        )
        if values
    }
    return tokens, arg_paths, ambiguous


class _PluginLoader:
    """The plugin modules a session loads, and those they declare in turn,
    each linked as it is found: pytest registers a module plugin, then its
    own ``pytest_plugins`` (poetry's conftest loads a package whose
    ``__init__`` lists the modules holding its autouse fixtures)."""

    def __init__(
        self,
        snapshot: Snapshot,
        index: SourceIndex,
        module_facts: Callable[[str], ModuleFacts | None],
        link: Callable[[ModuleFacts], ModuleFacts],
        result: DiscoveryResult,
    ) -> None:
        self.snapshot = snapshot
        self.index = index
        self.module_facts = module_facts
        self.link = link
        self.result = result
        # What the session loads that cannot be read: a plugin in the
        # repository but outside the source roots (``plugin:<module>``), a
        # ``pytest_plugins`` that cannot be read. It loads for the whole
        # session, so every test depends on each as an unknown, as on a
        # conftest outside the roots.
        self.unknown: list[str] = []
        # The plugin modules the session loads by name (not the submodules
        # they re-export from, whose import-time code reaches them through
        # imports).
        self.declared: list[str] = []

    @property
    def modules(self) -> list[str]:
        """The plugin modules, and the unknowns: dependencies of every test."""
        return sorted(set(self.declared)) + self.unknown

    def _file(self, name: str) -> str | None:
        stem = name.replace(".", "/")
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            for path in sorted(self.snapshot.python_paths):
                if path == candidate or path.endswith("/" + candidate):
                    return path
        return None

    def load(
        self,
        declared: list[str],
        where: str,
        kind: str = "pytest_plugins",
        seen: set[str] | None = None,
    ) -> list[ModuleFacts]:
        """The plugin modules ``declared`` loads, and those they declare."""
        seen = set() if seen is None else seen
        found: list[ModuleFacts] = []
        for name in declared:
            # pytest's own plugins are builtin unless this repository *is*
            # pytest and ships them under _pytest.
            if name not in self.index.modules and f"_pytest.{name}" in self.index.modules:
                name = f"_pytest.{name}"
            if name.split(".")[0] in BUILTIN_PLUGINS and name not in self.index.modules:
                continue
            facts = self.module_facts(name)
            if facts is None:
                path = self._file(name)
                if path is not None:
                    if f"plugin:{name}" not in self.unknown:
                        self.unknown.append(f"plugin:{name}")
                    held = (
                        "diffcone.toml's [index] exclude keeps out of the analysis"
                        if _excluded(path, self.snapshot)
                        else "the source roots do not hold under that name"
                    )
                    remedy = (
                        "narrow the exclude pattern that matches it"
                        if _excluded(path, self.snapshot)
                        else "add a source root that names it"
                    )
                    self.result.notes.append(
                        DiscoveryNote(
                            RUNNER,
                            "plugin_outside_roots",
                            f"{where}: {kind} entry {name!r} is {path}, which {held}, so what "
                            f"it defines is unknown and every test is always selected; {remedy}",
                            path,
                        )
                    )
                    continue
                self.result.notes.append(
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
            self.declared.append(facts.parsed.module)
            found.append(self.link(facts))
            found.extend(self.link(sub) for sub in _reexported_facts(facts, self.module_facts))
            found.extend(self.declared_by(facts, seen))
        return found

    def declared_by(self, facts: ModuleFacts, seen: set[str]) -> list[ModuleFacts]:
        """The plugins a conftest, test module or plugin module declares. A
        ``pytest_plugins`` that cannot be read loads what is unknown: an
        unknown dependency of every test (``plugin:<path>:pytest_plugins``),
        reported."""
        path = facts.parsed.path
        if facts.unreadable_plugins and f"plugin:{path}:pytest_plugins" not in self.unknown:
            self.unknown.append(f"plugin:{path}:pytest_plugins")
            self.result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "unreadable_plugins",
                    f"{path}: pytest_plugins holds {facts.unreadable_plugins}, which discovery "
                    "cannot read, so which plugins it loads is unknown and every test is always "
                    "selected; write the list out as strings",
                    path,
                )
            )
        return self.load(facts.plugins, path, seen=seen) if facts.plugins else []


def discover_pytest(
    snapshot: Snapshot, index: SourceIndex, options: DiscoveryOptions
) -> DiscoveryResult:
    result = DiscoveryResult(runner=RUNNER)
    config = read_pytest_config(snapshot, options.pytest_args)
    every_path = {*snapshot.python_paths, *snapshot.files, *snapshot.other_files}
    directories = _directories(every_path)
    tokens, arg_paths, ambiguous = _runner_arguments(
        config, options, every_path, result, snapshot.config_files
    )
    result.config = {k: (list(v) if isinstance(v, tuple) else v) for k, v in config.items()}
    python_files = tuple(config["python_files"])
    # Where pytest starts collecting: the paths among its arguments, else
    # ``testpaths`` as it expands them (entries that exist, wildcards
    # matched), else the rootdir. A file named there is collected whatever
    # ``python_files`` says, and no ignore applies to one.
    testpaths = _expand_testpaths(config["testpaths"], every_path, directories)
    initial = tuple(arg_paths) or tuple(testpaths) or (".",)
    # Startup conftests come from every one; collection only from those no
    # other contains (pytest drops a path inside another, which then gets no
    # initial path's exemptions, unless --keep-duplicates).
    anchors = initial
    if "--keep-duplicates" not in tokens:
        initial = _prefix_free(initial)
    config["initial"] = initial
    named_files = {p for p in initial if p.endswith(".py")}

    # Conftests come first in pytest's ``pytest_ignore_collect``, so one that
    # may return False collects what ignores and ``norecursedirs`` skip below
    # its directory; one outside the roots cannot be read, so it may too.
    facts_by_module: dict[str, ModuleFacts] = {}

    def module_facts(name: str) -> ModuleFacts | None:
        if name in facts_by_module:
            return facts_by_module[name]
        if name in index.modules:
            pm, _ = parse_modules(snapshot, [index.symbols[name].path])
            if pm:
                facts_by_module[name] = _collect_facts(pm[0])
                return facts_by_module[name]
        return None

    every_conftest = sorted(p for p in snapshot.files if PurePosixPath(p).name == "conftest.py")
    conftest_parsed, _ = parse_modules(snapshot, every_conftest)
    conftest_facts = {pm.path: _collect_facts(pm) for pm in conftest_parsed}
    for facts in conftest_facts.values():
        facts_by_module.setdefault(facts.parsed.module, facts)

    def unignores(facts: ModuleFacts) -> bool:
        return facts.unignores or any(
            kind == "star"
            and (origin := module_facts(source)) is not None
            and "pytest_ignore_collect" in _star_exports(origin, module_facts)
            for kind, _, source, _ in facts.bindings
        )

    config["unignore_dirs"] = tuple(
        sorted(
            {_parent_dir(p) for p, f in conftest_facts.items() if unignores(f)}
            | {
                _parent_dir(p)
                for p in snapshot.python_paths
                if PurePosixPath(p).name == "conftest.py" and p not in snapshot.files
            }
        )
    )

    test_paths = [
        p
        for p in snapshot.files
        if (any(_matches_python_file(p, pat) for pat in python_files) or p in named_files)
        and _reaches(p, config)
    ]
    for path in snapshot.python_paths:
        if (
            path not in snapshot.files
            and any(_matches_python_file(path, pat) for pat in python_files)
            and _reaches(path, config)
        ):
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "test_file_outside_roots",
                    f"{path}: pytest collects it, but {_unread_because(path, snapshot)}, so its "
                    f"tests are not targets; {_unread_remedy(path, snapshot)}",
                    path,
                )
            )
    # Only the conftests pytest loads: at startup, those of the initial
    # paths, the directories above them and their ``test*`` directories
    # (whatever ignores say; a path an option may take as its value counts
    # too), and while collecting, those of the directories its walk reaches.
    # One elsewhere (a sibling package's own test suite) is never imported.
    maybe_initial = [
        p
        for p in (_normalise_testpath(a.split("::", 1)[0].replace("\\", "/")) for a in ambiguous)
        if p in every_path or p in directories
    ]
    startup = _startup_conftest_dirs([*anchors, *maybe_initial], directories)
    conftest_paths = [p for p in every_conftest if _conftest_loaded(p, config, startup)]
    # A conftest outside the source roots is not read: its fixtures, hooks
    # and import-time code are invisible, so every test under it depends on
    # it as an unknown (selected, with this note saying why).
    outside_conftests = sorted(
        p
        for p in snapshot.python_paths
        if PurePosixPath(p).name == "conftest.py"
        and p not in snapshot.files
        and _conftest_loaded(p, config, startup)
    )

    def outside_conftests_of(path: str) -> list[str]:
        """Unknown dependencies on the conftests outside the roots that
        apply to ``path``."""
        above = {str(d) for d in PurePosixPath(path).parents}
        return [f"conftest:{c}" for c in outside_conftests if str(PurePosixPath(c).parent) in above]

    for path in outside_conftests:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "conftest_outside_roots",
                f"{path}: pytest loads it, but {_unread_because(path, snapshot)}, so what it "
                "defines is unknown and every test under it is always selected; "
                f"{_unread_remedy(path, snapshot)}",
                path,
            )
        )
    parsed, failed = parse_modules(snapshot, sorted(set(test_paths + conftest_paths)))
    for path, why in failed.items():
        # Two reasons, and they need different answers: a file that does not
        # parse (or nests too deeply to walk) is also an analysis error (the
        # plan degrades and selects everything), while one that cannot be
        # named is dropped silently -- pytest imports it by its basename, so
        # its tests are collected and are not targets (pytest-asyncio's
        # ``docs/how-to-guides``).
        detail = (
            f"{path}: {why}"
            if why
            else (
                f"{path}: cannot be named from any source root (a directory component is not a "
                "Python identifier), so pytest collects it and it is not a target; name it with "
                "a DIR=PREFIX source root"
            )
        )
        result.notes.append(DiscoveryNote(RUNNER, "unparsed_file", detail, path))
    facts_by_path: dict[str, ModuleFacts] = {}
    for pm in parsed:
        known = facts_by_module.get(pm.module)
        facts = known if known is not None and known.parsed.path == pm.path else None
        facts = facts or conftest_facts.get(pm.path) or _collect_facts(pm)
        facts_by_path[pm.path] = facts
        facts_by_module[pm.module] = facts

    # Names bound to fixtures defined elsewhere. Plugin modules are linked
    # where plugin_facts finds them, with their own requests added.
    requested = _requested_names(f.parsed.tree for f in facts_by_path.values())
    fixture_reach = _fixture_reach(snapshot, index)
    # Modules ``[index] exclude`` keeps out, for what a note says of them.
    kept_out = excluded_modules(snapshot)
    linked: set[str] = set()

    def link(facts: ModuleFacts) -> ModuleFacts:
        if facts.parsed.module not in linked:
            linked.add(facts.parsed.module)
            requested.update(_requested_names([facts.parsed.tree]))
            _link_fixtures(facts, module_facts, requested, fixture_reach)
            _link_bound_hooks(facts, module_facts)
        return facts

    for facts in list(facts_by_path.values()):
        link(facts)

    loader = _PluginLoader(snapshot, index, module_facts, link, result)
    global_plugins: list[ModuleFacts] = []
    if config["entry_point_plugins"]:
        global_plugins.extend(
            loader.load(list(config["entry_point_plugins"]), "pyproject/setup.cfg", "pytest11")
        )
    if config["addopts_plugins"]:
        global_plugins.extend(loader.load(list(config["addopts_plugins"]), "addopts", "-p"))
    if options.env_plugins:
        global_plugins.extend(
            loader.load(list(options.env_plugins), "the environment", "PYTEST_PLUGINS")
        )
    # ``pytest_plugins`` in a conftest or a test module registers the plugin
    # for the session once pytest imports the file, so its fixtures and
    # hooks reach every test, not only those beside the declaration.
    plugins_seen: set[str] = set()
    for path in sorted(conftest_paths) + sorted(test_paths):
        facts = facts_by_path.get(path)
        if facts is not None:
            global_plugins.extend(loader.declared_by(facts, plugins_seen))
    for plugin in global_plugins:
        if plugin.unignores and plugin.parsed.path not in conftest_facts:
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "plugin_collects_files",
                    f"{plugin.parsed.path}: binds pytest_ignore_collect, which may return False "
                    "and so collect what --ignore, --ignore-glob or norecursedirs would skip; "
                    "those tests are not targets",
                    plugin.parsed.path,
                )
            )
    # Plugin modules load for the whole session: their import-time code, as
    # well as their hooks, reaches every test.
    plugin_modules = loader.modules
    plugin_hooks = [h for p in global_plugins for h in p.hooks]
    # The ini option ``usefixtures``, and what the session's conftests and
    # plugin modules add to every test's requests in code (_session_requests).
    ini_usefixtures = tuple(
        dict.fromkeys(
            [
                *config["usefixtures"],
                *(
                    name
                    for path in sorted(conftest_paths)
                    if path in facts_by_path
                    for name in facts_by_path[path].session_requests
                ),
                *(name for p in global_plugins for name in p.session_requests),
            ]
        )
    )
    # Hooks of conftests off a test's path that pytest calls for the whole
    # session (``pytest_collection_modifyitems`` in ``tests/a/conftest.py``
    # sees, and may reorder, skip or mark, the items of ``tests/b``).
    plugin_hooks += [
        h
        for path in sorted(conftest_paths)
        if path in facts_by_path
        for h in facts_by_path[path].session_hooks
    ]
    # Hooks of plugin objects (``config.pluginmanager.register(Obj())``), and
    # code pytest runs while collecting that leaves process-global state
    # behind for every later test (discovery/session_state.py).
    test_modules = {facts_by_path[p].parsed.module for p in test_paths if p in facts_by_path}
    collected = [
        facts_by_path[p]
        for p in sorted(set(conftest_paths) | set(test_paths))
        if p in facts_by_path
    ]
    plugin_hooks += plugin_object_hooks(index, test_modules)
    plugin_hooks += state_writing_roots(
        index,
        [f.parsed.module for f in collected],
        [
            *(h for f in collected for h in f.hooks if h.rsplit(".", 1)[-1] in COLLECTION_HOOKS),
            *(
                s.id
                for s in index.symbols.values()
                if s.kind == SymbolKind.METHOD
                and s.name == "pytest_generate_tests"
                and s.module in test_modules
            ),
        ],
    )
    plugin_hooks = list(dict.fromkeys(plugin_hooks))

    unresolved: Counter[str] = Counter()
    assumed: Counter[str] = Counter()
    unread: Counter[str] = Counter()
    # Filled while walking every module: class symbols followed as a base,
    # and (symbol, detail) for each class pytest's rules skipped.
    used_as_base: set[str] = set()
    uncollected: list[tuple[str, str]] = []
    scopes = _Scopes(module_facts)
    flow = _RequestFlow(module_facts, scopes)
    for path in sorted(test_paths):
        facts = facts_by_path.get(path)
        if facts is None:
            continue
        conftests = _conftest_chain(path, facts_by_path)
        resolver = _Resolver(
            facts,
            conftests,
            global_plugins,
            options,
            unresolved,
            assumed,
            ini_usefixtures,
            module_facts,
            unread,
            flow,
        )
        module_deps = [facts.parsed.module]
        module_deps += outside_conftests_of(path)
        module_deps += [c.parsed.module for c in conftests]
        for c in conftests:
            module_deps += c.hooks
        module_deps += facts.hooks  # e.g. pytest_generate_tests in the test module
        module_deps += plugin_hooks  # hooks of the project's own pytest plugins
        module_deps += plugin_modules
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
            scopes,
            module_facts,
            used_as_base,
            uncollected,
            flow,
            kept_out,
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
    # modules the session loads; and what a conftest or a plugin module
    # star-imports from outside the source roots, which may bind one.
    hook_owners = {f.parsed.path: f for f in facts_by_path.values()}
    for plugin in global_plugins:
        hook_owners.setdefault(plugin.parsed.path, plugin)
    registered = set(conftest_facts) | {p.parsed.path for p in global_plugins}
    for path, facts in sorted(hook_owners.items()):
        for hook in facts.collect_hooks:
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "plugin_collects_files",
                    f"{path}: binds {hook}, which makes tests out of files or "
                    "objects by its own rules; what it collects is not a target",
                    path,
                )
            )
        for source in facts.unknown_stars if path in registered else ():
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "plugin_collects_files",
                    f"{path}: ``from {source} import *`` imports from outside the source roots "
                    "and may bind a collection hook (pytest_collect_file, "
                    "pytest_pycollect_makeitem, pytest_ignore_collect, ...) that makes tests by "
                    "its own rules; what it collects is not a target",
                    path,
                )
            )

    def doctest_deps(pm: ParsedModule, conftests: list[ModuleFacts]) -> list[str]:
        """What a doctest item runs besides its examples: pytest picks up the
        module's and the conftests' autouse fixtures for it, provides
        ``doctest_namespace`` (which an autouse fixture usually fills, as
        pandas's root conftest does), applies the ini ``usefixtures``, and
        calls the session-wide hooks. What those fixtures store into
        ``doctest_namespace`` is what the examples read their names from: each
        module in the source roots it names is a ``dynamic:`` dependency, as a
        module the examples import is (_namespace_modules)."""
        facts = facts_by_path.get(pm.path)
        if facts is None:
            known = facts_by_module.get(pm.module)
            same = known is not None and known.parsed.path == pm.path
            facts = link(known if known is not None and same else _collect_facts(pm))
        resolver = _Resolver(
            facts,
            conftests,
            global_plugins,
            options,
            unresolved,
            assumed,
            ini_usefixtures,
            module_facts,
            unread,
            flow,
        )
        reached: list[Fixture] = []
        deps = resolver.lifecycle([], ["doctest_namespace"], reached=reached)
        stored = [
            f"{DYNAMIC_DEPENDENCY}{module}"
            for fixture in reached
            for module in _namespace_modules(fixture, scopes, index)
        ]
        return plugin_hooks + plugin_modules + outside_conftests_of(pm.path) + deps + stored

    _collect_doctests(result, snapshot, index, config, facts_by_path, doctest_deps, scopes)

    for name, count in sorted(unresolved.items()):
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unresolved_fixture",
                f"fixture {name!r} requested by {count} test(s) was not found in the source "
                "roots; its users are selected conservatively (declare it external to opt out)",
            )
        )
    for name, count in sorted(unread.items()):
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unresolved_fixture",
                f"fixture {name!r} requested by {count} test(s) is bound in scope to what "
                "discovery cannot read (a decorator it does not recognise as a fixture's, or "
                "a value it cannot follow); its users are selected conservatively",
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


def _namespace_modules(fixture: Fixture, scopes: _Scopes, index: SourceIndex) -> list[str]:
    """The modules in the source roots a fixture stores into pytest's
    ``doctest_namespace`` (``doctest_namespace["np"] = np``, ``.update(pd=pd)``,
    ``.setdefault``): each name or dotted name a stored value is built from,
    resolved through the imports of the fixture's function and of its module
    (``import pkg.core``, ``from pkg import core``) to the longest module the
    index has, and the fixture's own module for a name bound there."""
    node = fixture.node
    if node is None or "doctest_namespace" not in _parameters(node):
        return []
    values: list[ast.expr] = []
    local: dict[str, str] = {}
    for inner in ast.walk(node):
        if isinstance(inner, ast.Assign):
            if any(
                isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Name)
                and t.value.id == "doctest_namespace"
                for t in inner.targets
            ):
                values.append(inner.value)
        elif isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
            func = inner.func
            if (
                isinstance(func.value, ast.Name)
                and func.value.id == "doctest_namespace"
                and func.attr in ("update", "setdefault", "__setitem__")
            ):
                values += [*inner.args, *(k.value for k in inner.keywords)]
        elif isinstance(inner, ast.Import):
            for alias in inner.names:
                head = alias.name if alias.asname else alias.name.split(".")[0]
                local[alias.asname or head] = head
        elif isinstance(inner, ast.ImportFrom) and inner.module and not inner.level:
            for alias in inner.names:
                local[alias.asname or alias.name] = f"{inner.module}.{alias.name}"
    if not values:
        return []
    scope = scopes.scope_for(fixture.module) if fixture.module else None
    prefixes = {**(scope.prefixes if scope is not None else {}), **local}
    body = scope.parsed.tree.body if scope is not None else []
    module_names = {n for n, _ in scope_assignments(body)} | {
        stmt.name for stmt in iter_scope_statements(body) if isinstance(stmt, DEF_NODES)
    }
    found: list[str] = []
    for value in values:
        # Whole dotted names only: ``pkg.core`` is not also ``pkg``.
        heads = {id(n.value) for n in ast.walk(value) if isinstance(n, ast.Attribute)}
        for inner in ast.walk(value):
            if not isinstance(inner, (ast.Name, ast.Attribute)) or id(inner) in heads:
                continue
            parts, _ = decorator_chain(inner)
            if not parts:
                continue
            if parts[0] in prefixes:
                dotted = ".".join([prefixes[parts[0]], *parts[1:]]).split(".")
                for end in range(len(dotted), 0, -1):
                    if ".".join(dotted[:end]) in index.modules:
                        found.append(".".join(dotted[:end]))
                        break
            elif parts[0] in module_names and fixture.module in index.modules:
                found.append(fixture.module)
    return list(dict.fromkeys(found))


def _collect_doctests(
    result: DiscoveryResult,
    snapshot: Snapshot,
    index: SourceIndex,
    config: dict[str, Any],
    facts_by_path: dict[str, ModuleFacts],
    fixture_deps: Callable[[ParsedModule, list[ModuleFacts]], list[str]],
    scopes: _Scopes,
) -> None:
    """Doctest targets (see the module docstring)."""
    parser = doctest.DocTestParser()
    globs = tuple(config["doctest_globs"])
    # pytest's ``_is_doctest``: a ``.txt``/``.rst`` file named as an initial
    # path is a doctest whatever the globs say; otherwise a glob matches as
    # ``fnmatch_ex`` does (the name, or the path for a glob with a ``/``).
    named = {_normalise_testpath(p) for p in config.get("initial") or ()}

    def is_doctest_file(path: str) -> bool:
        if path in named and PurePosixPath(path).suffix in (".txt", ".rst"):
            return True
        return any(_matches_python_file(path, g) for g in globs)

    for path in sorted(snapshot.other_files):
        # A file a glob matches but the snapshot does not read (only .txt,
        # .rst and .md are read) holds doctests that are not targets.
        if path not in snapshot.text_files and is_doctest_file(path) and _reaches(path, config):
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
        if not (is_doctest_file(path) and _reaches(path, config)):
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
        if _reaches(p, config) and PurePosixPath(p).name not in ("setup.py", "__main__.py")
    ]
    for path in snapshot.python_paths:
        if (
            path not in snapshot.files
            and _reaches(path, config)
            and PurePosixPath(path).name not in ("setup.py", "__main__.py", "conftest.py")
        ):
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "test_file_outside_roots",
                    f"{path}: --doctest-modules collects its docstrings, but "
                    f"{_unread_because(path, snapshot)}, so they are not targets; "
                    f"{_unread_remedy(path, snapshot)}",
                    path,
                )
            )
    parsed, failed = parse_modules(snapshot, sorted(paths))
    for path, why in failed.items():
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unparsed_file",
                f"{path}: --doctest-modules collects its docstrings, but it "
                f"{why or 'cannot be named from any source root'}, so they are not targets",
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

        def writes_docstring(decorator: ast.expr, module: str = pm.module) -> bool:
            parts, _ = decorator_chain(decorator)
            return scopes.writes_docstring(module, parts)

        docstrings, unnamed = _docstrings(pm, writes_docstring)
        unknown: list[str] = []
        for qualname, symbol, docstring, computed in docstrings:
            try:
                examples = parser.get_examples(docstring)
            except ValueError:
                examples = None  # pytest reports it as a failing item
            if examples == [] and computed:
                # Whether the docstring it gets has examples, and so whether
                # pytest collects it, is unknown.
                unknown.append(qualname or "the module")
            if examples == []:
                continue
            if not fixtures_added:
                # Once per module, and only for one with examples.
                fixtures_added = True
                base_deps += fixture_deps(pm, conftests)
            deps = [*base_deps, f"{DYNAMIC_DEPENDENCY}{pm.module}"]
            if computed:
                # The examples it runs are not all written here: always run.
                deps.append("doctest:computed")
            if examples is None:
                deps.append("doctest:unparsed")
            else:
                for module in _example_imports(examples):
                    if module is None:
                        deps.append("doctest:unparsed")
                    elif module in index.modules:
                        deps.append(f"{DYNAMIC_DEPENDENCY}{module}")
            name = module_name if not qualname else f"{module_name}.{qualname}"
            result.targets.append(
                Target(RUNNER, f"{pm.path}::{name}", symbol, tuple(sorted(set(deps))))
            )
        if unknown or unnamed:
            shown = ", ".join([*unknown[:5], *unnamed[:3]])
            more = len(unknown) + len(unnamed) - min(len(unknown), 5) - min(len(unnamed), 3)
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "unmodelled_test_binding",
                    f"{pm.path}: docstrings set when the module is imported, which discovery "
                    f"cannot read ({shown}{f', and {more} more' if more else ''}); the doctests "
                    "pytest collects from them are not targets",
                    pm.path,
                )
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


def _docstrings(
    pm: ParsedModule, writes_docstring: Callable[[ast.expr], bool] = lambda _: False
) -> tuple[list[tuple[str, str, str, bool]], list[str]]:
    """(qualified name, symbol id, docstring, whether it is computed) of
    every object doctest's finder visits: the module, its functions and
    classes, and recursively their methods and nested classes; and the
    statements that set a docstring on an object discovery cannot name.

    A docstring is the one written first in the body, unless the scope
    defining the object sets it (``f.__doc__ = text``, ``+=``, a class or
    module body's ``__doc__ = text``): a value discovery can work out is read
    as the docstring (a literal; another object's docstring, ``g.__doc__``,
    of an object defined in the same scope; and, at module level, a string
    expression _static_text evaluates, as networkx's
    ``maximum_branching.__doc__ = docstring_branching.format(...)``); any
    other value is computed at import, and so is the docstring of an object
    under a decorator ``writes_docstring`` says may set one (pandas'
    ``@doc(...)``, ``@Appender``). Computed objects are listed with their
    written docstring, empty when there is none."""
    found: dict[str, tuple[str, str, bool]] = {}
    unnamed: list[str] = []
    found[""] = (pm.module, ast.get_docstring(pm.tree, clean=False) or "", False)
    evaluated = _module_texts(pm.tree, pm.source)

    def walk(body: list[ast.stmt], prefix: str, container: str | None) -> None:
        # Definitions under ``if``/``try`` are attributes like any other.
        here: dict[str, str] = {}
        for node in iter_scope_statements(body):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            qual = f"{prefix}.{node.name}" if prefix else node.name
            symbol = f"{container}.{node.name}" if container else pm.member_id(node.name)
            doc = ast.get_docstring(node, clean=False) or ""
            computed = any(writes_docstring(d) for d in node.decorator_list)
            found[qual] = (symbol, doc, computed)
            here[node.name] = qual
            if isinstance(node, ast.ClassDef):
                walk(node.body, qual, symbol)
        for stmt in iter_scope_statements(body):
            if not isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                continue
            if stmt.value is None:
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "__doc__":
                    qual = prefix  # the class (or module) this body defines
                elif isinstance(target, ast.Attribute) and target.attr == "__doc__":
                    parts, call = decorator_chain(target.value)
                    named = bool(parts) and parts[0] in here
                    qual = ".".join([here[parts[0]], *parts[1:]]) if named else ""
                    if call is not None or not qual or qual not in found:
                        # Set on an object discovery cannot name (a loop's
                        # variable, an imported object): noted unless the
                        # text holds no example.
                        literal = isinstance(stmt.value, ast.Constant)
                        if not (literal and _inert_docstring(stmt.value)):
                            unnamed.append(_shown(stmt))
                        continue
                else:
                    continue
                symbol, doc, computed = found[qual]
                text, copied = _stored_text(stmt.value, evaluated.get(id(stmt)), here, found)
                if text is None:
                    found[qual] = (symbol, doc, True)
                    continue
                doc = doc + text if isinstance(stmt, ast.AugAssign) else text
                found[qual] = (
                    symbol,
                    doc,
                    copied or (computed and isinstance(stmt, ast.AugAssign)),
                )

    walk(pm.tree.body, "", None)
    return [(q, s, d, c) for q, (s, d, c) in found.items() if d or c], unnamed


def _stored_text(
    value: ast.expr,
    evaluated: str | None,
    here: dict[str, str],
    found: dict[str, tuple[str, str, bool]],
) -> tuple[str | None, bool]:
    """The text a docstring store puts (None when discovery cannot work it
    out), and whether that text is itself computed: a literal, what
    _module_texts evaluated for the statement, or the docstring of an object
    the same scope defines (``out_edges.__doc__ = edges.__doc__``)."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value, False
    if evaluated is not None:
        return evaluated, False
    if isinstance(value, ast.Attribute) and value.attr == "__doc__":
        parts, call = decorator_chain(value.value)
        if call is None and parts and parts[0] in here:
            other = found.get(".".join([here[parts[0]], *parts[1:]]))
            if other is not None:
                return other[1], other[2]
    return None, False


# String methods _static_text applies to a known string with known arguments.
_TEXT_METHODS = frozenset(
    {"format", "replace", "strip", "lstrip", "rstrip", "lower", "upper", "expandtabs"}
)


def _static_text(expr: ast.expr, env: dict[str, str], depth: int = 0) -> str | None:
    """The string a module-level expression evaluates to, when discovery can
    work it out without running code: literals, names ``env`` knows, ``+``,
    ``%`` with literal values, f-strings of those, and _TEXT_METHODS called
    with literal or known arguments (``doc.format(kind="maximum")``,
    ``doc.replace("node", "edge")``); None otherwise."""
    if depth > 20:
        return None

    def known(node: ast.expr) -> object:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            # No widths or tab sizes that would build a huge string.
            return node.value if abs(node.value) <= 1000 else None
        return _static_text(node, env, depth + 1)

    if isinstance(expr, ast.Constant):
        return expr.value if isinstance(expr.value, str) else None
    if isinstance(expr, ast.Name):
        return env.get(expr.id)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
        left = _static_text(expr.left, env, depth + 1)
        if left is None:
            return None
        if isinstance(expr.op, ast.Add):
            right = _static_text(expr.right, env, depth + 1)
            return None if right is None else left + right
        values = expr.right.elts if isinstance(expr.right, ast.Tuple) else [expr.right]
        args = [known(v) for v in values]
        if any(a is None for a in args) or _wide(left):
            return None
        try:
            return left % tuple(args)
        except (TypeError, ValueError, KeyError):
            return None
    if isinstance(expr, ast.JoinedStr):
        pieces: list[str] = []
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                pieces.append(part.value)
            elif (
                isinstance(part, ast.FormattedValue)
                and part.conversion == -1
                and part.format_spec is None
                and (text := _static_text(part.value, env, depth + 1)) is not None
            ):
                pieces.append(text)
            else:
                return None
        return "".join(pieces)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr in _TEXT_METHODS
    ):
        receiver = _static_text(expr.func.value, env, depth + 1)
        if receiver is None or any(isinstance(a, ast.Starred) for a in expr.args):
            return None
        args = [known(a) for a in expr.args]
        if any(a is None for a in args) or any(k.arg is None for k in expr.keywords):
            return None
        kwargs = {k.arg: known(k.value) for k in expr.keywords if k.arg is not None}
        if any(v is None for v in kwargs.values()):
            return None
        if expr.func.attr == "format" and _wide(receiver):
            return None
        try:
            result = getattr(str, expr.func.attr)(receiver, *args, **kwargs)
        except (TypeError, ValueError, KeyError, IndexError, AttributeError):
            return None
        return result if isinstance(result, str) else None
    return None


def _wide(template: str) -> bool:
    """Whether a ``%`` or ``format`` template asks for a field four digits
    wide or more (``%10000s``, ``{:>10000}``): not evaluated."""
    return re.search(r"[%:][^%:{}]*?\d{4,}", template) is not None


def _module_texts(tree: ast.Module, source: bytes) -> dict[int, str]:
    """For each top-level statement storing a docstring (``f.__doc__ =
    value``), the text its value evaluates to there (_static_text), with
    names bound to strings by earlier top-level assignments known. A name
    bound anywhere else (in a block, by an import, a definition, a loop, a
    function's ``global``) is not known."""
    if b"__doc__" not in source:
        return {}
    simple: set[int] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            simple.update(id(t) for t in targets if isinstance(t, ast.Name))
    unstable: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            if id(node) not in simple:
                unstable.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            unstable.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            unstable.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, DEF_NODES):
            unstable.add(node.name)
    env: dict[str, str] = {}
    texts: dict[int, str] = {}
    for stmt in tree.body:
        if not isinstance(stmt, (ast.Assign, ast.AugAssign)):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if any(isinstance(t, ast.Attribute) and t.attr == "__doc__" for t in targets):
            text = _static_text(stmt.value, env)
            if text is not None:
                texts[id(stmt)] = text
        for target in targets:
            if not isinstance(target, ast.Name) or target.id in unstable:
                continue
            value = _static_text(stmt.value, env)
            if isinstance(stmt, ast.AugAssign):
                previous = env.get(target.id)
                value = None if previous is None or value is None else previous + value
            if value is None:
                env.pop(target.id, None)
            else:
                env[target.id] = value
    return texts


def _example_imports(examples: list[doctest.Example]) -> list[str | None]:
    """Absolute modules the examples import; None for an example that does
    not parse (what it uses is unknown)."""
    modules: list[str | None] = []
    for example in examples:
        try:
            tree = ast.parse(example.source)
        except (SyntaxError, ValueError, RecursionError):
            modules.append(None)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules.append(node.module)
                modules += [f"{node.module}.{a.name}" for a in node.names]
    return modules


def _all_classes(body: list[ast.stmt]) -> Iterator[ast.ClassDef]:
    """Every class defined anywhere in ``body``, in functions and classes
    too: a class is a statement, so walking statements finds them all
    without walking expressions."""
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.ClassDef):
            yield node
        for name in ("body", "orelse", "finalbody", "handlers", "cases"):
            stack.extend(getattr(node, name, ()) or ())


def _shown(value: ast.expr, limit: int = 80) -> str:
    """A value's source, shortened for a note."""
    text = ast.unparse(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# Calls that return a mapping of a scope's names, whose items a statement
# can bind a test through: ``globals()[f"test_{n}"] = make(n)``.
_NAMESPACE_CALLS = {"globals", "locals", "vars"}


def _namespace_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _NAMESPACE_CALLS
        and not node.args
    )


def _dynamic_bindings(
    body: list[ast.stmt],
    functions: tuple[str, ...],
    classes: tuple[str, ...],
    origins: dict[str, str],
    in_class: bool = False,
) -> list[tuple[str, str]]:
    """Statements of a module (or, ``in_class``, a class body) that may bind
    a test under a name discovery cannot read, as (the name, empty when it
    is not known; the statement): an item stored into ``globals()``,
    ``locals()`` or ``vars()`` (or their ``update``/``setdefault``), a
    ``setattr`` whose name is not a literal or matches the naming rules, an
    attribute of another object assigned under such a name (``TestA.test_x
    = _check``), names bound by unpacking a value that may hold code
    (``test_a, test_b = make()``), and pytest-bdd's ``scenarios(...)``,
    which makes a test function per scenario of its feature files. A literal
    name the rules do not match is no test; a value that is evidently data
    (_may_bind_test) neither."""
    found: list[tuple[str, str]] = []

    def test_name(name: str) -> bool:
        return _matches(functions, name) or _matches(classes, name)

    def named(key: ast.expr | None) -> str | None:
        """The name a key binds when it may be a test's ("" when it is not
        a literal), else None."""
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value if test_name(key.value) else None
        return ""

    for stmt in iter_scope_statements(body):
        if isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and stmt.value is not None:
            if not _may_bind_test(stmt.value, origins):
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                unpacked = isinstance(target, (ast.Tuple, ast.List))
                for elt in target.elts if unpacked else [target]:  # type: ignore[union-attr]
                    if isinstance(elt, ast.Starred):
                        elt = elt.value
                    name = None
                    if isinstance(elt, ast.Subscript) and _namespace_call(elt.value):
                        name = named(elt.slice)
                    elif (
                        not in_class
                        and isinstance(elt, ast.Attribute)
                        and elt.attr != "__test__"
                        and test_name(elt.attr)
                    ):
                        name = f"{ast.unparse(elt.value)}::{elt.attr}"
                    elif unpacked and isinstance(elt, ast.Name) and test_name(elt.id):
                        name = elt.id
                    if name is not None:
                        found.append((name, _shown(stmt)))
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            continue
        call = stmt.value
        func = call.func
        names: list[str | None] = []
        if isinstance(func, ast.Attribute) and _namespace_call(func.value):
            if func.attr == "update":
                dicts = [a for a in call.args if isinstance(a, ast.Dict)]
                names = [named(ast.Constant(k.arg) if k.arg else None) for k in call.keywords]
                names += [named(k) for d in dicts for k in d.keys]
                names += [""] * (len(call.args) - len(dicts))
            elif func.attr in ("setdefault", "__setitem__"):
                names = [named(call.args[0] if call.args else None)]
        elif isinstance(func, ast.Name) and func.id == "setattr" and len(call.args) == 3:
            if _may_bind_test(call.args[2], origins):
                name = named(call.args[1])
                owner = ast.unparse(call.args[0])
                names = [None if name is None else f"{owner}::{name}" if name else ""]
        elif not in_class and (decorator_chain(func)[0] or [""])[-1] == "scenarios":
            names = [""]
        for name in dict.fromkeys(n for n in names if n is not None):
            found.append((name, _shown(stmt)))
    return found


def _collect_module_tests(
    result: DiscoveryResult,
    facts: ModuleFacts,
    resolver: _Resolver,
    config: dict[str, Any],
    module_deps: list[str],
    index: SourceIndex,
    scopes: _Scopes,
    module_facts: Callable[[str], ModuleFacts | None],
    used_as_base: set[str],
    uncollected: list[tuple[str, str]],
    flow: _RequestFlow,
    kept_out: dict[str, str],
) -> None:
    parsed = facts.parsed
    body = parsed.tree.body
    origins = _import_origins(body)
    functions = tuple(config["python_functions"])
    classes = tuple(config["python_classes"])
    scopes.begin()
    # What the module defines, under ``if``/``try`` too, as pytest reads it
    # from the module's namespace; and what it defines at top level, where a
    # definition is what the name is bound to (under ``if``/``try`` an
    # import or assignment may bind it instead).
    module_classes = {c.name: c for c in scope_classes(body)}
    module_funcs = {f.name: f for f in scope_functions(body)}
    defined = {n.name for n in body if isinstance(n, DEF_NODES)}
    # Names used as a base anywhere in the module: such a class contributes
    # its methods through its subclasses, so it is not an uncollected class.
    base_names = {
        (decorator_chain(base)[0] or [""])[-1] for node in _all_classes(body) for base in node.bases
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
        deps = module_deps + extra
        deps += resolver.lifecycle(class_levels, requests, marks.supplied, marks.uncertain)
        result.targets.append(Target(RUNNER, nodeid, entry, tuple(sorted(set(deps)))))

    found_scope = scopes.scope_for(parsed.module)
    own_scope = _Scope(
        parsed,
        module_classes,
        found_scope.imported if found_scope else {},
        found_scope.prefixes if found_scope else {},
    )
    own_marks = scopes.expander(parsed.module)
    module_marks = _marks_from_pytestmark(body, own_marks)
    if not _scope_values(body, "pytestmark") and "pytestmark" in own_scope.imported:
        # ``from tests.marks import pytestmark``: pytest reads the attribute.
        module_marks = module_marks + _marks_from_expressions(
            scopes.stored_mark(*own_scope.imported["pytestmark"])
        )

    def mro(
        cls: ast.ClassDef, nodeid: str, quiet: bool = False, scope: _Scope | None = None
    ) -> list[tuple[ast.ClassDef, str]]:
        """Base classes, nearest first, with their symbol ids. A base defined
        in another module in the source roots is followed too (networkx's
        ``TestDiGraph(BaseGraphTester)``), and its own bases resolve in the
        module that defines it, not in this one."""
        chain: list[tuple[ast.ClassDef, str]] = []
        seen: set[tuple[str, str]] = set()
        start = scope or own_scope
        queue = deque((base, start) for base in cls.bases)
        while queue:
            base, owner = queue.popleft()
            parts, _ = decorator_chain(base)
            name = parts[-1] if parts else ""
            if name in ("object", "") or (owner.parsed.module, name) in seen:
                continue
            seen.add((owner.parsed.module, name))
            found = None
            if name in owner.classes and not (
                owner.parsed.module == start.parsed.module and name == cls.name
            ):
                found = (owner.classes[name], owner.parsed.member_id(name), owner)
            elif name in owner.imported:
                found = scopes.class_in(*owner.imported[name])
            elif len(parts) > 1:
                # ``t.LifoDiskQueueTest``: the prefix names a module.
                found = scopes.class_in(_base_source(parts, owner), name)
            if found is not None:
                base_cls, base_id, base_scope = found
                used_as_base.add(base_id)
                scopes.defined_in[base_id] = base_scope.parsed.module
                chain.append((base_cls, base_id))
                queue.extend((b, base_scope) for b in base_cls.bases)
            elif not (
                quiet
                or name in NO_TEST_BASES
                or (name.endswith("TestCase") and _framework_base(parts, owner))
            ):
                source = (
                    owner.imported[name][0]
                    if name in owner.imported
                    else _base_source(parts, owner)
                    if len(parts) > 1
                    else None
                )
                excluded = (excluded_files(kept_out, source) or [None])[-1] if source else None
                where = (
                    f"is imported from {excluded}, which diffcone.toml's [index] exclude keeps "
                    "out of the analysis (narrow the exclude pattern that matches it)"
                    if excluded is not None
                    else "is not defined in this module or imported from one in the source roots"
                )
                result.notes.append(
                    DiscoveryNote(
                        RUNNER,
                        "unknown_base_class",
                        f"{nodeid}: base class {name!r} {where}; test methods it may "
                        "contribute are not discovered",
                        parsed.path,
                    )
                )
        return chain

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

    def dynamic(prefix: str, bindings: list[tuple[str, str]]) -> None:
        """Notes for _dynamic_bindings, under ``prefix`` (a node id)."""
        for name, statement in bindings:
            detail = (
                f"{prefix}::{name} is bound by ``{statement}``, which discovery cannot follow"
                if name
                else f"{prefix}: ``{statement}`` binds names discovery cannot read"
            )
            result.notes.append(
                DiscoveryNote(
                    RUNNER,
                    "unmodelled_test_binding",
                    f"{detail}; pytest collects any test function or class it binds, and "
                    "those are not targets",
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
        resolved = mro(cls, nodeid_prefix, quiet=True, scope=scope)
        unittest_style = _is_unittest_class(cls) or any(
            _is_unittest_class(base) for base, _ in resolved
        )
        # A class with a constructor of its own or one it inherits from a
        # base discovery resolves (``__init__``, ``__new__``, ``@dataclass``)
        # is skipped, except a TestCase: the unittest plugin collects it all
        # the same. A base discovery cannot resolve is taken to have none.
        has_init = _has_init(cls) or any(_has_init(base) for base, _ in resolved)
        if not (unittest_style or _matches(classes, name)) or (has_init and not unittest_style):
            # A class pytest's own rules skip, but that defines test methods,
            # is a class some plugin collects (SQLAlchemy's testing plugin
            # collects ``<Name>Test``): report it rather than guess either way.
            if (
                not imported
                and cls.name not in base_names
                and not has_init
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
                uncollected.append((symbol, detail))
            return
        if class_id is None:
            class_id = f"{prefix_ids[-1]}.{cls.name}" if prefix_ids else parsed.member_id(cls.name)
        nodeid = f"{nodeid_prefix}::{name}"
        bases = mro(cls, nodeid, scope=scope)
        home = (scope or own_scope).parsed.module
        # Fixture lookup: this class with what it inherits, then outer classes.
        class_levels = [
            _class_level(
                [(cls, class_id), *bases], lambda owner: scopes.defined_in.get(owner, home)
            ),
            *(outer or []),
        ]
        # Marks: the enclosing scopes', the bases' (pytest reads marks along
        # the MRO), then the class's own.
        class_marks = inherited
        for owner, owner_id in [*reversed(bases), (cls, class_id)]:
            expand = scopes.expander(scopes.defined_in.get(owner_id, home))
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
        # test_a``, ``test_x = _check``, under ``if`` too): pytest collects
        # any attribute whose name matches and whose value is a function.
        # Entry: the function.
        entries: dict[str, str] = {}
        for owner, owner_id in [*reversed(bases), (cls, class_id)]:
            owner_defs = {f.name: f for f in scope_functions(owner.body)}
            same_module = scopes.defined_in.get(owner_id, home) == parsed.module
            for stmt in iter_scope_statements(owner.body):
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
                elif _may_bind_test(value, origins):
                    # A name or attribute discovery cannot see, or a call's
                    # result (``test_x = make(1)``, ``partial(_check, 1)``).
                    unmodelled(f"{nodeid}::{bound}", _shown(value))
        if not imported:
            dynamic(nodeid, _dynamic_bindings(cls.body, functions, classes, origins, in_class=True))
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
        # What ``self.name`` and ``super().name`` find, for ``request``
        # handed to a method (_RequestFlow).
        found_on: _Methods = {
            n: (f, scopes.defined_in.get(o, home)) for n, (f, o) in methods.items()
        }
        found_above: _Methods = {}
        for owner, owner_id in reversed(bases):
            for f in scope_functions(owner.body):
                found_above[f.name] = (f, scopes.defined_in.get(owner_id, home))
        for method in sorted(collected):
            func, owner_id = methods[method]
            owner_module = scopes.defined_in.get(owner_id, home)
            expand = scopes.expander(owner_module)
            marks = class_marks + _marks_from_expressions(func.decorator_list, expand)
            requests = list(_fixture_requests(func, True, marks, class_injected))
            requests += list(marks.usefixtures)
            requests += flow.requests(
                func, owner_module, None, found_on, found_above if owner_id == class_id else None
            )
            requests += flow.lazy(facts)
            if owner_module != parsed.module and (owner_facts := module_facts(owner_module)):
                requests += flow.lazy(owner_facts)
            entry = entries.get(method, f"{owner_id}.{method}")
            add(f"{nodeid}::{method}", entry, class_levels, requests, extra, marks)
        # Nested classes, inherited ones too (pytest collects a class's
        # attributes along its MRO), the nearest definition of a name winning.
        inners: dict[str, tuple[ast.ClassDef, str, _Scope | None]] = {}
        for owner, owner_id in reversed(bases):
            owner_scope = scopes.scope_for(scopes.defined_in.get(owner_id, home))
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
        requests += flow.requests(func, parsed.module) + flow.lazy(facts)
        add(f"{parsed.path}::{bound}", parsed.member_id(func.name), [], requests, [], marks)

    # ``f.__test__ = True``: pytest collects ``f`` whatever it is called.
    marked_tests: set[str] = set()
    for stmt in iter_scope_statements(body):
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
    for func in scope_functions(body):
        if not (_matches(functions, func.name) or func.name in marked_tests):
            continue
        if _is_fixture(func)[0]:
            continue
        module_test(func, func.name)

    # Tests bound by assignment (``test_alias = test_orig``, under ``if``
    # too): pytest collects any module attribute whose name matches and
    # whose value is a function or class. A value bound to something not
    # visible here (an attribute, an imported name: Hypothesis's
    # ``TestMachine = Machine.TestCase``), or a call's result that may be a
    # function or class (``test_one = make(1)``, ``functools.partial``,
    # ``TestGen = _make()``), is reported; data (``test_data = [...]``) is
    # not a test. So are tests bound dynamically (_dynamic_bindings).
    dynamic(parsed.path, _dynamic_bindings(body, functions, classes, origins))
    for stmt in iter_scope_statements(body):
        assigned = _assigned(stmt)
        if assigned is None or assigned[0] in defined:
            continue
        bound, value = assigned
        is_function = _matches(functions, bound)
        is_class = _matches(classes, bound)
        if not (is_function or is_class):
            continue
        if isinstance(value, ast.Name) and value.id in module_funcs and is_function:
            if not _is_fixture(module_funcs[value.id])[0]:
                module_test(module_funcs[value.id], bound)
        elif isinstance(value, ast.Name) and value.id in module_classes:
            target = module_classes[value.id]
            walk_class(
                target,
                [],
                parsed.path,
                module_marks,
                name=bound,
                class_id=parsed.member_id(target.name),
            )
        elif _may_bind_test(value, origins):
            unmodelled(f"{parsed.path}::{bound}", _shown(value))

    # pytest collects every module attribute matching the naming rules,
    # including functions and classes imported from elsewhere (fastapi's
    # tutorial tests import ``test_read_main`` from ``docs_src``).
    for stmt in iter_scope_statements(body):
        if not isinstance(stmt, ast.ImportFrom):
            continue
        source = parsed.import_source(stmt)
        aliases = list(stmt.names)
        starred = any(a.name == "*" for a in aliases)
        if starred:
            # ``from tests.test_install import *`` re-runs another module's
            # tests here (poetry's sync command does exactly this).
            origin = module_facts(source)
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
            aliases = [ast.alias(name=n, asname=None) for n in _star_exports(origin, module_facts)]
        for alias in aliases:
            bound = alias.asname or alias.name
            if bound in defined:
                continue
            is_function = _matches(functions, bound)
            is_class = _matches(classes, bound)
            # Followed through re-exports (``tests/base.py`` importing the
            # class from ``tests/impl.py``) to the module defining it.
            found = _definition_bound_to(source, alias.name, module_facts)
            origin, node = found if isinstance(found, tuple) else (None, None)
            if not (is_function or is_class):
                # Still a test when it is a unittest TestCase (collected
                # whatever its name): walk any in-scope class it names.
                if not isinstance(node, ast.ClassDef):
                    continue
            nodeid = f"{parsed.path}::{bound}"
            if isinstance(found, _Unfollowed) or found is None:
                # Bound there to what discovery cannot follow: reported,
                # unless it is data, which is no test. A name a star import
                # may offer but nothing binds is no test either (what it
                # offers can be a superset: _star_exports).
                if found is not None and found.value:
                    unmodelled(nodeid, f"{found.value} (in {source})")
                elif found is None and not starred:
                    unmodelled(nodeid, f"nothing discovery finds in {source}")
                continue
            if node is None or origin is None:
                # Outside the source roots, whether pytest collects anything
                # from it is unknown, so it is reported, not guessed; except
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
                origin_scope = scopes.scope_for(origin.parsed.module) or _Scope(
                    origin.parsed,
                    {c.name: c for c in scope_classes(origin.parsed.tree.body)},
                    {},
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
                    node.decorator_list, scopes.expander(origin.parsed.module)
                )
                requests = list(_fixture_requests(node, False, marks)) + list(marks.usefixtures)
                requests += flow.requests(node, origin.parsed.module)
                requests += flow.lazy(facts) + flow.lazy(origin)
                add(nodeid, entry, [], requests, [origin.parsed.module], marks)

    for cls in scope_classes(body):
        walk_class(cls, [], parsed.path, module_marks)

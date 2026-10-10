"""Regression scenarios for round 4 of the pre-release audit (the
code-quality pass): static discovery, Q4 to Q10.

Each test names the finding it guards, and each failed before its fix. The
pytest behaviour each one models was checked against pytest 9, and the ASV
behaviour against asv_runner's ``discovery.py``.
"""

from __future__ import annotations

import pytest

from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
from diffcone.testing import asv_target, selected, unselected

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'
BENCH = {
    "asv.conf.json": ASV_CONF,
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}
BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
T = "def test_{0}():\n    pass\n"


def _plan(repo, base, head, targets=(), runners=("pytest",)):
    return repo.plan(base, head, list(targets), discover_runners=list(runners))


def _discovered(repo, rev, runner="pytest"):
    snap = read_snapshot(repo.path, rev, ["."], with_config=True)
    return discover(runner, snap, build_index(snap), DiscoveryOptions())


def _kinds(result) -> set[str]:
    return {n.kind for n in result.notes}


# ---------------------------------------------------------------------- Q4

PLUG = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef auto():\n    {}\n"


@pytest.mark.parametrize(
    "declaration",
    [
        'pytest_plugins = ["tests.other"] + ["tests.plug"]\n',
        'pytest_plugins = ["tests.other"]\npytest_plugins += ["tests.plug"]\n',
        'pytest_plugins = ["tests.other"]\npytest_plugins.append("tests.plug")\n',
        'pytest_plugins = []\npytest_plugins.extend(("tests.other", "tests.plug"))\n',
        'pytest_plugins = "tests.other, tests.plug"\n',
        'BASE = ["tests.plug"]\npytest_plugins = list(BASE) + ["tests.other"]\n',
        'import sys\n\nif sys.platform:\n    pytest_plugins = ["tests.plug"]\n',
        "from tests.shared import pytest_plugins  # noqa\n",
        "from tests.shared import *  # noqa\n",
    ],
    ids=[
        "concat",
        "augassign",
        "append",
        "extend",
        "comma_string",
        "name",
        "under_if",
        "imported",
        "star",
    ],
)
def test_q4_pytest_plugins_however_built_are_read(repo, declaration):
    """Only ``pytest_plugins = [<literals>]`` was read: a plugin added with
    ``+``, ``+=``, ``.append`` or ``.extend`` (or a comma-separated string,
    or the list imported from another module) was no dependency, so a change
    to its autouse fixture selected nothing."""
    base = repo.commit(
        {
            **BENCH,
            "tests/__init__.py": "",
            "tests/other.py": "",
            "tests/shared.py": 'pytest_plugins = ["tests.plug"]\n',
            "tests/plug.py": PLUG.format("pass"),
            "tests/conftest.py": declaration,
            "tests/test_a.py": T.format("a"),
        }
    )
    head = repo.commit({"tests/plug.py": PLUG.format("x = 1")})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert unselected(plan) == {"bench.time_noop"}
    assert not plan.incomplete_discovery


@pytest.mark.parametrize(
    "declaration",
    [
        "from tests.names import plugin_names\n\npytest_plugins = plugin_names()\n",
        'pytest_plugins = [f"tests.{n}" for n in ("plug",)]\n',
        "from somewhere_else import pytest_plugins  # noqa\n",
    ],
    ids=["call", "comprehension", "outside"],
)
def test_q4_a_pytest_plugins_that_cannot_be_read_is_unknown(repo, declaration):
    """What pytest_plugins loads cannot be read: every test depends on an
    unknown, and the report says why."""
    base = repo.commit(
        {
            **BENCH,
            "tests/__init__.py": "",
            "tests/names.py": "def plugin_names():\n    return ['tests.plug']\n",
            "tests/plug.py": PLUG.format("pass"),
            "tests/conftest.py": declaration,
            "tests/test_a.py": T.format("a"),
            "tests/test_b.py": T.format("b"),
        }
    )
    head = repo.commit({"tests/plug.py": PLUG.format("x = 1")})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert unselected(plan) == {"bench.time_noop"}
    assert "unreadable_plugins" in _kinds(plan.discovery[0])


# ---------------------------------------------------------------------- Q5

CONF = "import pytest\n\n\n@pytest.fixture\ndef db():\n    {}\n"


@pytest.mark.parametrize(
    "mark",
    [
        'pytestmark = [pytest.mark.slow] + [pytest.mark.usefixtures("db")]\n',
        'pytestmark = [pytest.mark.slow]\npytestmark += [pytest.mark.usefixtures("db")]\n',
        'pytestmark = [pytest.mark.slow]\npytestmark.append(pytest.mark.usefixtures("db"))\n',
        'pytestmark = []\npytestmark.extend([pytest.mark.usefixtures("db")])\n',
        'NEEDS = [pytest.mark.usefixtures("db")]\npytestmark = [pytest.mark.slow] + NEEDS\n',
        "from tests.marks import pytestmark  # noqa\n",
    ],
    ids=["concat", "augassign", "append", "extend", "stored", "imported"],
)
def test_q5_pytestmark_however_built_applies_its_fixtures(repo, mark):
    """``pytestmark`` built with ``+`` or ``+=`` lost its ``usefixtures``:
    the fixture was no dependency of the module's tests."""
    base = repo.commit(
        {
            **BENCH,
            "tests/__init__.py": "",
            "tests/conftest.py": CONF.format("pass"),
            "tests/marks.py": 'import pytest\n\npytestmark = [pytest.mark.usefixtures("db")]\n',
            "tests/test_a.py": "import pytest\n" + mark + "\n\n" + T.format("a"),
            "tests/test_b.py": T.format("b"),
        }
    )
    head = repo.commit({"tests/conftest.py": CONF.format("x = 1")})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert unselected(plan) == {"tests/test_b.py::test_b", "bench.time_noop"}


def test_q5_a_class_pytestmark_built_with_augassign(repo):
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/conftest.py": CONF.format("pass"),
            "tests/test_a.py": (
                "import pytest\n\n\nclass TestA:\n"
                "    pytestmark = [pytest.mark.slow]\n"
                '    pytestmark += [pytest.mark.usefixtures("db")]\n\n'
                "    def test_a(self):\n        pass\n\n\n" + T.format("b")
            ),
        }
    )
    head = repo.commit({"tests/conftest.py": CONF.format("x = 1")})
    assert selected(_plan(repo, base, head)) == {"tests/test_a.py::TestA::test_a"}


# ---------------------------------------------------------------------- Q6

FIXTURE = "import pytest\n\n\n@pytest.fixture\ndef db(request):\n    {}\n"


@pytest.mark.parametrize(
    "decorator",
    [
        'INDIRECT = True\n\n\n@pytest.mark.parametrize("db", ["x"], indirect=INDIRECT)\n',
        '@pytest.mark.parametrize("db", ["x"], True)\n',
        'OPTS = {"indirect": True}\n\n\n@pytest.mark.parametrize("db", ["x"], **OPTS)\n',
        'NAMES = ["db"]\n\n\n@pytest.mark.parametrize("db", ["x"], indirect=NAMES)\n',
        '@pytest.mark.parametrize("db", ["x"], indirect=["d" + "b"])\n',
    ],
    ids=["variable", "positional", "kwargs", "name_list", "built_list"],
)
def test_q6_indirect_that_is_not_a_literal_false_keeps_the_fixture(repo, decorator):
    """``indirect=`` as a variable or as the third positional argument
    counted as not indirect, so the fixture was pruned as a parameter."""
    base = repo.commit(
        {
            **BENCH,
            "tests/__init__.py": "",
            "tests/conftest.py": FIXTURE.format("pass"),
            "tests/test_a.py": "import pytest\n\n" + decorator + "def test_a(db):\n    pass\n",
        }
    )
    head = repo.commit({"tests/conftest.py": FIXTURE.format("x = 1")})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert unselected(plan) == {"bench.time_noop"}


@pytest.mark.parametrize(
    "decorator",
    [
        '@pytest.mark.parametrize("db", ["x"])\n',
        '@pytest.mark.parametrize("db", ["x"], indirect=False)\n',
        '@pytest.mark.parametrize("db", ["x"], False)\n',
        '@pytest.mark.parametrize("db, other", [("x", 1)], indirect=["other"])\n',
    ],
    ids=["none", "false", "positional_false", "other_name"],
)
def test_q6_a_direct_parameter_still_replaces_the_fixture(repo, decorator):
    """The narrowing this keeps: a name parametrized directly is no fixture."""
    args = "db, other" if "other" in decorator else "db"
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/conftest.py": FIXTURE.format("pass")
            + "\n\n@pytest.fixture\ndef other(request):\n    pass\n",
            "tests/test_a.py": (
                "import pytest\n\n" + decorator + f"def test_a({args}):\n    pass\n"
            ),
        }
    )
    head = repo.commit(
        {
            "tests/conftest.py": FIXTURE.format("x = 1")
            + "\n\n@pytest.fixture\ndef other(request):\n    pass\n"
        }
    )
    assert selected(_plan(repo, base, head)) == set()


def test_q6_a_maybe_indirect_name_that_names_no_fixture_is_a_parameter(repo):
    """The other side, pydantic's ``parametrize('decorator1',
    **dataclass_decorators())``: ``**kwargs`` may hold ``indirect``, but
    with no fixture of that name pytest would fail the test, so the name is
    a parameter, not an unknown fixture selected on every change."""
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/test_a.py": (
                "import pytest\n\n\n"
                "def opts():\n    return {'argvalues': [1], 'ids': ['one']}\n\n\n"
                "@pytest.mark.parametrize('value', **opts())\ndef test_a(value):\n    pass\n"
            ),
            "tests/test_b.py": T.format("b"),
        }
    )
    head = repo.commit({"tests/test_b.py": "def test_b():\n    assert True\n"})
    plan = _plan(repo, base, head)
    assert selected(plan) == {"tests/test_b.py::test_b"}
    assert "unresolved_fixture" not in _kinds(plan.discovery[0])


# ---------------------------------------------------------------------- Q7


@pytest.mark.parametrize(
    "bench, name",
    [
        (
            "def _impl():\n    return 'import pkg.heavy'\n\n\ntimeraw_import = _impl\n",
            "bench._impl",
        ),
        (
            "class B:\n    def _impl(self):\n        return 'import pkg.heavy'\n\n"
            "    timeraw_import = _impl\n",
            "bench.B.timeraw_import",
        ),
        (
            "def run():\n    return 'import pkg.heavy'\n\n\n"
            "run.benchmark_name = 'suite.timeraw_run'\n",
            "suite.timeraw_run",
        ),
    ],
    ids=["module_alias", "class_alias", "benchmark_name"],
)
def test_q7_a_timeraw_benchmark_is_judged_by_its_bound_name(repo, bench, name):
    """asv_runner picks the benchmark type by the attribute's name (or a
    ``benchmark_name``'s last part); discovery judged ``timeraw_`` by the
    function's own name, so the imports of its code were no dependency."""
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "pkg/__init__.py": "",
            "pkg/heavy.py": "X = 1\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": bench,
            "benchmarks/other.py": "def time_other():\n    pass\n",
            "tests/__init__.py": "",
            "tests/test_a.py": T.format("a"),
        }
    )
    head = repo.commit({"pkg/heavy.py": "X = 2\n"})
    plan = _plan(repo, base, head, runners=("pytest", "asv"))
    assert selected(plan) == {name}
    assert unselected(plan) == {"other.time_other", "tests/test_a.py::test_a"}


# ---------------------------------------------------------------------- Q8


@pytest.mark.parametrize(
    "impl",
    [
        "import sys\n\nif sys.version_info >= (3,):\n"
        "    def test_x():\n        pass\n\n"
        "    class TestY:\n        def test_y(self):\n            pass\n",
        "try:\n    import json  # noqa\nexcept ImportError:\n    pass\nelse:\n"
        "    def test_x():\n        pass\n\n"
        "    class TestY:\n        def test_y(self):\n            pass\n",
    ],
    ids=["if", "try_else"],
)
def test_q8_an_imported_test_defined_under_a_block_is_collected(repo, impl):
    """``_definition_bound_to`` read only the defining module's top level:
    the imported tests were dropped with no note (under-run, exit 0)."""
    base = repo.commit(
        {
            **BENCH,
            "tests/__init__.py": "",
            "tests/impl.py": impl,
            "tests/test_a.py": "from tests.impl import TestY, test_x  # noqa\n",
        }
    )
    targets = {t.runner_id for t in _discovered(repo, base).targets}
    assert targets == {"tests/test_a.py::test_x", "tests/test_a.py::TestY::test_y"}
    head = repo.commit({"tests/impl.py": impl.replace("pass\n\n", "assert 1\n\n", 1)})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert "tests/test_a.py::test_x" in selected(plan)
    assert "bench.time_noop" in unselected(plan)


@pytest.mark.parametrize(
    "module, expected",
    [
        (
            "import sys\n\n\ndef _check():\n    pass\n\n\nif sys.platform:\n"
            "    test_alias = _check\n",
            "tests/test_a.py::test_alias",
        ),
        (
            "import sys\n\n\ndef check():\n    pass\n\n\nif sys.platform:\n"
            "    check.__test__ = True\n",
            "tests/test_a.py::check",
        ),
        (
            "import sys\n\n\nclass TestA:\n    def _check(self):\n        pass\n\n"
            "    if sys.platform:\n        test_alias = _check\n",
            "tests/test_a.py::TestA::test_alias",
        ),
    ],
    ids=["module_alias", "dunder_test", "class_alias"],
)
def test_q8_a_test_bound_under_a_block_is_collected(repo, module, expected):
    """A module-level test binding under ``if`` (and ``f.__test__ = True``,
    and a class-body binding) was read from the top level only: dropped."""
    base = repo.commit({"tests/__init__.py": "", "tests/test_a.py": module})
    result = _discovered(repo, base)
    assert {t.runner_id for t in result.targets} == {expected}
    assert not result.incomplete


@pytest.mark.parametrize(
    "impl",
    [
        "class _Factory:\n    def make(self):\n        pass\n\n\nF = _Factory()\ntest_x = F.make\n",
        "def _make():\n    pass\n\n\nglobals()['test_x'] = _make\n",
    ],
    ids=["attribute", "unbound"],
)
def test_q8_an_imported_test_discovery_cannot_follow_is_reported(repo, impl):
    """Never a silent drop: what the defining module binds the name to
    cannot be followed, so the report says the target list may be short."""
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/impl.py": impl,
            "tests/test_a.py": "from tests.impl import test_x  # noqa\n\n\n" + T.format("a"),
        }
    )
    result = _discovered(repo, base)
    assert {n.kind for n in result.incomplete} == {"unmodelled_test_binding"}
    assert any("test_x" in n.detail for n in result.notes)


def test_q8_imported_data_and_modules_are_no_tests(repo):
    """The other side: a name bound to data, or to a module, is no test and
    no note."""
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/helpers.py": "import os as test_os  # noqa\n\ntest_data = [1, 2]\n",
            "tests/testenv.py": "X = 1\n",
            "tests/test_a.py": (
                "from tests import testenv  # noqa\n"
                "from tests.helpers import test_data, test_os  # noqa\n"
                "from tests.helpers import *  # noqa\n\n\n" + T.format("a")
            ),
        }
    )
    result = _discovered(repo, base)
    assert {t.runner_id for t in result.targets} == {"tests/test_a.py::test_a"}
    assert not result.incomplete


def test_q8_an_asv_benchmark_defined_under_a_block(repo):
    """asv reads the module's namespace: a benchmark defined, imported or
    aliased under ``if``/``try`` counts."""
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "",
            "benchmarks/common.py": (
                "import sys\n\nif sys.platform:\n    def time_shared():\n        pass\n"
            ),
            "benchmarks/bench.py": (
                "import sys\n\ntry:\n    from .common import time_shared  # noqa\n"
                "except ImportError:\n    pass\n\n\ndef _impl():\n    pass\n\n\n"
                "if sys.platform:\n    time_alias = _impl\n"
            ),
        }
    )
    targets = {t.runner_id for t in _discovered(repo, base, "asv").targets}
    assert {"bench.time_shared", "bench._impl", "common.time_shared"} <= targets


# ---------------------------------------------------------------------- Q9


@pytest.mark.parametrize(
    "files",
    [
        {
            "tests/helpers.py": "def pytest_collect_file(parent, file_path):\n    return None\n",
            "tests/conftest.py": "from tests.helpers import *  # noqa\n",
        },
        {"tests/conftest.py": "from sybil_ext.pytest import pytest_collect_file  # noqa\n"},
        {"tests/conftest.py": "from sybil_ext.pytest import *  # noqa\n"},
        {
            "tests/helpers.py": "from sybil_ext.pytest import *  # noqa\n",
            "tests/conftest.py": "pytest_plugins = ['tests.helpers']\n",
        },
    ],
    ids=["star_in_roots", "named_outside", "star_outside", "plugin_star_outside"],
)
def test_q9_a_collection_hook_bound_by_import_is_reported(repo, files):
    """A ``pytest_collect_file`` bound by a star import, or imported from
    outside the source roots, gave no ``plugin_collects_files`` note: exit 0
    instead of 3."""
    base = repo.commit({"tests/__init__.py": "", "tests/test_a.py": T.format("a"), **files})
    result = _discovered(repo, base)
    assert {n.kind for n in result.incomplete} == {"plugin_collects_files"}


# ---------------------------------------------------------------------- Q10


def test_q10_benchmark_dir_naming_the_configuration_directory(repo):
    """``"benchmark_dir": "."`` resolves to the configuration's directory;
    discovery matched no file there, with only a ``no_benchmarks`` note."""
    base = repo.commit(
        {
            "asv.conf.json": '{"version": 1, "benchmark_dir": "."}',
            "bench_x.py": "def time_a():\n    pass\n",
            "tests/__init__.py": "",
            "tests/test_a.py": T.format("a"),
        }
    )
    result = _discovered(repo, base, "asv")
    assert [t.runner_id for t in result.targets] == ["bench_x.time_a"]
    assert not result.notes
    head = repo.commit({"bench_x.py": "def time_a():\n    return 1\n"})
    plan = _plan(repo, base, head, runners=("pytest", "asv"))
    assert selected(plan) == {"bench_x.time_a"}


def test_q10_a_benchmark_dir_outside_the_repository_is_incomplete(repo):
    base = repo.commit(
        {
            "asv.conf.json": '{"version": 1, "benchmark_dir": "../elsewhere"}',
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_a():\n    pass\n",
        }
    )
    result = _discovered(repo, base, "asv")
    assert not result.targets
    assert {n.kind for n in result.incomplete} == {"test_file_outside_roots"}


# ------------------------------------------------- quality: asv benchmark_name


def test_asv_benchmark_name_follows_the_function(repo):
    """asv_runner reads ``benchmark_name`` off the function, so it applies
    to an inherited method and to a function imported from the module that
    sets it, not only in the body that defines it."""
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "",
            "benchmarks/common.py": (
                "def time_shared():\n    pass\n\n\n"
                "time_shared.benchmark_name = 'custom.time_shared'\n\n\n"
                "class Base:\n    def time_x(self):\n        pass\n\n"
                "    time_x.benchmark_name = 'custom.time_x'\n"
            ),
            "benchmarks/bench.py": (
                "from .common import Base, time_shared  # noqa\n\n\n"
                "class Sub(Base):\n    def time_own(self):\n        pass\n"
            ),
        }
    )
    targets = {t.runner_id for t in _discovered(repo, base, "asv").targets}
    assert targets == {"custom.time_shared", "custom.time_x", "bench.Sub.time_own"}


# ------------------------------------------- quality: shared module scopes

CYCLE = {
    "tests/__init__.py": "",
    "tests/conftest.py": (
        "import pytest\n\n\n@pytest.fixture\ndef fa():\n    pass\n\n\n"
        "@pytest.fixture\ndef fb():\n    pass\n"
    ),
    "tests/test_a.py": (
        "import pytest\n\nfrom tests.test_b import *  # noqa\n\n"
        "needs_a = pytest.mark.usefixtures('fa')\n\n\n"
        "class BaseA:\n    def test_from_a(self):\n        pass\n\n\n"
        "@needs_b\nclass TestA(BaseB):\n    def test_a(self):\n        pass\n"
    ),
    "tests/test_b.py": (
        "import pytest\n\nfrom tests.test_a import *  # noqa\n\n"
        "needs_b = pytest.mark.usefixtures('fb')\n\n\n"
        "class BaseB:\n    def test_from_b(self):\n        pass\n\n\n"
        "@needs_a\nclass TestB(BaseA):\n    def test_b(self):\n        pass\n"
    ),
}


@pytest.mark.parametrize("module", ["tests/test_a.py", "tests/test_b.py"])
def test_shared_scopes_find_what_a_walk_of_one_module_finds(repo, module):
    """Module scopes are built once for every test module; through an
    import cycle what is found depends on where the walk entered it, so
    such scopes are kept per test module: each module's targets are the
    ones discovery finds when pytest is given that module alone."""
    base = repo.commit(CYCLE)
    snap = read_snapshot(repo.path, base, ["."], with_config=True)
    index = build_index(snap)
    both = discover("pytest", snap, index, DiscoveryOptions())
    alone = discover("pytest", snap, index, DiscoveryOptions(runner_args=(module,)))
    assert alone.targets
    assert [t for t in both.targets if t.runner_id.startswith(module)] == alone.targets

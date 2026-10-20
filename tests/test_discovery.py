"""Static runner discovery: pytest and ASV."""

from __future__ import annotations

import json

import pytest

from diffcone.cache import IndexCache
from diffcone.cli import main
from diffcone.discovery import (
    DiscoveryOptions,
    discover,
    environment_options,
    pytest_command_arguments,
    relative_arguments,
)
from diffcone.indexer import build_index
from diffcone.report import to_text
from diffcone.snapshot import read_snapshot
from diffcone.testing import asv_target, py_target, reason, rules, selected, unselected


def run_discovery(repo, rev, runner, roots=None, **opts):
    snap = read_snapshot(repo.path, rev, roots or ["."], with_config=True)
    return discover(runner, snap, build_index(snap), DiscoveryOptions(**opts))


def by_id(result):
    return {t.runner_id: t for t in result.targets}


ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'
BENCH = {
    "asv.conf.json": ASV_CONF,
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}
BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
T = "def test_{0}():\n    pass\n"
LIB = {
    "lib/__init__.py": "",
    "lib/core.py": "def value():\n    return 1\n",
    "tests/__init__.py": "",
}


def _change_selects(repo, files, change, roots=None):
    base = repo.commit({**LIB, **BENCH, **files})
    head = repo.commit(change)
    plan = repo.plan(base, head, [], roots, discover_runners=["pytest", "asv"])
    assert "bench.time_noop" in unselected(plan)
    return selected(plan)


def _targets_and_notes(repo, files, runner="pytest"):
    rev = repo.commit(files)
    plan = repo.plan(rev, rev, [], discover_runners=[runner])
    notes = {n.kind for d in plan.discovery for n in d.notes}
    return {t.runner_id for t in plan.targets}, notes


def _plan_with_options(repo, base, head, targets=(), **options):
    kwargs = {"discovery_options": DiscoveryOptions(**options)} if options else {}
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], **kwargs)


def _targets(plan) -> set[str]:
    return {t.runner_id for t in plan.targets}


def _plan_with_runners(repo, base, head, targets=(), runners=("pytest",)):
    return repo.plan(base, head, list(targets), discover_runners=list(runners))


def _discovered(repo, rev, runner="pytest"):
    snap = read_snapshot(repo.path, rev, ["."], with_config=True)
    return discover(runner, snap, build_index(snap), DiscoveryOptions())


def _kinds(result) -> set[str]:
    return {n.kind for n in result.notes}


def _plan_with_bench(repo, base, head):
    return repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])


def _deps(result, nodeid: str) -> tuple[str, ...]:
    return next(t for t in result.targets if t.runner_id == nodeid).lifecycle_dependencies


PYTEST_TREE = {
    "conftest.py": (
        "import pytest\n\n"
        "pytest_plugins = ['plugins.shared']\n\n\n"
        "@pytest.fixture\ndef root_db():\n    return {}\n\n\n"
        "@pytest.fixture(autouse=True)\ndef root_auto():\n    pass\n\n\n"
        "def pytest_collection_modifyitems(items):\n    pass\n"
    ),
    "plugins/__init__.py": "",
    "plugins/shared.py": "import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 1\n",
    "tests/conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef db(root_db):\n    return root_db\n\n\n"
        "@pytest.fixture(name='renamed')\ndef _renamed_impl():\n    return 2\n\n\n"
        "def pytest_runtest_setup(item):\n    pass\n"
    ),
    "tests/test_basic.py": (
        "import pytest\n\n"
        "pytestmark = pytest.mark.usefixtures('renamed')\n\n\n"
        "def setup_module():\n    pass\n\n\n"
        "@pytest.fixture\ndef local(db):\n    return db\n\n\n"
        "def test_plain():\n    pass\n\n\n"
        "def test_with_fixtures(local, tmp_path, widget, shared):\n    pass\n\n\n"
        "@pytest.mark.usefixtures('root_db')\ndef test_marked():\n    pass\n\n\n"
        "def helper():\n    pass\n\n\n"
        "class TestGroup:\n"
        "    @pytest.fixture\n    def cfix(self):\n        return 3\n\n"
        "    def setup_method(self):\n        pass\n\n"
        "    def test_method(self, cfix):\n        pass\n\n"
        "    class TestInner:\n"
        "        def test_deep(self):\n            pass\n\n\n"
        "class TestWithInit:\n"
        "    def __init__(self):\n        pass\n\n"
        "    def test_ignored(self):\n        pass\n\n\n"
        "class NotCollected:\n"
        "    def test_no(self):\n        pass\n\n\n"
        "import unittest\n\n\n"
        "class LegacyCase(unittest.TestCase):\n"
        "    def setUp(self):\n        pass\n\n"
        "    def testOld(self):\n        pass\n"
    ),
    "tests/sub/test_override.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef db():\n    return 'overridden'\n\n\n"
        "def test_override(db):\n    pass\n"
    ),
    "tests/helpers_test.py": "def test_suffix():\n    pass\n",
    "tests/notatest.py": "def test_not_collected():\n    pass\n",
    "pkg/__init__.py": "",
    "pkg/core.py": "def f():\n    pass\n",
}


def test_pytest_discovery_rules(repo):
    rev = repo.commit(PYTEST_TREE)
    result = run_discovery(repo, rev, "pytest")
    targets = by_id(result)

    assert set(targets) == {
        "tests/test_basic.py::test_plain",
        "tests/test_basic.py::test_with_fixtures",
        "tests/test_basic.py::test_marked",
        "tests/test_basic.py::TestGroup::test_method",
        "tests/test_basic.py::TestGroup::TestInner::test_deep",
        "tests/test_basic.py::LegacyCase::testOld",
        "tests/sub/test_override.py::test_override",
        "tests/helpers_test.py::test_suffix",
    }
    assert result.config["python_files"] == ["test_*.py", "*_test.py"]
    assert result.config["source"] is None

    common = {
        "tests.test_basic",  # the test module itself
        "tests.conftest",
        "conftest",
        "tests.conftest.pytest_runtest_setup",
        "conftest.pytest_collection_modifyitems",
        "tests.test_basic.setup_module",
        "conftest.root_auto",  # autouse
        "tests.conftest._renamed_impl",  # module pytestmark usefixtures('renamed')
        "tests.test_basic.pytestmark",  # module-level pytest names are variable symbols
        "conftest.pytest_plugins",
        "plugins.shared",  # a plugin module's import-time code runs for every test
    }
    plain = targets["tests/test_basic.py::test_plain"]
    assert plain.entry_symbol == "tests.test_basic.test_plain"
    assert set(plain.lifecycle_dependencies) == common

    rich = targets["tests/test_basic.py::test_with_fixtures"]
    assert set(rich.lifecycle_dependencies) == common | {
        "tests.test_basic.local",
        "tests.conftest.db",  # transitively through local
        "conftest.root_db",  # transitively through db
        "plugins.shared.shared",  # via pytest_plugins
        "fixture:widget",  # unknown: conservative
    }
    assert {(n.kind) for n in result.notes} == {"unresolved_fixture", "uncollected_test_class"}
    assert "widget" in next(n for n in result.notes if n.kind == "unresolved_fixture").detail
    # ``NotCollected`` has test methods but no matching name: reported, since
    # a plugin may collect it (SQLAlchemy's testing plugin collects ``*Test``).
    uncollected = next(n for n in result.notes if n.kind == "uncollected_test_class")
    assert uncollected.detail.startswith("tests/test_basic.py::NotCollected: defines test methods")

    marked = targets["tests/test_basic.py::test_marked"]
    assert "conftest.root_db" in marked.lifecycle_dependencies

    method = targets["tests/test_basic.py::TestGroup::test_method"]
    assert method.entry_symbol == "tests.test_basic.TestGroup.test_method"
    assert {"tests.test_basic.TestGroup.cfix", "tests.test_basic.TestGroup.setup_method"} <= set(
        method.lifecycle_dependencies
    )

    deep = targets["tests/test_basic.py::TestGroup::TestInner::test_deep"]
    assert deep.entry_symbol == "tests.test_basic.TestGroup.TestInner.test_deep"

    legacy = targets["tests/test_basic.py::LegacyCase::testOld"]
    assert "tests.test_basic.LegacyCase.setUp" in legacy.lifecycle_dependencies

    # Nearest scope wins: the module-level db shadows tests/conftest.py's db.
    override = targets["tests/sub/test_override.py::test_override"]
    assert "tests.sub.test_override.db" in override.lifecycle_dependencies
    assert "tests.conftest.db" not in override.lifecycle_dependencies
    assert "conftest.root_db" not in override.lifecycle_dependencies

    # Declaring the plugin fixture as external removes the conservative dep
    # and reports the assumption.
    result2 = run_discovery(repo, rev, "pytest", external_fixtures=frozenset({"widget"}))
    rich2 = by_id(result2)["tests/test_basic.py::test_with_fixtures"]
    assert "fixture:widget" not in rich2.lifecycle_dependencies
    assert sorted(n.kind for n in result2.notes) == ["external_fixture", "uncollected_test_class"]
    external = next(n for n in result2.notes if n.kind == "external_fixture")
    assert "'widget' requested by 1 test(s)" in external.detail
    assert "declared external" in external.detail


def test_pytest_config_from_pyproject_and_ini(repo):
    files = {
        "pyproject.toml": (
            "[tool.pytest.ini_options]\n"
            'python_files = ["check_*.py"]\n'
            'python_functions = ["check_*"]\n'
            'python_classes = ["Suite*"]\n'
            'testpaths = ["qa"]\n'
        ),
        "qa/check_one.py": (
            "def check_it():\n    pass\n\n\n"
            "def test_not_matched():\n    pass\n\n\n"
            "class SuiteA:\n    def check_m(self):\n        pass\n"
        ),
        "other/check_two.py": "def check_outside_testpaths():\n    pass\n",
        "tests/test_default.py": "def test_ignored_now():\n    pass\n",
    }
    rev = repo.commit(files)
    result = run_discovery(repo, rev, "pytest")
    assert result.config["source"] == "pyproject.toml"
    assert set(by_id(result)) == {"qa/check_one.py::check_it", "qa/check_one.py::SuiteA::check_m"}

    rev2 = repo.commit(
        {"pytest.ini": "[pytest]\npython_files = check_*.py\ntestpaths = qa other\n"}
    )
    result = run_discovery(repo, rev2, "pytest")
    assert result.config["source"] == "pytest.ini"
    assert result.config["python_functions"] == ["test"]
    # "other" is now a testpath, but its function names use the default prefix.
    assert set(by_id(result)) == {"qa/check_one.py::test_not_matched"}


ASV_TREE = {
    "asv.conf.json": (
        "{\n"
        "    // comments are allowed in asv.conf.json\n"
        '    "version": 1,\n'
        '    "benchmark_dir": "asv_bench/benchmarks"\n'
        "}\n"
    ),
    "asv_bench/__init__.py": "",
    "asv_bench/benchmarks/__init__.py": "",
    "asv_bench/benchmarks/ops.py": (
        "timeout = 60\n\n\n"
        "def setup():\n    pass\n\n\n"
        "def time_module_level():\n    pass\n\n\n"
        "def helper():\n    pass\n\n\n"
        "class TimeOps:\n"
        "    params = [1, 2]\n\n"
        "    def setup(self, n):\n        pass\n\n"
        "    def setup_cache(self):\n        pass\n\n"
        "    def time_add(self, n):\n        pass\n\n"
        "    def peakmem_add(self, n):\n        pass\n\n"
        "    def track_ratio(self, n):\n        return 1\n\n"
        "    def _time_private(self, n):\n        pass\n\n"
        "    def not_a_benchmark(self, n):\n        pass\n\n\n"
        "class _Hidden:\n    def time_hidden(self):\n        pass\n"
    ),
    "asv_bench/benchmarks/sub/__init__.py": "",
    "asv_bench/benchmarks/sub/deep.py": "def mem_thing():\n    pass\n",
    "asv_bench/benchmarks/_skipped.py": "def time_skipped():\n    pass\n",
    "benchmarks/other.py": "def time_wrong_dir():\n    pass\n",
}


def test_asv_discovery_rules(repo):
    rev = repo.commit(ASV_TREE)
    result = run_discovery(repo, rev, "asv")
    targets = by_id(result)
    assert result.config == {"source": "asv.conf.json", "benchmark_dir": "asv_bench/benchmarks"}
    assert set(targets) == {
        "ops.time_module_level",
        "ops.TimeOps.time_add",
        "ops.TimeOps.peakmem_add",
        "ops.TimeOps.track_ratio",
        "sub.deep.mem_thing",
        # ASV walks the package with pkgutil: underscore modules too (audit D10).
        "_skipped.time_skipped",
    }
    module_level = targets["ops.time_module_level"]
    assert module_level.entry_symbol == "asv_bench.benchmarks.ops.time_module_level"
    assert set(module_level.lifecycle_dependencies) == {
        "asv_bench.benchmarks.ops",
        "asv_bench.benchmarks.ops.setup",
    }
    add = targets["ops.TimeOps.time_add"]
    assert add.entry_symbol == "asv_bench.benchmarks.ops.TimeOps.time_add"
    assert set(add.lifecycle_dependencies) == {
        "asv_bench.benchmarks.ops",
        "asv_bench.benchmarks.ops.setup",
        "asv_bench.benchmarks.ops.TimeOps.setup",
        "asv_bench.benchmarks.ops.TimeOps.setup_cache",
    }
    assert result.notes == []

    rev2 = repo.commit({"asv.conf.json": None})
    result = run_discovery(repo, rev2, "asv")
    assert result.config["benchmark_dir"] == "benchmarks"
    assert set(by_id(result)) == {"other.time_wrong_dir"}


def test_plan_with_discovery_end_to_end(repo):
    base = repo.commit(
        {
            "pkg/db.py": "def connect():\n    return {}\n",
            "pkg/math.py": "def add(a, b):\n    return a + b\n",
            "tests/conftest.py": (
                "import pytest\nfrom pkg.db import connect\n\n\n"
                "@pytest.fixture\ndef db():\n    return connect()\n"
            ),
            "tests/test_all.py": (
                "from pkg.math import add\n\n\n"
                "def test_db(db):\n    assert db == {}\n\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n\n"
                "def test_nothing():\n    assert True\n"
            ),
            "benchmarks/bench.py": (
                "from pkg.db import connect\nfrom pkg.math import add\n\n\n"
                "class Suite:\n"
                "    def setup(self):\n        self.db = connect()\n\n"
                "    def time_add(self):\n        add(1, 2)\n\n\n"
                "def time_free():\n    pass\n"
            ),
        }
    )
    head = repo.commit({"pkg/db.py": "def connect():\n    return dict()\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert {t.runner_id for t in plan.targets} == {
        "tests/test_all.py::test_db",
        "tests/test_all.py::test_add",
        "tests/test_all.py::test_nothing",
        "bench.Suite.time_add",
        "bench.time_free",
    }
    assert selected(plan) == {"tests/test_all.py::test_db", "bench.Suite.time_add"}
    assert unselected(plan) == {
        "tests/test_all.py::test_add",
        "tests/test_all.py::test_nothing",
        "bench.time_free",
    }
    r = reason(plan, "tests/test_all.py::test_db")
    assert [s.kind for s in r.path] == ["lifecycle", "references"]
    assert r.changed_symbol == "pkg.db.connect"
    assert [d.runner for d in plan.discovery] == ["pytest", "asv"]

    # A manifest entry for the same target overrides the discovered one.
    override = [
        py_target(
            "tests/test_all.py::test_nothing", "tests.test_all.test_nothing", "pkg.db.connect"
        )
    ]
    plan2 = repo.plan(base, head, override, discover_runners=["pytest"])
    assert "tests/test_all.py::test_nothing" in selected(plan2)
    assert "tests/test_all.py::test_add" in unselected(plan2)

    # Conftest edits select every test under that conftest via its module.
    head2 = repo.commit(
        {
            "tests/conftest.py": (
                "import pytest\n\nX = 1\n\n\n@pytest.fixture\ndef db():\n    return {}\n"
            )
        }
    )
    plan3 = repo.plan(base, head2, [], discover_runners=["pytest", "asv"])
    assert {
        "tests/test_all.py::test_db",
        "tests/test_all.py::test_add",
        "tests/test_all.py::test_nothing",
    } <= selected(plan3)
    assert "bench.time_free" in unselected(plan3)


def test_cli_discover_and_plan_with_discover(repo, capsys):
    base = repo.commit(
        {
            "pkg/m.py": "def f():\n    return 1\n",
            "tests/test_m.py": "from pkg.m import f\n\n\ndef test_f(widget):\n    assert f()\n",
        }
    )
    head = repo.commit({"pkg/m.py": "def f():\n    return 2\n"})
    code = main(["discover", "--repo", str(repo.path), "--rev", head, "--discover", "pytest"])
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert data["source_roots"] == ["."]
    assert [t["runner_id"] for t in data["targets"]] == ["tests/test_m.py::test_f"]
    assert data["targets"][0]["lifecycle_dependencies"] == ["fixture:widget", "tests.test_m"]
    assert data["discovery"]["runners"][0]["notes"][0]["kind"] == "unresolved_fixture"

    manifest = repo.path.parent / "discovered.json"
    manifest.write_text(out)
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--targets",
            str(manifest),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [t["runner_id"] for t in report["selected_targets"]] == ["tests/test_m.py::test_f"]
    assert report["selected_targets"][0]["conservative"] is True  # fixture:widget unresolved

    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--discover",
            "pytest",
            "--assume-external-fixture",
            "widget",
            "--format",
            "text",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "discovery (pytest): 1 target(s), 1 note(s)" in out
    assert "tests/test_m.py::test_f\n" in out and "(conservative)" not in out


# --- regression tests added after code review ------------------------------


def test_pytest_parameters_that_are_not_fixture_requests(repo):
    rev = repo.commit(
        {
            "tests/conftest.py": (
                "import pytest\n\n\n"
                "@pytest.fixture\ndef db():\n    return {}\n\n\n"
                "@pytest.fixture\ndef cfg():\n    return {}\n"
            ),
            "tests/test_params.py": (
                "import pytest\nfrom unittest import mock\n\n"
                "pytestmark = pytest.mark.parametrize('mod_case', [1])\n\n\n"
                "@pytest.mark.parametrize('n, m', [(1, 2)])\n"
                "def test_parametrized(n, m, db):\n    pass\n\n\n"
                "@pytest.mark.parametrize(['a', 'b'], [(1, 2)])\n"
                "def test_list_argnames(a, b):\n    pass\n\n\n"
                "@pytest.mark.parametrize('db', ['x'], indirect=True)\n"
                "def test_indirect(db):\n    pass\n\n\n"
                "@pytest.mark.parametrize('db, plain', [({}, 1)], indirect=['db'])\n"
                "def test_indirect_list(db, plain):\n    pass\n\n\n"
                "def test_defaults(db, limit=3, *, verbose=False, cfg):\n    pass\n\n\n"
                "@mock.patch('os.getcwd')\n"
                "@mock.patch.object(dict, 'get')\n"
                "@mock.patch('os.sep', '/')\n"
                "def test_patched(mock_get, mock_cwd, db):\n    pass\n\n\n"
                "def test_module_mark(mod_case, cfg):\n    pass\n\n\n"
                "@pytest.mark.parametrize('cls_case', [1])\n"
                "class TestGroup:\n"
                "    def test_in_class(self, cls_case, db):\n        pass\n"
            ),
        }
    )
    result = run_discovery(repo, rev, "pytest")
    targets = by_id(result)
    fixture_deps = {
        k: {d for d in t.lifecycle_dependencies if d.startswith(("fixture:", "tests.conftest."))}
        for k, t in targets.items()
    }
    assert fixture_deps == {
        "tests/test_params.py::test_parametrized": {"tests.conftest.db"},
        "tests/test_params.py::test_list_argnames": set(),
        "tests/test_params.py::test_indirect": {"tests.conftest.db"},
        "tests/test_params.py::test_indirect_list": {"tests.conftest.db"},
        "tests/test_params.py::test_defaults": {"tests.conftest.db", "tests.conftest.cfg"},
        "tests/test_params.py::test_patched": {"tests.conftest.db"},
        "tests/test_params.py::test_module_mark": {"tests.conftest.cfg"},
        "tests/test_params.py::TestGroup::test_in_class": {"tests.conftest.db"},
    }
    assert result.notes == []


def test_pytest_same_name_fixture_override_keeps_outer_fixture(repo):
    base = repo.commit(
        {
            "conftest.py": "import pytest\n\n\n@pytest.fixture\ndef db():\n    return {}\n",
            "tests/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef db(db):\n    return dict(db)\n"
            ),
            "tests/test_x.py": "def test_x(db):\n    pass\n",
        }
    )
    result = run_discovery(repo, base, "pytest")
    deps = set(by_id(result)["tests/test_x.py::test_x"].lifecycle_dependencies)
    assert {"tests.conftest.db", "conftest.db"} <= deps
    assert result.notes == []

    head = repo.commit(
        {"conftest.py": "import pytest\n\n\n@pytest.fixture\ndef db():\n    return {'v': 2}\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_x.py::test_x"}
    assert reason(plan, "tests/test_x.py::test_x").changed_symbol == "conftest.db"


def test_pytest_inherited_test_methods(repo):
    base = repo.commit(
        {
            "pkg/codec.py": "def encode(x):\n    return x\n",
            "tests/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef payload():\n    return 1\n"
            ),
            "tests/test_inherit.py": (
                "import pytest\nfrom pkg.codec import encode\n"
                "from somewhere import ExternalMixin\n\n\n"
                "class Base:\n"
                "    @pytest.fixture\n    def base_fix(self):\n        return 1\n\n"
                "    def setup_method(self):\n        pass\n\n"
                "    def test_roundtrip(self, payload, base_fix):\n"
                "        assert encode(payload)\n\n"
                "    def test_overridden(self):\n        pass\n\n\n"
                "class TestJson(Base):\n"
                "    codec = 'json'\n\n"
                "    def test_overridden(self):\n        pass\n\n\n"
                "class TestExternal(ExternalMixin):\n"
                "    def test_own(self):\n        pass\n"
            ),
        }
    )
    result = run_discovery(repo, base, "pytest")
    targets = by_id(result)
    assert set(targets) == {
        "tests/test_inherit.py::TestJson::test_roundtrip",
        "tests/test_inherit.py::TestJson::test_overridden",
        "tests/test_inherit.py::TestExternal::test_own",
    }
    inherited = targets["tests/test_inherit.py::TestJson::test_roundtrip"]
    assert inherited.entry_symbol == "tests.test_inherit.Base.test_roundtrip"
    assert {
        "tests.conftest.payload",
        "tests.test_inherit.Base.base_fix",
        "tests.test_inherit.Base.setup_method",
        "tests.test_inherit.TestJson",  # the collecting class
    } <= set(inherited.lifecycle_dependencies)
    own = targets["tests/test_inherit.py::TestJson::test_overridden"]
    assert own.entry_symbol == "tests.test_inherit.TestJson.test_overridden"
    assert [(n.kind, "ExternalMixin" in n.detail) for n in result.notes] == [
        ("unknown_base_class", True)
    ]

    # Changing the base's test body or the subclass's class attribute selects it.
    head = repo.commit({"pkg/codec.py": "def encode(x):\n    return [x]\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_inherit.py::TestJson::test_roundtrip"}
    head2 = repo.commit(
        {
            "tests/test_inherit.py": (
                "import pytest\nfrom pkg.codec import encode\n"
                "from somewhere import ExternalMixin\n\n\n"
                "class Base:\n"
                "    @pytest.fixture\n    def base_fix(self):\n        return 1\n\n"
                "    def setup_method(self):\n        pass\n\n"
                "    def test_roundtrip(self, payload, base_fix):\n"
                "        assert encode(payload)\n\n"
                "    def test_overridden(self):\n        pass\n\n\n"
                "class TestJson(Base):\n"
                "    codec = 'msgpack'\n\n"
                "    def test_overridden(self):\n        pass\n\n\n"
                "class TestExternal(ExternalMixin):\n"
                "    def test_own(self):\n        pass\n"
            )
        }
    )
    plan = repo.plan(base, head2, [], discover_runners=["pytest"])
    # A class body runs when its module is imported, so the module's other
    # tests (which depend on their module) are selected too.
    assert selected(plan) == {
        "tests/test_inherit.py::TestJson::test_roundtrip",
        "tests/test_inherit.py::TestJson::test_overridden",
        "tests/test_inherit.py::TestExternal::test_own",
    }
    assert rules(plan, "tests/test_inherit.py::TestJson::test_roundtrip") == {"dependency"}


def test_pytest_testpaths_globs_and_dot_prefix(repo):
    files = {
        "tests/unit/test_a.py": "def test_a():\n    pass\n",
        "tests/integ_db/test_b.py": "def test_b():\n    pass\n",
        "tests/integ_web/test_c.py": "def test_c():\n    pass\n",
        "tests/other/test_d.py": "def test_d():\n    pass\n",
        "pytest.ini": "[pytest]\ntestpaths = ./tests/unit tests/integ*\n",
    }
    rev = repo.commit(files)
    result = run_discovery(repo, rev, "pytest")
    assert set(by_id(result)) == {
        "tests/unit/test_a.py::test_a",
        "tests/integ_db/test_b.py::test_b",
        "tests/integ_web/test_c.py::test_c",
    }


def test_pytest_module_hooks_and_nested_class_marks(repo):
    base = repo.commit(
        {
            "tests/conftest.py": ("import pytest\n\n\n@pytest.fixture\ndef db():\n    return {}\n"),
            "tests/test_gen.py": (
                "import pytest\n\n\n"
                "def pytest_generate_tests(metafunc):\n"
                "    if 'case' in metafunc.fixturenames:\n"
                "        metafunc.parametrize('case', [1, 2])\n\n\n"
                "def test_cases(case):\n    pass\n\n\n"
                "@pytest.mark.usefixtures('db')\n"
                "class TestOuter:\n"
                "    class TestInner:\n"
                "        def test_a(self):\n            pass\n"
            ),
        }
    )
    result = run_discovery(repo, base, "pytest")
    targets = by_id(result)
    cases = targets["tests/test_gen.py::test_cases"]
    assert "tests.test_gen.pytest_generate_tests" in cases.lifecycle_dependencies
    # ``case`` comes from the hook, not a fixture: it is still reported as
    # unresolved (conservative), which is documented behaviour.
    assert "fixture:case" in cases.lifecycle_dependencies
    inner = targets["tests/test_gen.py::TestOuter::TestInner::test_a"]
    assert "tests.conftest.db" in inner.lifecycle_dependencies

    head = repo.commit(
        {
            "tests/test_gen.py": (
                "import pytest\n\n\n"
                "def pytest_generate_tests(metafunc):\n"
                "    if 'case' in metafunc.fixturenames:\n"
                "        metafunc.parametrize('case', [1, 2, 3])\n\n\n"
                "def test_cases(case):\n    pass\n\n\n"
                "@pytest.mark.usefixtures('db')\n"
                "class TestOuter:\n"
                "    class TestInner:\n"
                "        def test_a(self):\n            pass\n"
            )
        }
    )
    plan = repo.plan(
        base,
        head,
        [],
        discover_runners=["pytest"],
        discovery_options=DiscoveryOptions(external_fixtures=frozenset({"case"})),
    )
    # The hook runs for every test in the module, so both are selected.
    assert selected(plan) == {
        "tests/test_gen.py::test_cases",
        "tests/test_gen.py::TestOuter::TestInner::test_a",
    }
    r = reason(plan, "tests/test_gen.py::test_cases")
    assert r.changed_symbol == "tests.test_gen.pytest_generate_tests"
    assert [s.kind for s in r.path] == ["lifecycle"]


def test_empty_pytest_ini_wins_over_pyproject(repo):
    rev = repo.commit(
        {
            "pytest.ini": "",
            "pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n',
            "tests/test_default.py": "def test_x():\n    pass\n",
            "tests/check_y.py": "def test_y():\n    pass\n",
        }
    )
    result = run_discovery(repo, rev, "pytest")
    assert result.config["source"] == "pytest.ini"
    assert result.config["python_files"] == ["test_*.py", "*_test.py"]
    assert set(by_id(result)) == {"tests/test_default.py::test_x"}


def test_asv_config_with_inline_comments(repo):
    from diffcone.discovery.asv_static import strip_json_comments

    text = (
        "{\n"
        "  // leading comment\n"
        '  "benchmark_dir": "perf",  // trailing comment\n'
        '  "repo": "https://example.invalid/x", /* block */\n'
        '  "note": "a // not a comment /* nor this */"\n'
        "}\n"
    )
    assert json.loads(strip_json_comments(text)) == {
        "benchmark_dir": "perf",
        "repo": "https://example.invalid/x",
        "note": "a // not a comment /* nor this */",
    }
    rev = repo.commit({"asv.conf.json": text, "perf/b.py": "def time_x():\n    pass\n"})
    result = run_discovery(repo, rev, "asv")
    assert result.config["benchmark_dir"] == "perf"
    assert set(by_id(result)) == {"b.time_x"}
    assert result.notes == []

    rev2 = repo.commit({"asv.conf.json": '{"benchmark_dir": "perf" "repo": "x"}\n'})
    result = run_discovery(repo, rev2, "asv")
    assert result.config["benchmark_dir"] == "benchmarks"
    assert [n.kind for n in result.notes] == ["unparsable_config", "no_benchmarks"]


def test_w27_asv_trailing_commas_parse_as_asv_reads_them(repo):
    """asv reads its configuration as JSON5, so a trailing comma is valid."""
    from diffcone.discovery.asv_static import strip_json_comments

    text = '{\n  "benchmark_dir": "perf", // c\n  "pythons": ["3.12", ],\n  "s": ",]",\n}\n'
    assert json.loads(strip_json_comments(text)) == {
        "benchmark_dir": "perf",
        "pythons": ["3.12"],
        "s": ",]",
    }
    rev = repo.commit({"asv.conf.json": text, "perf/b.py": "def time_x():\n    pass\n"})
    result = run_discovery(repo, rev, "asv")
    assert result.config["benchmark_dir"] == "perf"
    assert set(by_id(result)) == {"b.time_x"}
    assert result.notes == []


def test_w27_asv_conf_jsonc_is_the_configuration(repo):
    """asv reads ``asv.conf.jsonc`` as well as ``asv.conf.json``: its
    ``benchmark_dir`` is where the benchmarks are, and a change to it is a
    runner configuration change."""
    rev = repo.commit(
        {
            "asv_bench/asv.conf.jsonc": '{"benchmark_dir": "perf", // jsonc\n}\n',
            "asv_bench/perf/b.py": "def time_x():\n    pass\n",
        }
    )
    result = run_discovery(repo, rev, "asv")
    assert result.config["source"] == "asv_bench/asv.conf.jsonc"
    assert result.config["benchmark_dir"] == "asv_bench/perf"
    assert set(by_id(result)) == {"b.time_x"}
    assert result.notes == []

    # Both spellings in one directory: asv refuses to choose.
    rev2 = repo.commit({"asv_bench/asv.conf.json": '{"benchmark_dir": "perf"}\n'})
    result = run_discovery(repo, rev2, "asv")
    assert [n.kind for n in result.incomplete] == ["unparsable_config"]


W27_TREE = {
    "pkg/__init__.py": "",
    "pkg/ops.py": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
    "tests/test_ops.py": "from pkg.ops import sub\n\n\ndef test_sub():\n    assert sub(2, 1)\n",
    # Not valid JSON (a missing comma), so its benchmark_dir is unknown.
    "asv.conf.json": '{"version": 1, "benchmark_dir": "perf" "repo": "."}\n',
    "perf/__init__.py": "",
    "perf/bench_ops.py": "from pkg.ops import add\n\n\ndef time_add():\n    add(1, 2)\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_sub.py": "from pkg.ops import sub\n\n\ndef time_sub():\n    sub(2, 1)\n",
}


def test_w27_an_unparsable_asv_config_makes_the_plan_incomplete(repo, capsys):
    """The benchmarks ASV runs live under a ``benchmark_dir`` discovery could
    not read: the plan holds the default directory's benchmarks, and says
    the list may be short (exit 3) instead of passing as complete."""
    base = repo.commit(W27_TREE)
    head = repo.commit(
        {"pkg/ops.py": "def add(a, b):\n    return b + a\n\n\ndef sub(a, b):\n    return a - b\n"}
    )
    args = ["--repo", str(repo.path), "--base", base, "--head", head]
    code = main(["plan", *args, "--discover", "pytest", "--discover", "asv", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 3
    assert data["discovery_incomplete"] is True
    notes = {d["runner"]: [n["kind"] for n in d["notes"]] for d in data["discovery"]}
    assert notes == {"pytest": [], "asv": ["unparsable_config"]}
    # The default directory's benchmarks are still targets.
    ids = {t["runner_id"] for t in data["selected_targets"] + data["unselected_targets"]}
    assert ids == {"tests/test_ops.py::test_sub", "bench_sub.time_sub"}

    code = main(["run", *args, "--discover", "asv", "--dry-run", "--command", "asv run"])
    assert code == 3
    assert "--allow-incomplete-discovery" in capsys.readouterr().err


# Each the only configuration file, and each not parsable.
W27_PYTEST_CONFIGS = {
    "pytest.toml": '[pytest]\npython_files = ["check_*.py"\n',
    "pytest.ini": "[pytest]\npython_files check_*.py\n",
    "pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["check_*.py"\n',
    "tox.ini": "[pytest]\npython_files = check_*.py\n[pytest]\n",
    "setup.cfg": "python_files = check_*.py\n[tool:pytest]\n",
}

INCOMPLETE_W27_PYTEST = {
    "pkg/__init__.py": "",
    "pkg/ops.py": "def add(a, b):\n    return a + b\n",
    "tests/test_ops.py": "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1)\n",
}


@pytest.mark.parametrize("name", sorted(W27_PYTEST_CONFIGS))
def test_w27_an_unparsable_pytest_config_makes_discovery_incomplete(repo, name):
    """pytest stops on a configuration file it cannot parse; one this parser
    cannot read may set testpaths or python_files, so the default rules do
    not vouch for the target list."""
    text = W27_PYTEST_CONFIGS[name]
    rev = repo.commit(
        {
            name: text,
            "tests/test_default.py": "def test_x():\n    pass\n",
            "tests/check_y.py": "def test_y():\n    pass\n",
        }
    )
    result = run_discovery(repo, rev, "pytest")
    assert [n.kind for n in result.incomplete] == ["unparsable_config"]
    assert result.config["source"] == f"{name} (unparsable, defaults used)"
    assert result.config["error"].startswith(f"{name}: ")
    assert set(by_id(result)) == {"tests/test_default.py::test_x"}


def test_w27_an_unparsable_pytest_config_exits_three(repo, capsys):
    base = repo.commit({**INCOMPLETE_W27_PYTEST, "pytest.ini": "[pytest]\ntestpaths\n"})
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    args = ["--repo", str(repo.path), "--base", base, "--head", head, "--discover", "pytest"]
    code = main(["plan", *args, "--format", "json"])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["discovery_incomplete"] is True


def test_snapshot_reads_config_only_on_request(repo):
    rev = repo.commit({"pytest.ini": "[pytest]\n", "pkg/m.py": "x = 1\n"})
    assert read_snapshot(repo.path, rev, ["."]).config_files == {}
    assert set(read_snapshot(repo.path, rev, ["."], with_config=True).config_files) == {
        "pytest.ini"
    }


def test_entry_point_plugin_fixtures_and_hooks_are_resolved(repo):
    files = {
        "pyproject.toml": (
            '[project]\nname = "x"\n[project.entry-points.pytest11]\nmyplug = "myplug"\n'
        ),
        "src/myplug/__init__.py": "from myplug.plugin import mocker, helper\n",
        "src/myplug/plugin.py": (
            "import pytest\n\n\n"
            "@pytest.fixture\ndef mocker():\n    return 1\n\n\n"
            "@pytest.fixture\ndef hidden():\n    return 2\n\n\n"
            "def helper():\n    pass\n\n\n"
            "def pytest_configure(config):\n    pass\n"
        ),
        "tests/test_x.py": (
            "pytest_plugins = ['pytester']\n\n\n"
            "def test_a(mocker):\n    assert mocker\n\n\n"
            "def test_b(hidden):\n    assert hidden\n"
        ),
    }
    rev = repo.commit(files)
    result = run_discovery(repo, rev, "pytest", roots=["src", "tests"])
    assert result.config["entry_point_plugins"] == ["myplug"]
    targets = by_id(result)
    a = targets["tests/test_x.py::test_a"]
    assert "myplug.plugin.mocker" in a.lifecycle_dependencies
    # The hook is defined in the submodule but not imported into the entry
    # module, so pytest never registers it.
    assert "myplug.plugin.pytest_configure" not in a.lifecycle_dependencies
    assert not any(d.startswith("fixture:") for d in a.lifecycle_dependencies)
    # ``hidden`` is not re-exported by the package, so pytest would not see it.
    b = targets["tests/test_x.py::test_b"]
    assert "fixture:hidden" in b.lifecycle_dependencies
    # pytester is pytest's own plugin: no note about it.
    assert [n.kind for n in result.notes] == ["unresolved_fixture"]

    rev2 = repo.commit(
        {
            "pyproject.toml": None,
            "setup.cfg": "[options.entry_points]\npytest11 =\n    myplug = myplug.plugin\n",
        }
    )
    result2 = run_discovery(repo, rev2, "pytest", roots=["src", "tests"])
    assert result2.config["entry_point_plugins"] == ["myplug.plugin"]
    assert (
        "myplug.plugin.hidden" in by_id(result2)["tests/test_x.py::test_b"].lifecycle_dependencies
    )


def test_assignment_style_fixtures(repo):
    rev = repo.commit(
        {
            "tests/conftest.py": (
                "import pytest\n\n\n"
                "def _mocker(pytestconfig):\n    return 1\n\n\n"
                "mocker = pytest.fixture()(_mocker)\n"
                "class_mocker = pytest.fixture(scope='class')(_mocker)\n"
                "named = pytest.fixture(name='alias')(_mocker)\n"
            ),
            "tests/test_x.py": (
                "def test_a(mocker):\n    pass\n\n\ndef test_b(class_mocker, alias):\n    pass\n"
            ),
        }
    )
    result = run_discovery(repo, rev, "pytest")
    targets = by_id(result)
    assert "tests.conftest._mocker" in targets["tests/test_x.py::test_a"].lifecycle_dependencies
    b = targets["tests/test_x.py::test_b"]
    assert "tests.conftest._mocker" in b.lifecycle_dependencies
    assert not any(d.startswith("fixture:") for d in b.lifecycle_dependencies)
    assert result.notes == []


def test_entry_point_reexports_use_original_names_and_filter_hooks(repo):
    rev = repo.commit(
        {
            "pyproject.toml": '[tool.poetry.plugins."pytest11"]\nmyplug = "myplug"\n',
            "src/myplug/__init__.py": (
                "try:\n    from myplug.plugin import mocker as m, pytest_configure\n"
                "except ImportError:\n    pass\n"
                "from myplug.plugin import mocker as m2\n"
            ),
            "src/myplug/plugin.py": (
                "import pytest\n\n\n"
                "@pytest.fixture\ndef mocker():\n    return 1\n\n\n"
                "def pytest_configure(config):\n    pass\n\n\n"
                "def pytest_unconfigure(config):\n    pass\n"
            ),
            "tests/test_x.py": "def test_a(mocker):\n    assert mocker\n",
        }
    )
    result = run_discovery(repo, rev, "pytest", roots=["src", "tests"])
    assert result.config["entry_point_plugins"] == ["myplug"]
    deps = set(by_id(result)["tests/test_x.py::test_a"].lifecycle_dependencies)
    assert "myplug.plugin.mocker" in deps  # aliased import still registers ``mocker``
    assert "myplug.plugin.pytest_configure" in deps  # imported hook
    assert "myplug.plugin.pytest_unconfigure" not in deps  # not imported: not registered
    assert not any(d.startswith("fixture:") for d in deps)


def test_hypothesis_given_arguments_are_not_fixture_requests(repo):
    rev = repo.commit(
        {
            "tests/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef C():\n    return 1\n\n\n"
                "@pytest.fixture\ndef db():\n    return 2\n"
            ),
            "tests/test_h.py": (
                "from hypothesis import given, strategies as st\n\n\n"
                "class TestX:\n"
                "    @given(st.booleans())\n"
                "    def test_positional_fills_last(self, C, tuple_factory):\n        pass\n\n"
                "    @given(container=st.integers())\n"
                "    def test_keyword(self, container, C):\n        pass\n\n"
                "    @given(st.integers(), st.integers())\n"
                "    def test_two_positional(self, db, a, b):\n        pass\n\n\n"
                "@given(x=st.integers())\n"
                "def test_module_level(db, x):\n    pass\n"
            ),
        }
    )
    result = run_discovery(repo, rev, "pytest")
    targets = by_id(result)

    def fixtures(runner_id):
        return {
            d
            for d in targets[runner_id].lifecycle_dependencies
            if d.startswith(("fixture:", "tests.conftest."))
        }

    assert fixtures("tests/test_h.py::TestX::test_positional_fills_last") == {"tests.conftest.C"}
    assert fixtures("tests/test_h.py::TestX::test_keyword") == {"tests.conftest.C"}
    assert fixtures("tests/test_h.py::TestX::test_two_positional") == {"tests.conftest.db"}
    assert fixtures("tests/test_h.py::test_module_level") == {"tests.conftest.db"}
    assert result.notes == []


def test_pytest9_native_toml_table(repo):
    rev = repo.commit(
        {
            "pyproject.toml": '[tool.pytest]\ntestpaths = ["qa"]\npython_files = ["check_*.py"]\n',
            "qa/check_a.py": "def test_a():\n    pass\n",
            "tests/test_b.py": "def test_b():\n    pass\n",
        }
    )
    result = run_discovery(repo, rev, "pytest")
    assert result.config["source"] == "pyproject.toml [tool.pytest]"
    assert set(by_id(result)) == {"qa/check_a.py::test_a"}


def test_addopts_p_plugins_are_resolved_including_pytest_internal_names(repo):
    rev = repo.commit(
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\n"
                'addopts = ["-rfEX", "-p", "pytester", "-pmyplug", '
                '"-p", "no:cacheprovider", "-p", "xdist"]\n'
            ),
            "src/_pytest/__init__.py": "",
            "src/_pytest/pytester.py": (
                "import pytest\n\n\n@pytest.fixture\ndef linecomp():\n    return 1\n"
            ),
            "src/myplug.py": "import pytest\n\n\n@pytest.fixture\ndef mine():\n    return 2\n",
            "tests/test_x.py": "def test_a(linecomp, mine):\n    pass\n",
        }
    )
    result = run_discovery(repo, rev, "pytest", roots=["src", "tests"])
    assert result.config["addopts_plugins"] == ["pytester", "myplug", "xdist"]
    deps = set(by_id(result)["tests/test_x.py::test_a"].lifecycle_dependencies)
    assert {"_pytest.pytester.linecomp", "myplug.mine"} <= deps
    assert not any(d.startswith("fixture:") for d in deps)
    assert [(n.kind, "xdist" in n.detail) for n in result.notes] == [("plugin_out_of_scope", True)]


def test_python_files_patterns_with_directories_match_the_path(repo):
    rev = repo.commit(
        {
            "pyproject.toml": (
                '[tool.pytest.ini_options]\npython_files = ["test_*.py", "testing/python/*.py"]\n'
                'testpaths = ["testing"]\n'
            ),
            "testing/test_a.py": "def test_a():\n    pass\n",
            "testing/python/approx.py": "def test_b():\n    pass\n",
            "testing/other/approx.py": "def test_c():\n    pass\n",
        }
    )
    result = run_discovery(repo, rev, "pytest")
    assert set(by_id(result)) == {"testing/test_a.py::test_a", "testing/python/approx.py::test_b"}


def test_well_known_plugin_fixtures_are_assumed_and_reported(repo):
    rev = repo.commit(
        {
            "pkg/__init__.py": "",
            "tests/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef freezer():\n    return 1\n"
            ),
            "tests/test_a.py": (
                "def test_a(mocker, fake_process):\n    pass\n\n\n"
                "def test_b(mocker, freezer, widget):\n    pass\n"
            ),
        }
    )
    result = run_discovery(repo, rev, "pytest")
    targets = by_id(result)
    a = targets["tests/test_a.py::test_a"]
    assert list(a.lifecycle_dependencies) == ["tests.conftest", "tests.test_a"]  # both assumed
    b = targets["tests/test_a.py::test_b"]
    # A fixture defined in scope wins over the table; an unknown name stays conservative.
    assert list(b.lifecycle_dependencies) == [
        "fixture:widget",
        "tests.conftest",
        "tests.conftest.freezer",
        "tests.test_a",
    ]
    assert [(n.kind, n.detail.split(" ")[1]) for n in result.notes] == [
        ("unresolved_fixture", "'widget'"),
        ("external_fixture", "'fake_process'"),
        ("external_fixture", "'mocker'"),
    ]
    assert "requested by 2 test(s)" in result.notes[2].detail
    assert "assumed from the installed plugin pytest-mock" in result.notes[2].detail
    assert "pytest-subprocess" in result.notes[1].detail

    # Opting out reports them as unresolved again.
    plain = run_discovery(repo, rev, "pytest", well_known_fixtures=False)
    a2 = by_id(plain)["tests/test_a.py::test_a"]
    assert list(a2.lifecycle_dependencies) == [
        "fixture:fake_process",
        "fixture:mocker",
        "tests.conftest",
        "tests.test_a",
    ]
    assert [n.kind for n in plain.notes] == ["unresolved_fixture"] * 3


def test_cli_no_well_known_fixtures(repo, capsys):
    base = repo.commit(
        {
            "pkg/m.py": "def f():\n    return 1\n",
            "tests/test_m.py": "from pkg.m import f\n\n\ndef test_f(mocker):\n    assert f()\n",
        }
    )
    head = repo.commit({"pkg/m.py": "def f():\n    return 2\n"})
    common = [
        "plan",
        "--repo",
        str(repo.path),
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
    ]
    assert main(common) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["selected_targets"][0]["conservative"] is False
    assert report["discovery"][0]["notes"][0]["kind"] == "external_fixture"
    assert main([*common, "--no-well-known-fixtures"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["selected_targets"][0]["conservative"] is True
    assert report["discovery"][0]["notes"][0]["kind"] == "unresolved_fixture"


def test_class_level_mock_patch_injects_into_every_test_method(repo):
    rev = repo.commit(
        {
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef db():\n    return {}\n",
            "tests/test_cls.py": (
                "from unittest import mock\n\n\n"
                "@mock.patch('os.getcwd')\n"
                "class Base:\n"
                # Patched by Base's decorator and again by TestPatched's (mock
                # appends to the same function's patchings): two injected.
                "    def test_inherited(self, m, m2, db):\n        pass\n\n"
                "    def helper(self, db):\n        pass\n\n\n"
                "@mock.patch.object(dict, 'get')\n"
                "class TestPatched(Base):\n"
                "    def test_plain(self, m1, m2, db):\n        pass\n\n"
                "    @mock.patch('os.sep', '/')\n"
                "    @mock.patch('os.name')\n"
                "    def test_stacked(self, m_name, m1, m2, db):\n        pass\n\n"
                "    def test_underscore(self, _, _2):\n        pass\n\n\n"
                "class TestUnpatched:\n"
                "    def test_needs(self, db):\n        pass\n"
            ),
        }
    )
    result = run_discovery(repo, rev, "pytest")
    targets = by_id(result)
    deps = {k: set(v.lifecycle_dependencies) for k, v in targets.items()}
    for node in (
        "TestPatched::test_plain",
        "TestPatched::test_stacked",
        "TestPatched::test_inherited",
    ):
        assert "tests.conftest.db" in deps[f"tests/test_cls.py::{node}"], node
        assert not any(d.startswith("fixture:") for d in deps[f"tests/test_cls.py::{node}"]), node
    assert not any(
        d.startswith("fixture:") for d in deps["tests/test_cls.py::TestPatched::test_underscore"]
    )
    assert "tests.conftest.db" in deps["tests/test_cls.py::TestUnpatched::test_needs"]
    assert [n.kind for n in result.notes] == []


def test_pytest_base_classes_re_exported_by_a_package(repo):
    # pandas's extension tests: ``base/__init__.py`` re-exports the classes of
    # its submodules, and test modules inherit ``base.Backed2DTests`` or
    # import the name from the package.
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def dup(x):\n    return x * 2\n",
            "tests/__init__.py": "",
            "tests/ext/__init__.py": "",
            "tests/ext/base/__init__.py": (
                "from tests.ext.base.dim2 import Backed2DTests, Dim2Tests\n"
            ),
            "tests/ext/base/dim2.py": (
                "from pkg.ops import dup\n\n\n"
                "class Dim2Tests:\n"
                "    def test_shift_2d(self):\n        assert dup(1)\n\n\n"
                "class Backed2DTests(Dim2Tests):\n"
                "    def test_copy_order(self):\n        pass\n"
            ),
            "tests/ext/test_dt.py": (
                "from tests.ext import base\n\n\n"
                "class Test2DCompat(base.Backed2DTests):\n    pass\n"
            ),
            "tests/ext/test_np.py": (
                "from tests.ext.base import Dim2Tests\n\n\nclass TestNp(Dim2Tests):\n    pass\n"
            ),
            # Re-exports that lead in a circle name no class: reported, not a hang.
            "tests/ext/loop.py": "from tests.ext.loop2 import Ghost\n",
            "tests/ext/loop2.py": "from tests.ext.loop import Ghost\n",
            "tests/ext/test_loop.py": (
                "from tests.ext.loop import Ghost\n\n\n"
                "class TestLoop(Ghost):\n    def test_own(self):\n        pass\n"
            ),
        }
    )
    result = run_discovery(repo, base, "pytest")
    targets = by_id(result)
    assert set(targets) == {
        "tests/ext/test_dt.py::Test2DCompat::test_shift_2d",
        "tests/ext/test_dt.py::Test2DCompat::test_copy_order",
        "tests/ext/test_np.py::TestNp::test_shift_2d",
        "tests/ext/test_loop.py::TestLoop::test_own",
    }
    shift = targets["tests/ext/test_dt.py::Test2DCompat::test_shift_2d"]
    assert shift.entry_symbol == "tests.ext.base.dim2.Dim2Tests.test_shift_2d"
    assert [(n.kind, "Ghost" in n.detail) for n in result.notes] == [("unknown_base_class", True)]

    head = repo.commit({"pkg/ops.py": "def dup(x):\n    return x + x\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {
        "tests/ext/test_dt.py::Test2DCompat::test_shift_2d",
        "tests/ext/test_np.py::TestNp::test_shift_2d",
    }


FIXTURE_BINDINGS = {
    "tests/__init__.py": "",
    "tests/other/__init__.py": "",
    "tests/conftest.py": (
        "import pytest\n\n\n@pytest.fixture\ndef box():\n    return 'box'\n\n\nbox2 = box\n"
    ),
    "tests/other/conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef engine():\n    return 'engine'\n\n\n"
        "@pytest.fixture(name='named')\ndef _named_impl():\n    return 'named'\n"
    ),
    "tests/helpers.py": (
        "import pytest\n\n__all__ = ['shared']\n\n\n"
        "@pytest.fixture\ndef shared():\n    return 'shared'\n"
    ),
    "tests/test_x.py": (
        "from tests.helpers import *\n"
        "from tests.other.conftest import engine\n"
        "from tests.other.conftest import engine as motor\n"
        "from tests.other.conftest import _named_impl as alias_of_named\n\n\n"
        "def test_alias(box2):\n    pass\n\n\n"
        "def test_imported(engine):\n    pass\n\n\n"
        "def test_imported_as(motor):\n    pass\n\n\n"
        "def test_explicit_name(named):\n    pass\n\n\n"
        "def test_starred(shared):\n    pass\n\n\n"
        "def test_not_registered(alias_of_named):\n    pass\n"
    ),
}


def test_fixtures_bound_by_alias_or_import_resolve_as_pytest_registers_them(repo):
    """pytest registers a fixture under every name a module binds it to
    (``box2 = box``, ``from pkg.conftest import engine as motor``, a star
    import), unless the fixture names itself (``name=``): then under that
    name only."""
    rev = repo.commit(FIXTURE_BINDINGS)
    targets = by_id(run_discovery(repo, rev, "pytest"))

    def deps(test):
        return set(targets[f"tests/test_x.py::{test}"].lifecycle_dependencies)

    assert "tests.conftest.box" in deps("test_alias")
    assert "tests.other.conftest.engine" in deps("test_imported")
    assert "tests.other.conftest.engine" in deps("test_imported_as")
    assert "tests.other.conftest._named_impl" in deps("test_explicit_name")
    assert "tests.helpers.shared" in deps("test_starred")
    # A fixture that names itself is not registered under the alias.
    assert "fixture:alias_of_named" in deps("test_not_registered")
    for test in ("test_alias", "test_imported", "test_imported_as", "test_explicit_name"):
        assert not any(d.startswith("fixture:") for d in deps(test)), test


def test_a_fixture_reached_by_alias_or_import_selects_only_its_tests(repo):
    """Before, such a fixture was unresolved and its tests were selected on
    every change, as test_not_registered (whose name pytest does not
    register) still is (regression for the narrowing)."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            **FIXTURE_BINDINGS,
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
            "pkg/__init__.py": "",
            "pkg/other.py": "X = 1\n",
        }
    )
    T = "tests/test_x.py::"
    always = T + "test_not_registered"

    def plan_for(path, old, new):
        repo.git("reset", "-q", "--hard", base)  # each edit on its own
        head = repo.commit({path: FIXTURE_BINDINGS.get(path, "X = 1\n").replace(old, new)})
        return repo.plan(base, head, [bench], discover_runners=["pytest"])

    plan = plan_for("tests/conftest.py", "'box'", "'b'")
    assert selected(plan) == {T + "test_alias", always}
    plan = plan_for("tests/other/conftest.py", "'engine'", "'e'")
    assert selected(plan) == {T + "test_imported", T + "test_imported_as", always}
    plan = plan_for("pkg/other.py", "1", "2")
    assert selected(plan) == {always}
    assert "bench.time_noop" in unselected(plan)


PARAMETRIZED_CLOSURE = {
    "tests/__init__.py": "",
    "tests/conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef engine(dep):\n    return 'engine'\n\n\n"
        "@pytest.fixture\ndef dep():\n    return 'dep'\n"
    ),
    "tests/test_w.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef set_engine(engine, ext):\n    return engine, ext\n\n\n"
        "@pytest.mark.parametrize('engine, ext', [('x', '.x')])\n"
        "@pytest.mark.usefixtures('set_engine')\n"
        "class TestWriter:\n    def test_both(self):\n        pass\n\n\n"
        "@pytest.mark.parametrize('ext', ['.y'])\n"
        "def test_ext_only(set_engine):\n    pass\n\n\n"
        "@pytest.mark.parametrize('ext', ['.z'], indirect=True)\n"
        "def test_indirect(set_engine):\n    pass\n"
    ),
}


def test_parametrized_names_are_parameters_throughout_the_closure(repo):
    """pytest hands a directly parametrized name to every fixture in the
    test's closure, replacing a fixture of that name and pruning what it
    requests; an ``indirect`` name is still a fixture request."""
    rev = repo.commit(PARAMETRIZED_CLOSURE)
    targets = by_id(run_discovery(repo, rev, "pytest"))

    def deps(test):
        return set(targets[f"tests/test_w.py::{test}"].lifecycle_dependencies)

    both = deps("TestWriter::test_both")
    assert "tests.test_w.set_engine" in both
    assert not both & {"tests.conftest.engine", "tests.conftest.dep"}
    assert not any(d.startswith("fixture:") for d in both)
    ext_only = deps("test_ext_only")
    assert {"tests.conftest.engine", "tests.conftest.dep"} <= ext_only
    assert not any(d.startswith("fixture:") for d in ext_only)
    assert "fixture:ext" in deps("test_indirect")


def test_a_fixture_replaced_by_parametrize_does_not_select_its_test(repo):
    """Before, ``set_engine``'s parametrized requests were unresolved and its
    tests were selected on every change, as test_indirect (whose ``ext`` is
    a fixture nobody defines) still is (regression for the narrowing)."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            **PARAMETRIZED_CLOSURE,
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
            "pkg/__init__.py": "",
            "pkg/other.py": "X = 1\n",
        }
    )
    T = "tests/test_w.py::"
    always = T + "test_indirect"

    def plan_for(path, old, new):
        repo.git("reset", "-q", "--hard", base)  # each edit on its own
        head = repo.commit({path: PARAMETRIZED_CLOSURE.get(path, "X = 1\n").replace(old, new)})
        return repo.plan(base, head, [bench], discover_runners=["pytest"])

    for old, new in (("'engine'", "'e'"), ("'dep'", "'d'")):
        plan = plan_for("tests/conftest.py", old, new)
        assert selected(plan) == {T + "test_ext_only", always}, old
        assert T + "TestWriter::test_both" in unselected(plan)
    plan = plan_for("pkg/other.py", "1", "2")
    assert selected(plan) == {always}
    assert "bench.time_noop" in unselected(plan)


INHERITED_CLASS = {
    "tests/__init__.py": "",
    "tests/base/__init__.py": "",
    "tests/conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef data():\n    return 'conftest-data'\n\n\n"
        "@pytest.fixture\ndef marked():\n    return 'marked'\n"
    ),
    "tests/base/setitem.py": (
        "import pytest\n\n\n"
        "@pytest.mark.usefixtures('marked')\n"
        "@pytest.mark.parametrize('ext', ['.x'])\n"
        "class BaseSetitem:\n"
        "    def setup_method(self):\n        self.ready = True\n\n"
        "    @pytest.fixture\n    def full_indexer(self, ext):\n        return 'indexer' + ext\n\n"
        "    @pytest.fixture\n    def data(self):\n        return 'base-data'\n\n"
        "    def test_inherited(self, full_indexer):\n        assert self.ready\n"
    ),
    "tests/test_numpy.py": (
        "import pytest\n\nfrom tests.base.setitem import BaseSetitem\n\n\n"
        "class TestNumpy(BaseSetitem):\n"
        "    @pytest.fixture\n    def data(self, data):\n        return 'sub:' + data\n\n"
        "    def test_override(self, full_indexer, data):\n        pass\n"
    ),
    "tests/test_imported.py": "from tests.base.setitem import BaseSetitem as TestImported\n",
}


def test_a_test_class_inherits_fixtures_and_marks_from_bases_anywhere(repo):
    """pytest reads a class's fixtures from ``dir(cls)`` and its marks along
    the MRO, wherever the bases are defined; a subclass fixture requesting
    its own name reaches past the base's to the conftest's; an imported
    class is collected with its fixtures, marks and xunit setup."""
    rev = repo.commit(INHERITED_CLASS)
    targets = by_id(run_discovery(repo, rev, "pytest"))
    base = "tests.base.setitem.BaseSetitem"
    for test in (
        "tests/test_numpy.py::TestNumpy::test_inherited",
        "tests/test_numpy.py::TestNumpy::test_override",
        "tests/test_imported.py::TestImported::test_inherited",
    ):
        deps = set(targets[test].lifecycle_dependencies)
        assert {f"{base}.full_indexer", "tests.conftest.marked"} <= deps, test
        assert not any(d.startswith("fixture:") for d in deps), test
        assert f"{base}.data" not in deps, test
    override = set(targets["tests/test_numpy.py::TestNumpy::test_override"].lifecycle_dependencies)
    assert {"tests.test_numpy.TestNumpy.data", "tests.conftest.data"} <= override
    imported = set(
        targets["tests/test_imported.py::TestImported::test_inherited"].lifecycle_dependencies
    )
    assert f"{base}.setup_method" in imported


def test_fixtures_and_marks_from_a_base_elsewhere_select_their_tests(repo):
    """Before, the base's ``full_indexer`` was unresolved (its tests selected
    on every change, the narrowing this guards) and the base's
    ``usefixtures('marked')`` was no dependency at all (a miss)."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            **INHERITED_CLASS,
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
            "pkg/__init__.py": "",
            "pkg/other.py": "X = 1\n",
        }
    )
    every = {
        "tests/test_numpy.py::TestNumpy::test_inherited",
        "tests/test_numpy.py::TestNumpy::test_override",
        "tests/test_imported.py::TestImported::test_inherited",
    }

    def plan_for(path, old, new):
        repo.git("reset", "-q", "--hard", base)  # each edit on its own
        head = repo.commit({path: INHERITED_CLASS.get(path, "X = 1\n").replace(old, new)})
        return repo.plan(base, head, [bench], discover_runners=["pytest"])

    assert selected(plan_for("tests/conftest.py", "'marked'", "'m'")) == every
    assert selected(plan_for("tests/conftest.py", "'conftest-data'", "'d'")) == {
        "tests/test_numpy.py::TestNumpy::test_override"
    }
    assert selected(plan_for("tests/base/setitem.py", "'indexer'", "'i'")) == every
    plan = plan_for("pkg/other.py", "1", "2")
    assert selected(plan) == set()
    assert "bench.time_noop" in unselected(plan)


STORED_MARKS = {
    "tests/__init__.py": "",
    "tests/conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef db():\n    return 'db'\n\n\n"
        "@pytest.fixture\ndef net():\n    return 'net'\n\n\n"
        "@pytest.fixture\ndef clock():\n    return 'clock'\n"
    ),
    "tests/marks.py": (
        "import pytest\n\n"
        "needs_net = pytest.mark.usefixtures('net')\n"
        "needs_both = [pytest.mark.usefixtures('db'), needs_net]\n"
    ),
    "tests/test_local.py": (
        "import pytest\n\n"
        "needs_db = pytest.mark.usefixtures('db')\n"
        "also_db = needs_db\n"
        "pytestmark = [pytest.mark.usefixtures('clock')]\n\n\n"
        "@needs_db\ndef test_local():\n    pass\n\n\n"
        "@also_db\ndef test_alias():\n    pass\n\n\n"
        "def test_plain():\n    pass\n"
    ),
    "tests/test_imported.py": (
        "from tests import marks\n"
        "from tests.marks import needs_both, needs_net\n\n"
        "pytestmark = needs_both\n\n\n"
        "@needs_net\nclass TestNet:\n    def test_net(self):\n        pass\n\n\n"
        "@marks.needs_net\ndef test_dotted():\n    pass\n"
    ),
}


def test_a_mark_stored_in_a_variable_applies_where_it_is_used(repo):
    """``skip_pyarrow = pytest.mark.usefixtures("pyarrow_skip")`` then
    ``@skip_pyarrow``: the stored mark is the decorator, here, through an
    alias, an import, ``module.name`` or a ``pytestmark`` list."""
    rev = repo.commit(STORED_MARKS)
    targets = by_id(run_discovery(repo, rev, "pytest"))

    def deps(test):
        return set(targets[test].lifecycle_dependencies)

    db, net, clock = "tests.conftest.db", "tests.conftest.net", "tests.conftest.clock"
    assert {db, clock} <= deps("tests/test_local.py::test_local")
    assert {db, clock} <= deps("tests/test_local.py::test_alias")
    assert db not in deps("tests/test_local.py::test_plain")
    assert {db, net} <= deps("tests/test_imported.py::TestNet::test_net")
    assert {db, net} <= deps("tests/test_imported.py::test_dotted")


def test_a_fixture_applied_by_a_stored_mark_selects_its_tests(repo):
    """Before, a stored mark was no dependency: changing the fixture selected
    none of its tests (a miss)."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            **STORED_MARKS,
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
            "pkg/__init__.py": "",
            "pkg/other.py": "X = 1\n",
        }
    )

    def plan_for(path, old, new):
        repo.git("reset", "-q", "--hard", base)  # each edit on its own
        head = repo.commit({path: STORED_MARKS.get(path, "X = 1\n").replace(old, new)})
        return repo.plan(base, head, [bench], discover_runners=["pytest"])

    imported = {"tests/test_imported.py::TestNet::test_net", "tests/test_imported.py::test_dotted"}
    assert selected(plan_for("tests/conftest.py", "'db'", "'d'")) == imported | {
        "tests/test_local.py::test_local",
        "tests/test_local.py::test_alias",
    }
    assert selected(plan_for("tests/conftest.py", "'net'", "'n'")) == imported
    plan = plan_for("pkg/other.py", "1", "2")
    assert selected(plan) == set()
    assert "bench.time_noop" in unselected(plan)


IMPLICIT_REQUESTS = {
    "tests/__init__.py": "",
    "tests/conftest.py": (
        "import pytest\n\nENABLED = True\n\n\n"
        "@pytest.fixture(scope='session')\n"
        "def tmp_path_factory(request):\n    return 'factory'\n\n\n"
        "@pytest.fixture\ndef anyio_backend():\n    return 'asyncio'\n\n\n"
        "@pytest.fixture(autouse=ENABLED)\ndef guard():\n    return 'guard'\n\n\n"
        "@pytest.fixture(autouse=False)\ndef off():\n    return 'off'\n"
    ),
    "tests/test_paths.py": ("def test_tmp(tmp_path):\n    pass\n\n\ndef test_plain():\n    pass\n"),
}


def test_overrides_of_fixtures_provided_outside_and_conditional_autouse_apply(repo):
    """pytest's ``tmp_path`` requests ``tmp_path_factory``, so an override of
    it is a dependency of tests using ``tmp_path``; anyio's plugin adds
    ``anyio_backend`` to async tests, so an override of it is a dependency of
    every test that can see it. ``autouse=ENABLED`` is autouse unless
    literally false."""
    rev = repo.commit(IMPLICIT_REQUESTS)
    targets = by_id(run_discovery(repo, rev, "pytest"))
    for test in ("tests/test_paths.py::test_tmp", "tests/test_paths.py::test_plain"):
        deps = set(targets[test].lifecycle_dependencies)
        assert {"tests.conftest.anyio_backend", "tests.conftest.guard"} <= deps, test
        assert "tests.conftest.off" not in deps
    tmp = set(targets["tests/test_paths.py::test_tmp"].lifecycle_dependencies)
    plain = set(targets["tests/test_paths.py::test_plain"].lifecycle_dependencies)
    assert "tests.conftest.tmp_path_factory" in tmp
    assert "tests.conftest.tmp_path_factory" not in plain


def test_a_fixture_requested_from_outside_selects_the_tests_that_see_it(repo):
    """Before, changing these fixtures selected nothing (misses)."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            **IMPLICIT_REQUESTS,
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    tmp = "tests/test_paths.py::test_tmp"
    both = {tmp, "tests/test_paths.py::test_plain"}
    for old, new, expected in (
        ("'factory'", "'f'", {tmp}),
        ("'asyncio'", "'trio'", both),
        ("'guard'", "'g'", both),
    ):
        repo.git("reset", "-q", "--hard", base)  # each edit on its own
        head = repo.commit(
            {"tests/conftest.py": IMPLICIT_REQUESTS["tests/conftest.py"].replace(old, new)}
        )
        plan = repo.plan(base, head, [bench], discover_runners=["pytest"])
        assert selected(plan) == expected, old
        assert "bench.time_noop" in unselected(plan)


NESTED_PLUGINS = {
    "tests/__init__.py": "",
    "tests/fixtures/__init__.py": "pytest_plugins = ['tests.fixtures.repo']\n",
    "tests/fixtures/repo.py": (
        "import pytest\n\npytest_plugins = ['tests.fixtures.http']\n\n\n"
        "@pytest.fixture(autouse=True)\ndef repository():\n    return 'repo'\n"
    ),
    "tests/fixtures/http.py": (
        "import pytest\n\n\n@pytest.fixture\ndef http():\n    return 'http'\n"
    ),
    "tests/conftest.py": "pytest_plugins = ['tests.fixtures']\n",
    "tests/test_x.py": "def test_plain():\n    pass\n\n\ndef test_http(http):\n    pass\n",
}


def test_plugins_declared_by_a_plugin_are_loaded_too(repo):
    """pytest registers a module plugin's own ``pytest_plugins`` (poetry's
    conftest loads a package whose ``__init__`` lists its fixture modules).
    Before, their fixtures were unseen: the autouse one no dependency (a
    miss), the requested one unresolved."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            **NESTED_PLUGINS,
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    targets = by_id(run_discovery(repo, base, "pytest"))
    plain = set(targets["tests/test_x.py::test_plain"].lifecycle_dependencies)
    http = set(targets["tests/test_x.py::test_http"].lifecycle_dependencies)
    assert "tests.fixtures.repo.repository" in plain
    assert {"tests.fixtures.repo.repository", "tests.fixtures.http.http"} <= http
    assert not any(d.startswith("fixture:") for d in plain | http)

    path = "tests/fixtures/repo.py"
    head = repo.commit({path: NESTED_PLUGINS[path].replace("'repo'", "'r'")})
    plan = repo.plan(base, head, [bench], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_x.py::test_plain", "tests/test_x.py::test_http"}
    assert "bench.time_noop" in unselected(plan)


def _notes(repo, files):
    rev = repo.commit(files)
    plan = repo.plan(rev, rev, [], discover_runners=["pytest"])
    return [(n.kind, n.path) for d in plan.discovery for n in d.notes]


def test_a_library_test_client_import_is_not_an_incomplete_target_list(repo):
    """``from fastapi.testclient import TestClient`` matches ``Test*``, but the
    class defines ``__init__`` and pytest never collects it: no note, so the
    plan does not exit 3 (strata imports it in 50 test modules)."""
    notes = _notes(
        repo,
        {
            "tests/__init__.py": "",
            "tests/test_api.py": (
                "from fastapi.testclient import TestClient\n"
                "from mylib.testing import TestHarness\n\n\n"
                "def test_api():\n    assert TestClient\n"
            ),
        },
    )
    kinds = [path for kind, path in notes if kind == "imported_test_out_of_scope"]
    # One a library is not known to ship as a helper is still reported.
    assert kinds == ["tests/test_api.py"]
    assert not any("TestClient" in str(n) for n in notes if n[0] == "imported_test_out_of_scope")


def test_a_conftest_pytest_does_not_load_is_not_read(repo):
    """With ``testpaths = ["tests"]``, a sibling package's conftest is never
    imported: its unnameable directory is no incomplete target list, and its
    session hooks are not dependencies. The root conftest, above the
    testpath, is loaded and still counts."""
    hook = "def pytest_collection_modifyitems(items):\n    items.sort(key=str{})\n"
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "conftest.py": hook.format(""),
            "packages/my-pkg/tests/conftest.py": hook.format(""),
            "tests/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    plan = repo.plan(base, base, [bench], discover_runners=["pytest"])
    assert not plan.incomplete_discovery
    sibling = repo.commit({"packages/my-pkg/tests/conftest.py": hook.format(", reverse=True")})
    plan = repo.plan(base, sibling, [bench], discover_runners=["pytest"])
    assert selected(plan) == set()
    root = repo.commit({"conftest.py": hook.format(", reverse=True")})
    plan = repo.plan(sibling, root, [bench], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a"}


def test_ignored_paths_are_not_targets(repo):
    """``--ignore`` and ``--ignore-glob`` keep pytest from collecting a path;
    its tests were targets with no recording (always selected), and ``run``
    then reported them as selected but not collected (strata's CI ignores
    twelve files that need external services)."""
    rev = repo.commit(
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\naddopts = '--ignore-glob=tests/*_live.py'\n"
            ),
            "tests/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "tests/test_s3.py": "def test_s3():\n    pass\n",
            "tests/test_db_live.py": "def test_live():\n    pass\n",
            "tests/slow/__init__.py": "",
            "tests/slow/test_big.py": "def test_big():\n    pass\n",
        }
    )
    options = DiscoveryOptions(runner_args=("--ignore=tests/test_s3.py", "--ignore", "tests/slow"))
    plan = repo.plan(rev, rev, [], discover_runners=["pytest"], discovery_options=options)
    assert {t.runner_id for t in plan.targets} == {"tests/test_a.py::test_a"}


ARG_PATH_FILES = {
    "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
    "src/pkg/__init__.py": "",
    "tests/__init__.py": "",
    "tests/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": "def test_b():\n    pass\n",
    "tests/e2e/__init__.py": "",
    "tests/e2e/check_flow.py": "def test_flow():\n    pass\n",
}


def test_paths_given_to_pytest_replace_testpaths(repo):
    """strata's e2e and R jobs name their test files: pytest collects only
    those (a file named there whatever ``python_files`` says), so the other
    tests are no targets the run would report as selected but not
    collected."""
    rev = repo.commit(ARG_PATH_FILES)
    args = ("-n", "auto", "tests/test_a.py", "tests/e2e/check_flow.py", "-v", "--tb=short")
    plan = repo.plan(
        rev, rev, [], discover_runners=["pytest"],
        discovery_options=DiscoveryOptions(runner_args=args),
    )  # fmt: skip
    assert {t.runner_id for t in plan.targets} == {
        "tests/test_a.py::test_a",
        "tests/e2e/check_flow.py::test_flow",
    }
    assert not plan.incomplete_discovery


def test_a_path_after_an_option_that_may_take_it_is_reported(repo):
    """``--cov src``: ``src`` is the option's value, not a path to collect;
    a token that cannot be told apart narrows nothing and is reported."""
    rev = repo.commit(ARG_PATH_FILES)
    for args in (("--cov", "src", "tests/"), ("tests/test_a.py::test_a",)):
        plan = repo.plan(
            rev, rev, [], discover_runners=["pytest"],
            discovery_options=DiscoveryOptions(runner_args=args),
        )  # fmt: skip
        assert "tests/test_b.py::test_b" in {t.runner_id for t in plan.targets}
        assert [n.kind for n in plan.incomplete_discovery] == ["unmodelled_runner_option"]


def test_ignore_never_applies_to_where_pytest_starts(repo):
    """pytest never ignores an initial path (a run argument, or a
    ``testpaths`` entry), only what it finds below one."""
    rev = repo.commit(
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\naddopts = '--ignore=tests/integration'\n"
                "testpaths = ['tests/unit', 'tests/integration']\n"
            ),
            "tests/__init__.py": "",
            "tests/unit/__init__.py": "",
            "tests/unit/test_u.py": "def test_u():\n    pass\n",
            "tests/integration/__init__.py": "",
            "tests/integration/test_x.py": "def test_i():\n    pass\n",
        }
    )
    for args in ((), ("tests/integration",), ("tests/integration/test_x.py",)):
        plan = repo.plan(
            rev, rev, [], discover_runners=["pytest"],
            discovery_options=DiscoveryOptions(runner_args=args),
        )  # fmt: skip
        assert "tests/integration/test_x.py::test_i" in {t.runner_id for t in plan.targets}, args


def test_a_path_argument_discovery_cannot_match_keeps_testpaths(repo):
    rev = repo.commit(ARG_PATH_FILES)
    args = ("tests/test_a.py", str(repo.path / "tests" / "test_b.py"))
    plan = repo.plan(
        rev, rev, [], discover_runners=["pytest"],
        discovery_options=DiscoveryOptions(runner_args=args),
    )  # fmt: skip
    assert "tests/test_b.py::test_b" in {t.runner_id for t in plan.targets}
    assert [n.kind for n in plan.incomplete_discovery] == ["unmodelled_runner_option"]


# --------------------------------------------------------------------------- pytest configuration


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pytest.toml", '[pytest]\npython_files = ["check_*.py"]\n'),
        (".pytest.toml", '[pytest]\npython_files = ["check_*.py"]\n'),
        (".pytest.ini", "[pytest]\npython_files = check_*.py\n"),
    ],
)
def test_d2_pytest_9_config_files_are_read_first(repo, name, text):
    rev = repo.commit(
        {
            name: text,
            # Lower precedence: must be ignored.
            "pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["test_*.py"]\n',
            "tests/check_a.py": "def test_a():\n    pass\n",
            "tests/test_b.py": "def test_b():\n    pass\n",
        }
    )
    plan = repo.plan(rev, rev, [], discover_runners=["pytest"])
    assert {t.runner_id for t in plan.targets} == {"tests/check_a.py::test_a"}


def test_d8_quoted_addopts_lose_their_quotes(repo):
    rev = repo.commit(
        {
            "pytest.ini": '[pytest]\naddopts = --doctest-glob="*.rst"\n',
            "docs/guide.rst": ">>> 1 + 1\n2\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
        }
    )
    plan = repo.plan(rev, rev, [], discover_runners=["pytest"])
    assert "docs/guide.rst::guide.rst" in {t.runner_id for t in plan.targets}


@pytest.mark.parametrize(
    "files, args, expected",
    [
        # ``-o addopts=`` on the command line drops the configured addopts
        # (collection_check.py --clean-addopts collects so).
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--ignore=tests/slow'\n"},
            ("-o", "addopts="),
            {"tests/test_a.py::test_a", "tests/slow/test_b.py::test_b"},
        ),
        (
            {
                "pyproject.toml": (
                    "[tool.pytest.ini_options]\naddopts = '-o python_files=check_*.py'\n"
                )
            },
            (),
            {"tests/check_c.py::test_c"},
        ),
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests/slow']\n"},
            ("--override-ini=testpaths=tests",),
            {"tests/test_a.py::test_a", "tests/slow/test_b.py::test_b"},
        ),
        # A path in addopts is an initial path like one on the command line.
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = 'tests/check_c.py'\n"},
            (),
            {"tests/check_c.py::test_c"},
        ),
    ],
    ids=["clear_addopts", "python_files", "testpaths", "addopts_path"],
)
def test_dsc11_ini_overrides_and_addopts_paths_are_modelled(repo, files, args, expected):
    rev = repo.commit(
        {
            **files,
            "tests/test_a.py": T.format("a"),
            "tests/check_c.py": T.format("c"),
            "tests/slow/__init__.py": "",
            "tests/slow/test_b.py": T.format("b"),
        }
    )
    plan = _plan_with_options(repo, rev, rev, runner_args=args)
    assert _targets(plan) == expected
    assert not plan.incomplete_discovery


def test_d12_a_configured_collecting_plugin_makes_discovery_incomplete(repo):
    _, notes = _targets_and_notes(
        repo,
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntyping_checkers = ["mypy"]\n',
            "tests/test_a.py": "def test_a():\n    pass\n",
            "tests/test_types.md": "# cases\n",
        },
    )
    assert "plugin_collects_files" in notes


@pytest.mark.parametrize(
    "files, note",
    [
        # (``-o`` overrides and paths in addopts are modelled: see the
        # initial-paths tests.)
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-c other.ini'\n"},
            "unmodelled_runner_option",
        ),
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--rootdir=tests'\n"},
            "unmodelled_runner_option",
        ),
        (
            {
                "tests/plug.py": "def pytest_collect_file(parent, file_path):\n    return None\n",
                "tests/conftest.py": "pytest_plugins = ['tests.plug']\n",
            },
            "plugin_collects_files",
        ),
    ],
)
def test_d22_unmodelled_collection_is_reported(repo, files, note):
    _, notes = _targets_and_notes(
        repo, {"tests/__init__.py": "", "tests/test_a.py": "def test_a():\n    pass\n", **files}
    )
    assert note in notes


# ------------------------------------------------------------ initial paths, testpaths and ignores


def test_d7_a_missing_testpaths_entry_falls_back_to_the_rootdir(repo):
    targets, _ = _targets_and_notes(
        repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = test\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
        },
    )
    assert targets == {"tests/test_a.py::test_a"}


def test_d7_a_file_named_in_testpaths_bypasses_python_files(repo):
    targets, _ = _targets_and_notes(
        repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests checks/smoke.py\n",
            "checks/smoke.py": "def test_s():\n    pass\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
        },
    )
    assert targets == {"tests/test_a.py::test_a", "checks/smoke.py::test_s"}


HOOK = "def pytest_collection_modifyitems(config, items):\n    {}\n"
SKIP_ALL = "import pytest\n    for i in items:\n        i.add_marker(pytest.mark.skip)"


@pytest.mark.parametrize(
    "files, args",
    [
        ({}, ("--ignore=tests_legacy",)),
        ({}, ("--ignore", "tests_legacy")),
        ({"pyproject.toml": "[tool.pytest.ini_options]\nnorecursedirs = ['tests_legacy']\n"}, ()),
    ],
    ids=["ignore", "ignore_apart", "norecursedirs"],
)
def test_dsc3_a_test_dir_conftest_loads_at_startup_whatever_ignores_say(repo, files, args):
    """pytest imports ``<initial path>/test*/conftest.py`` before collecting
    (``_set_initial_conftests``), so its session hooks apply to every test."""
    base = repo.commit(
        {
            **BENCH,
            **files,
            "tests/test_a.py": T.format("a"),
            "tests_legacy/conftest.py": HOOK.format("pass"),
            "tests_legacy/test_l.py": T.format("l"),
        }
    )
    head = repo.commit({"tests_legacy/conftest.py": HOOK.format(SKIP_ALL)})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET], runner_args=args)
    assert _targets(plan) == {"tests/test_a.py::test_a", "bench.time_noop"}
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert not plan.incomplete_discovery


def test_dsc3_a_nested_dir_conftest_does_not_load_at_startup(repo):
    """Only ``test*`` directories directly in an initial path load at
    startup: an ignored ``legacy/`` (no ``test`` prefix) stays unloaded."""
    base = repo.commit(
        {
            "tests/test_a.py": T.format("a"),
            "legacy/conftest.py": HOOK.format("pass"),
            "legacy/test_l.py": T.format("l"),
        }
    )
    head = repo.commit({"legacy/conftest.py": HOOK.format(SKIP_ALL)})
    plan = _plan_with_options(repo, base, head, runner_args=("--ignore=legacy",))
    assert _targets(plan) == {"tests/test_a.py::test_a"}
    assert selected(plan) == set()


AUTO_ENV = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef env():\n    {}\n"


def test_dsc4_norecursedirs_never_applies_to_an_initial_path(repo):
    base = repo.commit(
        {
            **BENCH,
            "pyproject.toml": "[tool.pytest.ini_options]\nnorecursedirs = ['integration']\n",
            "tests/test_a.py": T.format("a"),
            "integration/conftest.py": AUTO_ENV.format("pass"),
            "integration/test_i.py": T.format("i"),
            "integration/integration/test_deeper.py": T.format("deeper"),
        }
    )
    head = repo.commit({"integration/conftest.py": AUTO_ENV.format("raise RuntimeError")})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET], runner_args=("integration",))
    # Below the initial path the rule applies again.
    assert _targets(plan) == {"integration/test_i.py::test_i", "bench.time_noop"}
    assert selected(plan) == {"integration/test_i.py::test_i"}


UNIGNORE = (
    "import sys\n\n\ndef pytest_ignore_collect(collection_path, config):\n"
    "    if sys.version_info < (3, 8) and 'py38' in str(collection_path):\n"
    "        return True\n"
    "    return {}\n"
)


@pytest.mark.parametrize(
    "files, args",
    [
        ({}, ("--ignore=tests/test_s3.py",)),
        (
            {
                "pyproject.toml": (
                    "[tool.pytest.ini_options]\naddopts = '--ignore-glob=tests/*_s3.py'\n"
                )
            },
            (),
        ),
    ],
    ids=["ignore", "ignore_glob"],
)
def test_dsc6_an_ignore_hook_returning_false_overrides_ignores(repo, files, args):
    """pytest_ignore_collect is firstresult and conftests come first: False
    collects what --ignore would skip."""
    base = repo.commit(
        {
            **BENCH,
            **files,
            "conftest.py": UNIGNORE.format("False"),
            "tests/test_a.py": T.format("a"),
            "tests/test_s3.py": "from lib import f\n\n\ndef test_s3():\n    assert f()\n",
            "lib.py": "def f():\n    return 1\n",
        }
    )
    head = repo.commit({"lib.py": "def f():\n    return 0\n"})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET], runner_args=args)
    assert selected(plan) == {"tests/test_s3.py::test_s3"}
    # A hook that only ever says True (or nothing) leaves the ignores alone.
    quiet = repo.commit({"conftest.py": UNIGNORE.format("None")})
    plan = _plan_with_options(repo, quiet, quiet, runner_args=args)
    assert _targets(plan) == {"tests/test_a.py::test_a"}


def test_dsc6_an_ignore_hook_applies_below_its_conftest_only(repo):
    rev = repo.commit(
        {
            "a/conftest.py": UNIGNORE.format("False"),
            "a/test_x.py": T.format("ax"),
            "b/test_x.py": T.format("bx"),
            "a/test_y.py": T.format("ay"),
            "b/test_y.py": T.format("by"),
        }
    )
    plan = _plan_with_options(
        repo, rev, rev, runner_args=("--ignore=a/test_y.py", "--ignore=b/test_y.py")
    )
    assert _targets(plan) == {
        "a/test_x.py::test_ax",
        "b/test_x.py::test_bx",
        "a/test_y.py::test_ay",
    }


def test_dsc6_an_ignore_hook_in_a_plugin_is_reported(repo):
    rev = repo.commit(
        {
            "plug.py": UNIGNORE.format("False"),
            "conftest.py": "pytest_plugins = ['plug']\n",
            "tests/test_a.py": T.format("a"),
        }
    )
    plan = _plan_with_options(repo, rev, rev)
    assert [(n.kind, n.path) for n in plan.incomplete_discovery] == [
        ("plugin_collects_files", "plug.py")
    ]


def test_dsc7_dot_is_an_initial_path(repo):
    base = repo.commit(
        {
            **BENCH,
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "tests/test_a.py": T.format("a"),
            "other/__init__.py": "",
            "other/lib.py": "def f():\n    return 1\n",
            "other/test_o.py": "from other.lib import f\n\n\ndef test_o():\n    assert f() == 1\n",
        }
    )
    head = repo.commit({"other/lib.py": "def f():\n    return 2\n"})
    for dot in (".", "./"):
        plan = _plan_with_options(repo, base, head, [BENCH_TARGET], runner_args=(dot,))
        assert selected(plan) == {"other/test_o.py::test_o"}, dot
        assert "tests/test_a.py::test_a" in unselected(plan)
        assert not plan.incomplete_discovery


@pytest.mark.parametrize(
    "files, args",
    [
        ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--ignore tests/slow'\n"}, ()),
        ({}, ("--ignore", "tests/slow")),
        ({}, ("--ignore-glob", "tests/slow/*")),
        ({}, ("--deselect", "tests/slow/test_b.py::test_b", "--ignore=tests/slow")),
        ({}, ("--doctest-glob", "tests/*.txt", "--ignore=tests/slow")),
    ],
)
def test_dsc9_an_option_value_written_apart_is_not_a_path(repo, files, args):
    """The value of an option that always takes one is no initial path, and
    no ambiguity: the plan is complete and run does not refuse."""
    rev = repo.commit(
        {
            **files,
            "tests/test_a.py": T.format("a"),
            "tests/slow/__init__.py": "",
            "tests/slow/test_b.py": T.format("b"),
        }
    )
    plan = _plan_with_options(repo, rev, rev, runner_args=args)
    assert _targets(plan) == {"tests/test_a.py::test_a"}
    assert not plan.incomplete_discovery


def test_dsc10_an_absolute_ignore_path_inside_the_repository(repo, capsys):
    rev = repo.commit(
        {
            "tests/test_a.py": T.format("a"),
            "tests/slow/__init__.py": "",
            "tests/slow/test_b.py": T.format("b"),
        }
    )
    slow = repo.path.resolve() / "tests" / "slow"
    for args in ([f"--ignore={slow}"], ["--ignore", str(slow)]):
        argv = ["plan", "--repo", str(repo.path), "--base", rev, "--head", rev]
        assert main([*argv, "--discover", "pytest", "--no-cache", "--", *args]) == 0
        data = json.loads(capsys.readouterr().out)
        ids = {t["runner_id"] for t in data["unselected_targets"] + data["selected_targets"]}
        assert ids == {"tests/test_a.py::test_a"}, args


def test_dsc10_relative_arguments(tmp_path):
    inside = tmp_path / "tests" / "x.py"
    assert relative_arguments(
        [f"--ignore={tmp_path / 'tests'}", str(inside), str(tmp_path), "/elsewhere", "-x"],
        tmp_path,
    ) == ("--ignore=tests", "tests/x.py", ".", "/elsewhere", "-x")


def test_dsc11_a_path_inside_another_initial_path_is_dropped(repo):
    """pytest keeps initial paths prefix-free: ``tests/legacy/test_l.py``
    inside ``tests`` is no initial path, so --ignore=tests/legacy applies."""
    rev = repo.commit(
        {
            "tests/test_a.py": T.format("a"),
            "tests/legacy/test_l.py": T.format("l"),
            "tests/legacy/test_m.py": T.format("m"),
        }
    )
    args = ("tests", "tests/legacy/test_l.py", "--ignore=tests/legacy")
    assert _targets(_plan_with_options(repo, rev, rev, runner_args=args)) == {
        "tests/test_a.py::test_a"
    }
    args = ("tests/legacy/test_l.py", "--ignore=tests/legacy")
    assert _targets(_plan_with_options(repo, rev, rev, runner_args=args)) == {
        "tests/legacy/test_l.py::test_l"
    }


# ----------------------------------------------------------------- the environment and the command


PLUGIN_FILES = {
    "plugins/__init__.py": "",
    "plugins/auto.py": AUTO_ENV.format("pass"),
    "tests/__init__.py": "",
    "tests/test_a.py": T.format("a"),
}


@pytest.mark.parametrize(
    "options",
    [
        {"env_plugins": ("plugins.auto",)},
        {"env_addopts": ("-p", "plugins.auto")},
        {"command_args": ("-p", "plugins.auto")},
        {"command_args": ("-pplugins.auto",)},
    ],
    ids=["PYTEST_PLUGINS", "PYTEST_ADDOPTS", "command", "command_attached"],
)
def test_dsc5_plugins_loaded_outside_the_configuration_are_dependencies(repo, options):
    base = repo.commit({**BENCH, **PLUGIN_FILES})
    head = repo.commit({"plugins/auto.py": AUTO_ENV.format("raise RuntimeError('broken')")})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET], **options)
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert "bench.time_noop" in unselected(plan)


def test_dsc5_pytest_addopts_comes_after_addopts_and_before_the_command(repo):
    """pytest's order: ini addopts, PYTEST_ADDOPTS, the command line; a
    path in any of them is an initial path."""
    rev = repo.commit(
        {
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "tests/test_a.py": T.format("a"),
            "other/test_o.py": T.format("o"),
            "more/test_m.py": T.format("m"),
        }
    )
    plan = _plan_with_options(repo, rev, rev, env_addopts=("other",), runner_args=("more",))
    assert _targets(plan) == {"other/test_o.py::test_o", "more/test_m.py::test_m"}
    config = plan.discovery[0].config
    assert config["run_arguments"] == {"PYTEST_ADDOPTS": ["other"], "after --": ["more"]}
    text = to_text(plan)
    assert "  read (PYTEST_ADDOPTS): other\n" in text and "  read (after --): more\n" in text


@pytest.mark.parametrize(
    "command, args, problem",
    [
        ("python -m pytest -x -p a.b", ("-x", "-p", "a.b"), ""),
        ("uv run --with pytest-x python -m pytest -q", ("-q",), ""),
        ("coverage run -m pytest tests", ("tests",), ""),
        ("uv run pytest -p a.b", ("-p", "a.b"), ""),
        (".venv/bin/pytest", (), ""),
        ("py.test --lf", ("--lf",), ""),
        ("tox -e py", (), "names no pytest program"),
        ("uv run --with pytest pytest", (), "names pytest more than once"),
    ],
)
def test_dsc5_the_commands_pytest_arguments(command, args, problem):
    found, why = pytest_command_arguments(command.split())
    assert found == args
    assert problem in why and bool(why) == bool(problem)


def test_dsc5_an_unrecognised_command_is_reported(repo):
    rev = repo.commit({"tests/test_a.py": T.format("a")})
    plan = _plan_with_options(
        repo, rev, rev, command_problem="the command 'tox -e py' names no pytest program"
    )
    assert [n.kind for n in plan.incomplete_discovery] == ["unmodelled_runner_option"]


def test_dsc5_the_environment_and_command_reach_discovery_through_the_cli(
    repo, tmp_path, monkeypatch, capsys
):
    base = repo.commit({**PLUGIN_FILES, "plugins/other.py": AUTO_ENV.format("pass")})
    head = repo.commit({"plugins/auto.py": AUTO_ENV.format("raise RuntimeError('broken')")})
    common = ["plan", "--repo", str(repo.path), "--base", base, "--head", head]
    common += ["--discover", "pytest", "--no-cache"]
    for env, command in (
        ({"PYTEST_PLUGINS": "plugins.other, plugins.auto"}, None),
        ({"PYTEST_ADDOPTS": "-p plugins.auto"}, None),
        ({}, "python -m pytest -p plugins.auto"),
    ):
        monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        argv = common + (["--command", command] if command else [])
        assert main(argv) == 0
        data = json.loads(capsys.readouterr().out)
        assert [t["runner_id"] for t in data["selected_targets"]] == ["tests/test_a.py::test_a"]
        used = data["discovery"][0]["config"]["run_arguments"]
        assert used == (
            {"PYTEST_PLUGINS": ["plugins.other", "plugins.auto"]}
            if "PYTEST_PLUGINS" in env
            else {"PYTEST_ADDOPTS": ["-p", "plugins.auto"]}
            if env
            else {"command": ["-p", "plugins.auto"]}
        )


def test_dsc5_environment_options():
    assert environment_options({"PYTEST_ADDOPTS": "-p 'a b'", "PYTEST_PLUGINS": "x,,y "}) == {
        "env_addopts": ("-p", "a b"),
        "env_plugins": ("x", "y"),
    }
    assert environment_options({}) == {"env_addopts": (), "env_plugins": ()}


# ------------------------------------------------------------------ files outside the source roots


def test_d1_test_files_outside_the_source_roots_make_discovery_incomplete(repo, tmp_path):
    base = repo.commit(
        {
            "src/calc/__init__.py": "",
            "src/calc/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_ops.py": "from calc.ops import add\n\n\ndef test_add():\n    add(1, 2)\n",
        }
    )
    head = repo.commit({"src/calc/ops.py": "def add(a, b):\n    return b + a\n"})
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--discover",
            "pytest",
            "--source-root",
            "src",
            "-o",
            str(tmp_path / "plan.json"),
        ]
    )
    assert code == 3
    report = json.loads((tmp_path / "plan.json").read_text())
    assert report["discovery_incomplete"]
    notes = report["discovery"][0]["notes"]
    assert any(n["kind"] == "test_file_outside_roots" for n in notes)


def test_d22_doctests_outside_the_roots_are_reported(repo):
    rev = repo.commit(
        {
            "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--doctest-modules'\n",
            "src/lib/__init__.py": "",
            "tests/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "scripts/tool.py": 'def f():\n    """\n    >>> 1\n    1\n    """\n',
        }
    )
    plan = repo.plan(rev, rev, [], ["src", "tests=tests"], discover_runners=["pytest"])
    notes = {(n.kind, n.path) for d in plan.discovery for n in d.notes}
    assert ("test_file_outside_roots", "scripts/tool.py") in notes


def test_d24_a_conftest_outside_the_roots_is_an_unknown_dependency(repo):
    base = repo.commit(
        {
            "conftest.py": "X = 1\n",
            "src/lib/__init__.py": "",
            "src/lib/m.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "def test_m():\n    pass\n",
        }
    )
    head = repo.commit({"src/lib/m.py": "def f():\n    return 2\n"})
    plan = repo.plan(base, head, [], ["src", "tests=tests"], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_m.py::test_m"}
    notes = {n.kind for d in plan.discovery for n in d.notes}
    assert "conftest_outside_roots" in notes


OUTSIDE_PLUGIN = (
    "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _env(monkeypatch):\n"
    "    monkeypatch.setenv('X', '{}')\n"
)


@pytest.mark.parametrize(
    "files, options",
    [
        ({"tests/conftest.py": "pytest_plugins = ['support.plugin']\n"}, {}),
        ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-p support.plugin'\n"}, {}),
        ({}, {"env_plugins": ("support.plugin",)}),
    ],
    ids=["pytest_plugins", "addopts_p", "PYTEST_PLUGINS"],
)
def test_pln5_a_plugin_in_the_repository_outside_the_roots_is_unknown(repo, files, options):
    base = repo.commit(
        {
            **BENCH,
            "src/pkg/__init__.py": "",
            "src/pkg/ops.py": (
                "import os\n\n\ndef add(a, b):\n    return a + b + int(os.environ['X'])\n"
            ),
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
            ),
            "support/__init__.py": "",
            "support/plugin.py": OUTSIDE_PLUGIN.format(0),
            **files,
        }
    )
    head = repo.commit({"support/plugin.py": OUTSIDE_PLUGIN.format(1)})
    roots = ["src", "tests", "benchmarks=benchmarks"]
    plan = repo.plan(
        base,
        head,
        [BENCH_TARGET],
        source_roots=roots,
        discover_runners=["pytest"],
        discovery_options=DiscoveryOptions(**options),
    )
    assert selected(plan) == {"tests/test_ops.py::test_add"}
    assert "bench.time_noop" in unselected(plan)
    notes = [n for d in plan.discovery for n in d.notes if n.kind == "plugin_outside_roots"]
    assert [n.path for n in notes] == ["support/plugin.py"]


# --------------------------------------------------------------------------- what pytest collects


def test_d3_tests_bound_by_assignment_are_targets(repo):
    targets, notes = _targets_and_notes(
        repo,
        {
            "tests/test_a.py": (
                "import unittest\n\n\ndef test_orig():\n    pass\n\n\n"
                "def _check():\n    pass\n\n\n"
                "test_alias = test_orig\ntest_from_helper = _check\n\n\n"
                "class TestK:\n    def test_m(self):\n        pass\n\n    test_attr = test_m\n\n\n"
                "class Machine:\n    TestCase = unittest.TestCase\n\n\n"
                "TestMachine = Machine.TestCase\n"
                "test_data = [1, 2]\n"
            ),
        },
    )
    assert {
        "tests/test_a.py::test_alias",
        "tests/test_a.py::test_from_helper",
        "tests/test_a.py::TestK::test_attr",
    } <= targets
    assert "unmodelled_test_binding" in notes  # TestMachine


def test_d4_d5_unittest_runtest_and_imported_testcases_under_any_name(repo):
    targets, _ = _targets_and_notes(
        repo,
        {
            "tests/__init__.py": "",
            "tests/base.py": (
                "import unittest\n\n\nclass SharedChecks(unittest.TestCase):\n"
                "    def test_shared(self):\n        pass\n"
            ),
            "tests/test_a.py": (
                "import unittest\n\nfrom tests.base import SharedChecks as ImportedChecks\n\n\n"
                "class RunOnly(unittest.TestCase):\n    def runTest(self):\n        pass\n"
            ),
        },
    )
    assert {
        "tests/test_a.py::RunOnly::runTest",
        "tests/test_a.py::ImportedChecks::test_shared",
    } <= targets


def test_d11_a_function_marked_test_is_collected(repo):
    targets, _ = _targets_and_notes(
        repo, {"tests/test_a.py": "def verify():\n    pass\n\n\nverify.__test__ = True\n"}
    )
    assert "tests/test_a.py::verify" in targets


def test_d6_an_unresolvable_testcase_base_is_reported(repo):
    _, notes = _targets_and_notes(
        repo,
        {
            "tests/test_a.py": (
                "import unittest\n\n\ndef make_base():\n    return unittest.TestCase\n\n\n"
                "SharedTestCase = make_base()\n\n\n"
                "class TestThing(SharedTestCase):\n    def test_x(self):\n        pass\n"
            ),
        },
    )
    assert "unknown_base_class" in notes


def test_d6_a_framework_testcase_base_is_not(repo):
    _, notes = _targets_and_notes(
        repo,
        {
            "tests/test_a.py": (
                "from django.test import TestCase\n\n\n"
                "class TestThing(TestCase):\n    def test_x(self):\n        pass\n"
            ),
        },
    )
    assert "unknown_base_class" not in notes


def test_d22_collected_tests_discovery_used_to_drop(repo):
    targets, notes = _targets_and_notes(
        repo,
        {
            "tests/__init__.py": "",
            # A TestCase with ``__init__`` is still collected.
            "tests/test_init.py": (
                "import unittest\n\n\nclass TestI(unittest.TestCase):\n"
                "    def __init__(self, *a):\n        super().__init__(*a)\n\n"
                "    def test_i(self):\n        pass\n"
            ),
            # Nested classes are inherited.
            "tests/test_nested.py": (
                "class Base:\n    class TestInner:\n"
                "        def test_n(self):\n            pass\n\n\n"
                "class TestChild(Base):\n    pass\n"
            ),
            # A star import re-exports what the module imported, and a
            # computed ``__all__`` is a superset.
            "tests/impl.py": "class TestImpl:\n    def test_x(self):\n        pass\n",
            "tests/base.py": "from tests.impl import TestImpl  # noqa\n",
            "tests/test_star.py": "from tests.base import *  # noqa\n",
            "tests/computed.py": (
                "__all__ = ['helper'] + ['test_c']\n\n\ndef helper():\n    pass\n\n\n"
                "def test_c():\n    pass\n"
            ),
            "tests/test_computed.py": "from tests.computed import *  # noqa\n",
        },
    )
    assert {
        "tests/test_init.py::TestI::test_i",
        "tests/test_nested.py::TestChild::TestInner::test_n",
        "tests/test_star.py::TestImpl::test_x",
        "tests/test_computed.py::test_c",
    } <= targets
    assert "imported_test_out_of_scope" not in notes


def test_d26_a_django_testcase_import_is_not_reported(repo):
    _, notes = _targets_and_notes(
        repo,
        {
            "tests/__init__.py": "",
            "tests/test_a.py": (
                "from django.test import TestCase\n\n\nclass TestA(TestCase):\n"
                "    def test_a(self):\n        pass\n"
            ),
        },
    )
    assert "imported_test_out_of_scope" not in notes


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
    plan = _plan_with_runners(repo, base, head, [BENCH_TARGET])
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


# Tests bound by a call's result or dynamically: pytest collects them, and
# discovery cannot name them, so it reports them (audit round 6, T8).
@pytest.mark.parametrize(
    "module",
    [
        # A factory's result, here or imported.
        "def _make(n):\n    def t():\n        pass\n    return t\n\ntest_one = _make(1)\n",
        "from tests.helpers import make\n\ntest_one = make(1)\n",
        "import functools\n\ndef _check(n):\n    pass\n\ntest_one = functools.partial(_check, 1)\n",
        "def _make():\n    class TestGen:\n        def test_x(self):\n            pass\n"
        "    return TestGen\n\nTestGen = _make()\n",
        "def _make():\n    return 1, 2\n\ntest_a, test_b = _make()\n",
        # A lambda, an item of a container, a method of an object.
        "test_one = lambda: None\n",
        "def _f():\n    pass\n\ntest_one = [_f][0]\n",
        "from pathlib import Path\n\ntest_one = Path('.').resolve\n",
        # Stored into the module's namespace.
        "def _make(n):\n    return lambda: None\n\nfor n in range(2):\n"
        "    globals()[f'test_{n}'] = _make(n)\n",
        "def _t():\n    pass\n\nglobals().update(test_x=_t)\n",
        # Set on a class.
        "class TestA:\n    pass\n\ndef _check(self):\n    pass\n\n"
        "setattr(TestA, 'test_x', _check)\n",
        "class TestA:\n    pass\n\ndef _check(self):\n    pass\n\nTestA.test_x = _check\n",
        "def _make(n):\n    return lambda self: None\n\nclass TestA:\n    for _n in range(2):\n"
        "        locals()[f'test_{_n}'] = _make(_n)\n",
        "def _make():\n    return lambda self: None\n\nclass TestA:\n    test_x = _make()\n",
        # pytest-bdd makes a test per scenario of the feature file.
        "from pytest_bdd import scenarios\n\nscenarios('features/value.feature')\n",
    ],
)
def test_t8_tests_bound_by_calls_or_dynamically_are_reported(repo, module):
    targets, notes = _targets_and_notes(
        repo,
        {
            **LIB,
            "tests/helpers.py": "def make(n):\n    return lambda: None\n",
            "tests/test_a.py": module,
        },
    )
    assert "unmodelled_test_binding" in notes
    assert targets == set()


def test_t8_data_bound_to_a_test_name_is_still_no_test(repo):
    """A literal, a container, arithmetic, what a builtin or a data module
    of the standard library returns, or a name the rules do not match binds
    no test: nothing is reported (flask's ``test_path = (Path(__file__) /
    "..").resolve()``, httpx's ``test_cases = json.load(input)``)."""
    targets, notes = _targets_and_notes(
        repo,
        {
            **LIB,
            "tests/test_a.py": (
                "import json\nimport os\nfrom pathlib import Path\n\n"
                "test_data = [1, 2]\nTEST_DIR = 'x'\nglobals()['helper'] = len\n"
                "setattr(object, 'attr', len)\n"
                "test_path = (Path(__file__) / '..').resolve()\n"
                "test_dir = os.path.dirname(__file__)\ntest_cases = json.load(open('x'))\n"
                "test_content = ''.join(sorted('ab')) + '\\n'\ntest_n, test_m = divmod(7, 2)\n"
                "\n\nclass Index:\n    pass\n\n\ntest_index = Index()\n\ndef test_a():\n    pass\n"
            ),
        },
    )
    assert targets == {"tests/test_a.py::test_a"}
    assert "unmodelled_test_binding" not in notes


@pytest.mark.parametrize(
    "module",
    [
        # An ``__init__`` inherited from a base here or in another module.
        "class Base:\n    def __init__(self, x=1):\n        self.x = x\n\n"
        "class TestA(Base):\n    def test_x(self):\n        pass\n",
        "from tests.base import Base\n\nclass TestA(Base):\n    def test_x(self):\n        pass\n",
        # A ``__new__``, its own or inherited.
        "class TestA:\n    def __new__(cls):\n        return super().__new__(cls)\n\n"
        "    def test_x(self):\n        pass\n",
        "class Base:\n    def __new__(cls, *a):\n        return super().__new__(cls)\n\n"
        "class TestA(Base):\n    def test_x(self):\n        pass\n",
        # A generated ``__init__``.
        "from dataclasses import dataclass\n\n@dataclass\nclass TestA:\n    x: int = 1\n\n"
        "    def test_x(self):\n        pass\n",
        "import dataclasses\n\n@dataclasses.dataclass(frozen=True)\nclass TestA:\n"
        "    def test_x(self):\n        pass\n",
        "import attr\n\n@attr.s\nclass TestA:\n    def test_x(self):\n        pass\n",
    ],
)
def test_t17_classes_pytest_refuses_to_collect_are_not_targets(repo, module):
    """pytest skips a test class with a constructor (``hasinit``,
    ``hasnew``), wherever it comes from; a target for it is a selected test
    pytest never collects, which ``run`` reports as an error (audit round 6,
    T17). Before: only the class's own ``__init__`` counted."""
    targets, notes = _targets_and_notes(
        repo,
        {
            **LIB,
            "tests/base.py": "class Base:\n    def __init__(self, x=1):\n        self.x = x\n",
            "tests/test_a.py": module + "\n\ndef test_plain():\n    pass\n",
        },
    )
    assert targets == {"tests/test_a.py::test_plain"}
    assert not notes & {"uncollected_test_class", "unmodelled_test_binding"}


def test_t17_classes_without_a_constructor_are_still_collected(repo):
    """``@dataclass(init=False)``, a base discovery cannot resolve, and a
    TestCase with an ``__init__`` keep their targets."""
    targets, _ = _targets_and_notes(
        repo,
        {
            **LIB,
            "tests/test_a.py": (
                "import unittest\nfrom dataclasses import dataclass\nfrom somewhere import Base\n\n"
                "@dataclass(init=False)\nclass TestA:\n    def test_x(self):\n        pass\n\n"
                "class TestB(Base):\n    def test_y(self):\n        pass\n\n"
                "class TestC(unittest.TestCase):\n    def __init__(self, *a):\n"
                "        super().__init__(*a)\n\n    def test_z(self):\n        pass\n"
            ),
        },
    )
    assert targets == {
        "tests/test_a.py::TestA::test_x",
        "tests/test_a.py::TestB::test_y",
        "tests/test_a.py::TestC::test_z",
    }


# --------------------------------------------------------------------------- doctests


def test_d9_doctest_names_follow_pytests_import_mode(repo):
    targets, _ = _targets_and_notes(
        repo,
        {
            "pytest.ini": "[pytest]\naddopts = --doctest-modules\n",
            "tools/helpers.py": "def f():\n    '''\n    >>> 1\n    1\n    '''\n",
            "pkg/__init__.py": "",
            "pkg/mod.py": (
                "import sys\n\nif sys.platform:\n    def g():\n        '''\n        >>> 2\n"
                "        2\n        '''\n"
            ),
        },
    )
    # No __init__.py in tools/: pytest imports the module as ``helpers``; a
    # def under ``if`` is a module attribute like any other.
    assert {"tools/helpers.py::helpers.f", "pkg/mod.py::pkg.mod.g"} <= targets


def test_d9_doctests_discovery_cannot_read_are_reported(repo):
    _, notes = _targets_and_notes(
        repo,
        {
            "pytest.ini": "[pytest]\naddopts = --doctest-modules --doctest-glob=*.doctest\n",
            "how-to/example.py": "def f():\n    '''\n    >>> 1\n    1\n    '''\n",
            "docs/a.doctest": ">>> 1\n1\n",
            "pkg/__init__.py": "",
            "pkg/mod.py": "__test__ = {'extra': '>>> 1\\n1\\n'}\n",
        },
    )
    assert {"unparsed_file", "unmodelled_test_binding"} <= notes


def test_d15_doctests_depend_on_the_autouse_fixtures_that_fill_their_namespace(repo):
    conftest = (
        "import pytest\n\nfrom lib import helpers\n\n\n@pytest.fixture(autouse=True)\n"
        "def add_np(doctest_namespace):\n    doctest_namespace['K'] = helpers.k()\n"
    )
    chosen = _change_selects(
        repo,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--doctest-modules'\n",
            "conftest.py": conftest,
            "lib/helpers.py": "def k():\n    return 1\n",
            "lib/doc.py": 'def f():\n    """\n    >>> K\n    1\n    """\n',
        },
        {"lib/helpers.py": "def k():\n    return 2\n"},
    )
    assert "lib/doc.py::lib.doc.f" in chosen


def test_dsc4_doctests_under_an_initial_path_in_norecursedirs(repo):
    rev = repo.commit(
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\nnorecursedirs = ['tools']\n"
                "addopts = '--doctest-modules'\n"
            ),
            "tools/__init__.py": "",
            "tools/helper.py": 'def f():\n    """\n    >>> f()\n    1\n    """\n    return 1\n',
        }
    )
    plan = _plan_with_options(repo, rev, rev, runner_args=("tools",))
    assert _targets(plan) == {"tools/helper.py::tools.helper.f"}
    plan = _plan_with_options(repo, rev, rev)
    assert _targets(plan) == set()


DOC = "Example\n=======\n\n>>> 1 + 1\n2\n"


def test_dsc8_a_text_file_named_as_an_argument_is_a_doctest(repo):
    """pytest's ``_is_doctest``: a .txt/.rst initial path is a doctest
    whatever --doctest-glob says."""
    rev = repo.commit(
        {"tests/test_a.py": T.format("a"), "docs/guide.rst": DOC, "docs/other.rst": DOC}
    )
    plan = _plan_with_options(repo, rev, rev, runner_args=("tests", "docs/guide.rst"))
    assert _targets(plan) == {"tests/test_a.py::test_a", "docs/guide.rst::guide.rst"}
    assert "docs/guide.rst::guide.rst" in selected(plan)  # its content is not analysed


def test_dsc8_a_doctest_glob_with_a_directory_matches_the_path(repo):
    """``fnmatch_ex``: a glob with a ``/`` matches the path, not the name."""
    rev = repo.commit(
        {"tests/test_a.py": T.format("a"), "docs/guide.rst": DOC, "other/guide.rst": DOC}
    )
    plan = _plan_with_options(repo, rev, rev, runner_args=("--doctest-glob=docs/*.rst",))
    assert _targets(plan) == {"tests/test_a.py::test_a", "docs/guide.rst::guide.rst"}


DOCTEST_INI = {"pytest.ini": "[pytest]\naddopts = --doctest-modules\n"}
EXAMPLE = "'''\n>>> from lib.core import value\n>>> value()\n1\n'''"


@pytest.mark.parametrize(
    "module",
    [
        # Set from a name bound elsewhere, a decorator that writes it, a
        # value appended to, or a class body's f-string.
        "from lib.docs import DOC\n\n\ndef f():\n    pass\n\n\nf.__doc__ = DOC\n",
        "from lib.docs import doc\n\n\n@doc('x')\ndef f():\n    pass\n",
        "def f():\n    pass\n\n\nf.__doc__ = (f.__doc__ or '') + " + EXAMPLE + "\n",
        "N = 1\n\n\nclass C:\n    __doc__ = f'''\n    >>> {N}\n    1\n    '''\n",
        # Set on an object discovery cannot name.
        "def f():\n    pass\n\n\nfor g in (f,):\n    g.__doc__ = " + EXAMPLE + "\n",
    ],
)
def test_t8_docstrings_set_at_import_are_reported(repo, module):
    """pytest collects the doctests of docstrings computed at import
    (pandas' ``@doc``, ``f.__doc__ = ...``); discovery cannot read them,
    so it reports them (audit round 6, T8). Before: dropped silently."""
    targets, notes = _targets_and_notes(
        repo,
        {
            **LIB,
            **DOCTEST_INI,
            "lib/docs.py": (
                f"DOC = {EXAMPLE}\n\n\ndef doc(text):\n    def deco(fn):\n"
                "        fn.__doc__ = text\n        return fn\n    return deco\n"
            ),
            "lib/m.py": module,
        },
    )
    assert "unmodelled_test_binding" in notes
    assert not {t for t in targets if t.startswith("lib/m.py")}


def test_t8_docstrings_discovery_can_work_out_are_doctests(repo):
    """A literal, a module-level string expression, another function's
    docstring: read as the docstring they set. A docstring with examples a
    decorator rewrites is always run."""
    base = repo.commit(
        {
            **LIB,
            **DOCTEST_INI,
            "lib/docs.py": (
                "def doc(text):\n    def deco(fn):\n        fn.__doc__ = fn.__doc__ + text\n"
                "        return fn\n    return deco\n"
            ),
            "lib/m.py": (
                "from lib.docs import doc\n\nTEMPLATE = " + EXAMPLE + "\n\n\n"
                "def f():\n    pass\n\n\ndef g():\n    pass\n\n\ndef h():\n    pass\n\n\n"
                "class C:\n    def a(self):\n        " + EXAMPLE + "\n\n"
                "    def b(self):\n        pass\n\n    b.__doc__ = a.__doc__\n\n\n"
                "@doc('more')\ndef k():\n    " + EXAMPLE + "\n\n\n"
                "f.__doc__ = TEMPLATE.replace('1', '1')\n"
                "g.__doc__ = " + EXAMPLE + "\n"
                "h.__doc__ = 'no examples'\n"
            ),
        }
    )
    found = _discovered(repo, base)
    targets = {t.runner_id for t in found.targets}
    assert {
        "lib/m.py::lib.m.f",
        "lib/m.py::lib.m.g",
        "lib/m.py::lib.m.C.a",
        "lib/m.py::lib.m.C.b",
        "lib/m.py::lib.m.k",
    } <= targets
    assert "lib/m.py::lib.m.h" not in targets
    assert "unmodelled_test_binding" not in _kinds(found)
    assert "doctest:computed" in _deps(found, "lib/m.py::lib.m.k")
    head = repo.commit({"lib/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert {"lib/m.py::lib.m.f", "lib/m.py::lib.m.k"} <= selected(plan)


@pytest.mark.parametrize(
    "store",
    [
        "doctest_namespace['core'] = core",
        "doctest_namespace['core'] = lib.core",
        "doctest_namespace.setdefault('core', core)",
    ],
)
def test_t8_a_module_stored_into_doctest_namespace_is_a_dependency(repo, store):
    """The examples call ``core.value()`` through the name an autouse
    fixture stores; a change to it selects the doctest, as for
    ``doctest_namespace.update(core=core)`` (audit round 6, T8). Before: a
    subscript store handed nothing on and was no dependency."""
    conftest = (
        "import pytest\n\nimport lib.core\nfrom lib import core\n\n\n"
        "@pytest.fixture(autouse=True)\ndef add_ns(doctest_namespace):\n    " + store + "\n"
    )
    chosen = _change_selects(
        repo,
        {
            **DOCTEST_INI,
            "conftest.py": conftest,
            "lib/api.py": "def get():\n    '''\n    >>> core.value()\n    1\n    '''\n",
        },
        {"lib/core.py": "def value():\n    return 2\n"},
    )
    assert "lib/api.py::lib.api.get" in chosen


# -------------------------------------------------------------------- fixtures and setup functions


def test_p1_deleting_an_autouse_fixture_selects_its_tests(repo):
    """The conftest and its autouse fixture are gone at head, so head
    discovery no longer links the test to them."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/conftest.py": (
                "import os\nimport pytest\n\n\n@pytest.fixture(autouse=True)\n"
                "def mode():\n    os.environ['MODE'] = 'x'\n"
            ),
            "tests/test_v.py": (
                "import os\n\n\ndef test_v():\n    assert os.environ['MODE'] == 'x'\n"
            ),
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit({"tests/conftest.py": None})
    plan = repo.plan(base, head, [bench], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_v.py::test_v"}
    assert "bench.time_noop" in unselected(plan)


def test_p1_deleting_a_fixture_override_selects_its_tests(repo):
    """With the inner override deleted, the test resolves to the outer
    fixture: a different value, reached only through the base's edge."""
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 1\n",
            "tests/sub/__init__.py": "",
            "tests/sub/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 2\n"
            ),
            "tests/sub/test_v.py": "def test_v(value):\n    assert value == 2\n",
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    head = repo.commit({"tests/sub/conftest.py": None})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/sub/test_v.py::test_v"}


def test_d20_a_staticmethod_test_keeps_its_first_fixture(repo):
    fixture = "import pytest\n\n\n@pytest.fixture\ndef value():\n    return {}\n"
    chosen = _change_selects(
        repo,
        {
            "tests/conftest.py": fixture.format("1"),
            "tests/test_s.py": (
                "class TestS:\n    @staticmethod\n    def test_s(value):\n        assert value\n"
            ),
            "tests/test_t.py": "def test_t():\n    pass\n",
        },
        {"tests/conftest.py": fixture.format("2")},
    )
    assert chosen == {"tests/test_s.py::TestS::test_s"}


def test_d21_a_fixture_made_in_one_call_is_recognised(repo):
    conftest = (
        "import pytest\n\n\ndef _env():\n    return {}\n\n\n"
        "env = pytest.fixture(_env, autouse=True)\n"
    )
    chosen = _change_selects(
        repo,
        {
            "tests/sub/__init__.py": "",
            "tests/sub/conftest.py": conftest.format("1"),
            "tests/sub/test_s.py": "def test_s():\n    pass\n",
            "tests/test_t.py": "def test_t():\n    pass\n",
        },
        {"tests/sub/conftest.py": conftest.format("2")},
    )
    assert chosen == {"tests/sub/test_s.py::test_s"}


def test_d22_a_dynamic_getfixturevalue_depends_on_every_visible_fixture(repo):
    conftest = (
        "import pytest\n\n\n@pytest.fixture\ndef small():\n    return {}\n\n\n"
        "@pytest.fixture\ndef big():\n    return 10\n"
    )
    chosen = _change_selects(
        repo,
        {
            "tests/conftest.py": conftest.format("1"),
            "tests/test_d.py": (
                "import pytest\n\n\n@pytest.mark.parametrize('name', ['sm' + 'all', 'big'])\n"
                "def test_d(request, name):\n    assert request.getfixturevalue(name)\n"
            ),
            "tests/test_e.py": "def test_e():\n    pass\n",
        },
        {"tests/conftest.py": conftest.format("2")},
    )
    assert chosen == {"tests/test_d.py::test_d"}


AUTO = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef auto_env():\n    {}\n"


@pytest.mark.parametrize(
    "files",
    [
        # by name, by star, as a module attribute, into the test module
        {"conftest.py": "from helpers.fx import auto_env  # noqa\n"},
        {"conftest.py": "from helpers.fx import *  # noqa\n"},
        {"conftest.py": "import helpers.fx\n\nauto_env = helpers.fx.auto_env\n"},
        {"conftest.py": "import helpers.fx as h\n\nrenamed = h.auto_env\n"},
        {"tests/test_a.py": "from helpers.fx import auto_env  # noqa\n\n\n" + T.format("a")},
        # through a re-exporting module, by name and by a two-level star chain
        {"conftest.py": "from helpers.again import auto_env  # noqa\n"},
        {"conftest.py": "from helpers.star import *  # noqa\n"},
        # a pytest_plugins package re-exporting two levels deep
        {"conftest.py": "pytest_plugins = ['helpers.plugin']\n"},
    ],
    ids=["name", "star", "attr", "attr_alias", "test_module", "reexport", "star2", "plugin"],
)
def test_dsc1_an_imported_autouse_fixture_is_a_dependency(repo, files):
    """Nobody requests an autouse fixture by name, so a binding of one was
    never followed: a change to its body selected nothing."""
    base = repo.commit(
        {
            **BENCH,
            "helpers/__init__.py": "",
            "helpers/fx.py": AUTO.format("pass"),
            "helpers/again.py": "from helpers.fx import auto_env  # noqa\n",
            "helpers/star.py": "from helpers.star2 import *  # noqa\n",
            "helpers/star2.py": "from .fx import *  # noqa\n",
            "helpers/plugin/__init__.py": "from .db import *  # noqa\n",
            "helpers/plugin/db.py": "from helpers.star2 import *  # noqa\n",
            "tests/__init__.py": "",
            "tests/test_a.py": T.format("a"),
            "tests/test_b.py": T.format("b"),
            **files,
        }
    )
    head = repo.commit({"helpers/fx.py": AUTO.format("raise RuntimeError('broken')")})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET])
    expected = (
        {"tests/test_a.py::test_a"}
        if "tests/test_a.py" in files
        else {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    )
    assert selected(plan) == expected
    assert "bench.time_noop" in unselected(plan)
    assert not plan.incomplete_discovery


def test_dsc1_an_imported_fixture_that_is_not_autouse_is_still_not_a_dependency(repo):
    """The other side: following bindings for autouse fixtures adds no
    fixture a test does not use."""
    plain = AUTO.replace("autouse=True", "").replace("(\n", "\n")
    base = repo.commit(
        {
            "helpers/__init__.py": "",
            "helpers/fx.py": plain.format("pass"),
            "conftest.py": "from helpers.fx import *  # noqa\n",
            "tests/__init__.py": "",
            "tests/test_a.py": T.format("a"),
            "tests/test_b.py": "def test_b(auto_env):\n    pass\n",
        }
    )
    head = repo.commit({"helpers/fx.py": plain.format("raise RuntimeError('broken')")})
    assert selected(_plan_with_options(repo, base, head)) == {"tests/test_b.py::test_b"}


def test_dsc1_an_imported_fixture_with_its_own_name_is_found_under_it(repo):
    """The same gap for ``name=``: the binding's name is not the one tests
    request, and the module's fixture hides the conftest's."""
    named = "import pytest\n\n\n@pytest.fixture(name='db')\ndef real_db():\n    return {}\n"
    base = repo.commit(
        {
            **BENCH,
            "helpers/__init__.py": "",
            "helpers/fx.py": named.format(2),
            "conftest.py": "import pytest\n\n\n@pytest.fixture\ndef db():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_a.py": (
                "from helpers.fx import real_db  # noqa\n\n\ndef test_a(db):\n    assert db == 2\n"
            ),
            "tests/test_b.py": "def test_b(db):\n    assert db == 1\n",
        }
    )
    head = repo.commit({"helpers/fx.py": named.format(3)})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert unselected(plan) == {"tests/test_b.py::test_b", "bench.time_noop"}


# scrapy's tests/test_http2_client_protocol.py: pytest-twisted's
# ``async_yield_fixture`` was not recognised as a fixture decorator, so the
# fixtures ``client`` requested (``site``) were no dependency; and since
# ``client`` is also pytest-django's, the name was assumed to be that
# plugin's fixture.


def _twisted(site_value: str, name: str) -> dict[str, str]:
    return {
        **BENCH,
        "tests/test_h2.py": (
            "import pytest\n"
            "from pytest_twisted import async_yield_fixture\n\n\n"
            "class TestH2:\n"
            "    @pytest.fixture\n"
            "    def site(self):\n"
            f"        return {site_value}\n\n"
            "    @async_yield_fixture\n"
            f"    async def {name}(self, site):\n"
            "        yield site\n\n"
            f"    def test_uses(self, {name}):\n"
            f"        assert {name}\n\n"
            "    def test_other(self):\n"
            "        pass\n"
        ),
    }


@pytest.mark.parametrize("name", ["client", "conn"])
def test_async_yield_fixture_requests_are_dependencies(repo, name):
    base = repo.commit(_twisted("1", name))
    head = repo.commit(_twisted("2", name))  # only TestH2.site's body changes
    plan = _plan_with_bench(repo, base, head)
    assert selected(plan) == {"tests/test_h2.py::TestH2::test_uses"}
    deps = _deps(_discovered(repo, head), "tests/test_h2.py::TestH2::test_uses")
    assert f"tests.test_h2.TestH2.{name}" in deps
    assert "tests.test_h2.TestH2.site" in deps


@pytest.mark.parametrize(
    "decorator",
    [
        "pytest_asyncio.fixture",
        "pytest_asyncio.fixture(scope='module')",
        "async_fixture",
        "pytest_twisted.async_fixture()",
        "pytest_trio.trio_fixture",
    ],
)
def test_fixture_decorators_ending_in_fixture(repo, decorator):
    """Any decorator whose name ends in ``fixture`` makes a fixture, and its
    ``name=`` and ``autouse=`` keywords are pytest's."""

    def files(value: str) -> dict[str, str]:
        named = f"{decorator.split('(')[0]}(name='client')"
        auto = f"{decorator.split('(')[0]}(autouse=True)"
        return {
            **BENCH,
            "tests/conftest.py": (
                "import pytest\n"
                "import pytest_asyncio\n"
                "import pytest_trio\n"
                "import pytest_twisted\n"
                "from pytest_twisted import async_fixture\n\n\n"
                "@pytest.fixture\n"
                f"def site():\n    return {value}\n\n\n"
                "@pytest.fixture\n"
                "def other_site():\n    return 1\n\n\n"
                f"@{decorator}\n"
                "async def conn(site):\n    return site\n\n\n"
                f"@{named}\n"
                "async def _client(site):\n    return site\n\n\n"
                f"@{auto}\n"
                "async def everywhere(other_site):\n    return other_site\n"
            ),
            "tests/test_x.py": (
                "def test_conn(conn):\n    pass\n\n\n"
                "def test_client(client):\n    pass\n\n\n"
                "def test_plain():\n    pass\n"
            ),
        }

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))  # only site's body changes
    assert selected(_plan_with_bench(repo, base, head)) == {
        "tests/test_x.py::test_conn",
        "tests/test_x.py::test_client",
    }
    plain = _deps(_discovered(repo, head), "tests/test_x.py::test_plain")
    assert {"tests.conftest.everywhere", "tests.conftest.other_site"} <= set(plain)


# A fixture the hooks below parametrize, so the test resolves every name.
N_FIXTURE = "import pytest\n\n\n@pytest.fixture\ndef n():\n    return 0\n"


@pytest.mark.parametrize(
    "kind, setup",
    [
        ("module", "def setUpModule():\n    global N\n    N = {}\n"),
        (
            "async",
            "class TestA(unittest.IsolatedAsyncioTestCase):\n"
            "    async def asyncSetUp(self):\n        self.n = {}\n\n"
            "    def test_a(self):\n        pass\n",
        ),
        (
            "generate",
            "class TestA:\n    def pytest_generate_tests(self, metafunc):\n"
            "        metafunc.parametrize('n', [{}])\n\n"
            "    def test_a(self, n):\n        pass\n",
        ),
    ],
)
def test_d19_unittest_and_class_level_lifecycle_names(repo, kind, setup):
    tail = "\n\nclass TestA(unittest.TestCase):\n    def test_a(self):\n        pass\n"
    module = "import unittest\n\n\n" + setup + (tail if kind == "module" else "")
    chosen = _change_selects(
        repo,
        {
            "tests/conftest.py": N_FIXTURE,
            "tests/test_a.py": module.format("1"),
            "tests/test_b.py": "def test_b():\n    pass\n",
        },
        {"tests/test_a.py": module.format("2")},
    )
    assert chosen == {"tests/test_a.py::TestA::test_a"}


# Fixtures a test requests in ways its own syntax does not say (audit round
# 6, T8). ``val`` reads ``lib.core.value``: a change to it must select the
# test.
VAL_CONFTEST = (
    "import pytest\n\nfrom lib import core\n\n\n@pytest.fixture\ndef val():\n"
    "    return core.value()\n"
)


@pytest.mark.parametrize(
    "files",
    [
        # An alias of getfixturevalue, or one fetched by getattr.
        {
            "tests/test_a.py": "def test_a(request):\n    get = request.getfixturevalue\n"
            "    get('val')\n"
        },
        {
            "tests/test_a.py": "def test_a(request):\n"
            "    getattr(request, 'getfixturevalue')('val')\n"
        },
        # ``request`` handed to a helper: in the conftest, in the module, to
        # a fixture's helper, through a keyword, a method or ``super()``.
        {
            "tests/conftest.py": VAL_CONFTEST
            + "\n\ndef fixture_value(request, name):\n    return request.getfixturevalue(name)\n",
            "tests/test_a.py": (
                "from tests.conftest import fixture_value\n\n\ndef test_a(request):\n"
                "    fixture_value(request, 'val')\n"
            ),
        },
        {
            "tests/test_a.py": (
                "def _resolve(req):\n    return req.getfixturevalue('val')\n\n\n"
                "def test_a(request):\n    _resolve(req=request)\n"
            )
        },
        {
            "tests/conftest.py": VAL_CONFTEST
            + "\n\ndef _resolve(req):\n    return req.getfixturevalue('val')\n\n\n"
            "@pytest.fixture\ndef wrapped(request):\n    return _resolve(request)\n",
            "tests/test_a.py": "def test_a(wrapped):\n    pass\n",
        },
        {
            "tests/test_a.py": (
                "class TestA:\n    def _use(self, request):\n"
                "        request.getfixturevalue('val')\n\n"
                "    def test_a(self, request):\n        self._use(request)\n"
            )
        },
        {
            "tests/base.py": (
                "class Base:\n    def test_a(self, request):\n"
                "        request.getfixturevalue('val')\n"
            ),
            "tests/test_a.py": (
                "from tests.base import Base\n\n\nclass TestA(Base):\n"
                "    def test_a(self, request):\n        super().test_a(request)\n"
            ),
        },
        # Handed to what discovery cannot read: any fixture it can see.
        {
            "tests/test_a.py": (
                "from outside import resolve\n\n\ndef test_a(request):\n    resolve(request)\n"
            )
        },
        {"tests/test_a.py": "def test_a(request):\n    holder = [request]\n    holder[0]\n"},
        # pytest-lazy-fixtures and pytest-lazy-fixture in parametrize values.
        {
            "tests/test_a.py": (
                "import pytest\nfrom pytest_lazy_fixtures import lf\n\n\n"
                "@pytest.mark.parametrize('x', [lf('val')])\ndef test_a(x):\n    pass\n"
            )
        },
        {
            "tests/test_a.py": (
                "import pytest\nfrom pytest_lazyfixture import lazy_fixture\n\n"
                "CASES = [lazy_fixture('val')]\n\n\n"
                "@pytest.mark.parametrize('x', CASES)\ndef test_a(x):\n    pass\n"
            )
        },
        {
            "tests/conftest.py": VAL_CONFTEST
            + "\n\n@pytest.fixture(params=[pytest.lazy_fixture('val')])\n"
            "def through(request):\n    return request.param\n",
            "tests/test_a.py": "def test_a(through):\n    pass\n",
        },
        # Added to every item's fixtures by a hook.
        {
            "tests/conftest.py": VAL_CONFTEST
            + "\n\ndef pytest_collection_modifyitems(items):\n    for item in items:\n"
            "        item.fixturenames.append('val')\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
        },
    ],
)
def test_t8_fixtures_requested_outside_the_tests_own_syntax_are_dependencies(repo, files):
    chosen = _change_selects(
        repo,
        {"tests/conftest.py": VAL_CONFTEST, **files},
        {"lib/core.py": "def value():\n    return 2\n"},
    )
    assert chosen & {"tests/test_a.py::test_a", "tests/test_a.py::TestA::test_a"}


def test_t8_a_helper_that_only_marks_through_request_requests_nothing(repo):
    """``request`` handed to a helper whose code discovery reads (pandas'
    ``xfail_datetimes_with_pyxlsb(engine, request)``) requests what that
    code requests, here nothing; one handed to a helper it cannot read may
    request any fixture in scope."""
    rev = repo.commit(
        {
            **LIB,
            "tests/conftest.py": VAL_CONFTEST,
            "tests/helpers.py": (
                "import pytest\n\n\ndef xfail_if(engine, request):\n    if engine:\n"
                "        request.applymarker(pytest.mark.xfail)\n"
            ),
            "tests/test_a.py": (
                "from tests.helpers import xfail_if\nfrom outside import other\n\n\n"
                "def test_known(request):\n    xfail_if(1, request)\n\n\n"
                "def test_unknown(request):\n    other(request)\n"
            ),
        }
    )
    result = _discovered(repo, rev)
    assert "tests.conftest.val" not in _deps(result, "tests/test_a.py::test_known")
    assert "tests.conftest.val" in _deps(result, "tests/test_a.py::test_unknown")


@pytest.mark.parametrize(
    "conftest",
    [
        "from pytest import fixture as fx\nfrom lib import core\n\n\n"
        "@fx(autouse=True)\ndef _chk():\n    core.value()\n",
        "import pytest\nfrom lib import core\n\nfx = pytest.fixture\n\n\n"
        "@fx(autouse=True)\ndef _chk():\n    core.value()\n",
        "from pytest import fixture as fx\nfrom lib import core\n\n\ndef _chk():\n"
        "    core.value()\n\n\nchk = fx(autouse=True)(_chk)\n",
    ],
)
def test_t8_an_autouse_fixture_under_an_aliased_decorator(repo, conftest):
    """``from pytest import fixture as fx``: the alias is resolved through
    the module's imports, not matched by name. Before: ``@fx`` was no
    fixture, so its autouse reached no test."""
    chosen = _change_selects(
        repo,
        {"tests/conftest.py": conftest, "tests/test_a.py": T.format("a")},
        {"lib/core.py": "def value():\n    return 2\n"},
    )
    assert "tests/test_a.py::test_a" in chosen


# ---------------------------------------------------------------------- usefixtures and pytestmark


def test_d14_the_ini_usefixtures_option_is_requested_by_every_test(repo):
    fixture = (
        "import pytest\n\n\n@pytest.fixture\n"
        "def env(monkeypatch):\n    monkeypatch.setenv('M', '{}')\n"
    )
    chosen = _change_selects(
        repo,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\nusefixtures = ['env']\n",
            "tests/conftest.py": fixture.format("1"),
            "tests/test_m.py": "import os\n\n\ndef test_m():\n    assert os.environ['M'] == '1'\n",
        },
        {"tests/conftest.py": fixture.format("2")},
    )
    assert chosen == {"tests/test_m.py::test_m"}


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
    plan = _plan_with_runners(repo, base, head, [BENCH_TARGET])
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
    assert selected(_plan_with_runners(repo, base, head)) == {"tests/test_a.py::TestA::test_a"}


@pytest.mark.parametrize(
    "files",
    [
        # Arguments that are not literals.
        {
            "tests/test_a.py": "import pytest\nNAME = 'val'\n\n\n@pytest.mark.usefixtures(NAME)\n"
            "def test_a():\n    pass\n"
        },
        {
            "tests/test_a.py": "import pytest\nNAMES = ['val']\n\n\n"
            "@pytest.mark.usefixtures(*NAMES)\n"
            "def test_a():\n    pass\n"
        },
        # Through an alias of the mark.
        {
            "tests/test_a.py": "from pytest import mark\nuse = mark.usefixtures\n\n\n@use('val')\n"
            "def test_a():\n    pass\n"
        },
        # Applied by a function, a mark factory, or a list of marks a call
        # returns.
        {
            "tests/helpers.py": "import pytest\n\n\ndef needs_val(f):\n"
            "    return pytest.mark.usefixtures('val')(f)\n",
            "tests/test_a.py": "from tests.helpers import needs_val\n\n\n@needs_val\n"
            "def test_a():\n    pass\n",
        },
        {
            "tests/helpers.py": "import pytest\n\n\ndef uses(name):\n"
            "    return pytest.mark.usefixtures(name)\n",
            "tests/test_a.py": "from tests import helpers\n\n\n@helpers.uses('val')\n"
            "def test_a():\n    pass\n",
        },
        {
            "tests/helpers.py": "import pytest\n\n\ndef marks():\n"
            "    return [pytest.mark.usefixtures('val')]\n",
            "tests/test_a.py": "from tests.helpers import marks\n\npytestmark = marks()\n\n\n"
            "class TestA:\n    def test_a(self):\n        pass\n",
        },
        # Added to the ini option by a hook.
        {
            "tests/conftest.py": VAL_CONFTEST + "\n\ndef pytest_configure(config):\n"
            "    config.addinivalue_line('usefixtures', 'val')\n",
            "tests/test_a.py": T.format("a"),
        },
    ],
)
def test_t8_usefixtures_discovery_cannot_read_is_an_unknown_fixture(repo, files):
    """A ``usefixtures`` whose names discovery cannot read may name any
    fixture the test can see (audit round 6, T8). Before: such a mark
    requested nothing."""
    chosen = _change_selects(
        repo,
        {"tests/conftest.py": VAL_CONFTEST, **files},
        {"lib/core.py": "def value():\n    return 2\n"},
    )
    assert any(t.endswith("::test_a") for t in chosen)


def test_t8_a_decorator_that_applies_no_mark_requests_nothing(repo):
    """A decorator in the source roots whose code never mentions
    ``usefixtures`` (a skip helper) adds no fixture."""
    rev = repo.commit(
        {
            **LIB,
            "tests/conftest.py": VAL_CONFTEST,
            "tests/helpers.py": "import pytest\n\n\ndef skip_if_no(name):\n"
            "    return pytest.mark.skipif(not name, reason=name)\n",
            "tests/test_a.py": "from tests.helpers import skip_if_no\n\n\n@skip_if_no('x')\n"
            "def test_a():\n    pass\n",
        }
    )
    assert "tests.conftest.val" not in _deps(_discovered(repo, rev), "tests/test_a.py::test_a")


# ----------------------------------------------------------------- parametrized names and indirect


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
    plan = _plan_with_runners(repo, base, head, [BENCH_TARGET])
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
    assert selected(_plan_with_runners(repo, base, head)) == set()


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
    plan = _plan_with_runners(repo, base, head)
    assert selected(plan) == {"tests/test_b.py::test_b"}
    assert "unresolved_fixture" not in _kinds(plan.discovery[0])


def test_maybe_indirect_argname_bound_in_scope_is_unknown(repo):
    """An argname maybe handed to a fixture (``indirect=FLAG``) that names
    no fixture discovery reads is a parameter, unless something in scope
    binds it to what may be one."""

    def files(value: str) -> dict[str, str]:
        return {
            **BENCH,
            "tests/conftest.py": (
                f"import pytest\n\n\n@pytest.fixture\ndef site():\n    return {value}\n"
            ),
            "tests/test_x.py": (
                "import pytest\nimport some_lib\n\nFLAG = True\n\n\n"
                "@some_lib.thing\ndef engine(request, site):\n    return site\n\n\n"
                "@pytest.mark.parametrize('engine', [1], indirect=FLAG)\n"
                "def test_engine(engine):\n    pass\n\n\n"
                "@pytest.mark.parametrize('other', [1], indirect=FLAG)\n"
                "def test_other(other):\n    pass\n"
            ),
        }

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))
    assert selected(_plan_with_bench(repo, base, head)) == {"tests/test_x.py::test_engine"}


# ----------------------------------------------------------------- well-known plugin fixture names


UNREAD = {
    "class method": (
        "tests/test_h2.py",
        "class TestH2:\n"
        "    @some_lib.thing\n"
        "    def client(self, site):\n"
        "        return site\n\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "module function": (
        "tests/test_h2.py",
        "@some_lib.thing(scope='module')\n"
        "def client(site):\n"
        "    return site\n\n\n"
        "class TestH2:\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "module call": (
        "tests/test_h2.py",
        "client = some_lib.make(lambda site: site)\n\n\n"
        "class TestH2:\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "class attribute": (
        "tests/test_h2.py",
        "class TestH2:\n"
        "    client = some_lib.make(lambda self, site: site)\n\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "conftest import": (
        "tests/conftest.py",
        "from tests.helpers import client  # noqa: F401\n",
    ),
}


@pytest.mark.parametrize("case", sorted(UNREAD))
def test_well_known_name_bound_in_scope_is_unknown(repo, case):
    """``client`` is pytest-django's, but a definition in scope discovery
    cannot read (an unknown decorator, a call's result) may be the fixture
    pytest finds, requesting what nobody can see: it is ``fixture:client``,
    whose users are selected on every change, and not the plugin's."""
    path, text = UNREAD[case]

    def files(value: str) -> dict[str, str]:
        out = {
            **BENCH,
            "tests/__init__.py": "",
            "tests/conftest.py": (
                f"import pytest\n\n\n@pytest.fixture\ndef site():\n    return {value}\n"
            ),
            "tests/helpers.py": (
                "import some_lib\n\n\n@some_lib.thing\ndef client(site):\n    return site\n"
            ),
            "tests/test_h2.py": (
                "class TestH2:\n"
                "    def test_uses(self, client):\n"
                "        pass\n\n"
                "    def test_other(self):\n"
                "        pass\n"
            ),
        }
        prefix = "import some_lib\n\n\n" if path == "tests/test_h2.py" else ""
        out[path] = prefix + text + (out[path] if path == "tests/conftest.py" else "")
        return out

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))  # only the conftest's site changes
    assert selected(_plan_with_bench(repo, base, head)) == {"tests/test_h2.py::TestH2::test_uses"}
    result = _discovered(repo, head)
    assert "fixture:client" in _deps(result, "tests/test_h2.py::TestH2::test_uses")
    notes = {(n.kind, n.detail.split("'")[1]) for n in result.notes if "'" in n.detail}
    assert ("unresolved_fixture", "client") in notes
    assert not any(n.kind == "external_fixture" for n in result.notes)


def test_well_known_name_nothing_in_scope_binds_stays_the_plugins(repo):
    """The table still holds for a name nothing in scope binds, or binds to
    data or to an import from outside the source roots (outside code, as
    the plugin's fixture is)."""

    def files(value: str) -> dict[str, str]:
        return {
            **BENCH,
            "tests/conftest.py": (
                "import pytest\n"
                "from django.conf import settings  # noqa: F401\n\n"
                "client = None\n\n\n"
                f"@pytest.fixture\ndef site():\n    return {value}\n"
            ),
            "tests/test_x.py": (
                "def test_django(client, settings, mocker):\n    pass\n\n\n"
                "def test_site(site):\n    pass\n"
            ),
        }

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))
    assert selected(_plan_with_bench(repo, base, head)) == {"tests/test_x.py::test_site"}
    result = _discovered(repo, head)
    assumed = {n.detail.split("'")[1] for n in result.notes if n.kind == "external_fixture"}
    assert assumed == {"client", "settings", "mocker"}
    assert not any(n.kind == "unresolved_fixture" for n in result.notes)


# --------------------------------------------------------------------------- hooks and plugins


def test_d13_a_session_wide_hook_off_the_tests_path_is_a_dependency(repo):
    hook = "def pytest_collection_modifyitems(items):\n    items[:] = [i for i in items if {}]\n"
    chosen = _change_selects(
        repo,
        {
            "tests/a/__init__.py": "",
            "tests/a/conftest.py": hook.format("True"),
            "tests/a/test_a.py": "def test_a():\n    pass\n",
            "tests/b/__init__.py": "",
            "tests/b/test_b.py": "def test_b():\n    pass\n",
        },
        {"tests/a/conftest.py": hook.format("'test_b' not in i.name")},
    )
    assert chosen == {"tests/a/test_a.py::test_a", "tests/b/test_b.py::test_b"}


def test_d17_a_test_modules_pytest_plugins_reach_every_test(repo):
    plug = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef seed():\n    return {}\n"
    chosen = _change_selects(
        repo,
        {
            "tests/plug.py": plug.format("1"),
            "tests/test_a.py": "pytest_plugins = ['tests.plug']\n\n\ndef test_a():\n    pass\n",
            "tests/test_b.py": "def test_b():\n    pass\n",
        },
        {"tests/plug.py": plug.format("2")},
    )
    assert chosen == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_d18_a_hook_bound_by_import_is_a_dependency(repo):
    gen = (
        "def pytest_generate_tests(metafunc):\n"
        "    if 'n' in metafunc.fixturenames:\n"
        "        metafunc.parametrize('n', [{}])\n"
    )
    chosen = _change_selects(
        repo,
        {
            "tests/conftest.py": N_FIXTURE,
            "tests/common.py": gen.format("1"),
            "tests/test_g.py": (
                "from tests.common import pytest_generate_tests  # noqa\n\n\n"
                "def test_g(n):\n    assert n\n"
            ),
            "tests/test_h.py": "def test_h():\n    pass\n",
        },
        {"tests/common.py": gen.format("0")},
    )
    assert chosen == {"tests/test_g.py::test_g"}


def test_d23_a_sibling_packages_pytest11_plugin_is_loaded(repo):
    plug = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef seed():\n    return {}\n"
    chosen = _change_selects(
        repo,
        {
            "plugpkg/pyproject.toml": (
                "[project]\nname = 'plugpkg'\n\n"
                "[project.entry-points.pytest11]\nplugpkg = 'plugpkg.fixtures'\n"
            ),
            "plugpkg/plugpkg/__init__.py": "",
            "plugpkg/plugpkg/fixtures.py": plug.format("1"),
            "tests/test_a.py": "def test_a():\n    pass\n",
        },
        {"plugpkg/plugpkg/fixtures.py": plug.format("2")},
        roots=["plugpkg", "."],
    )
    assert chosen == {"tests/test_a.py::test_a"}


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
    plan = _plan_with_runners(repo, base, head, [BENCH_TARGET])
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
    plan = _plan_with_runners(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert unselected(plan) == {"bench.time_noop"}
    assert "unreadable_plugins" in _kinds(plan.discovery[0])


FLAG_PLUGIN = (
    "import pytest\n\nfrom pkg.flags import FLAGS\n\n\n@pytest.fixture(autouse=True)\n"
    "def _turn_on():\n    FLAGS['on'] = True\n    yield\n    FLAGS.clear()\n"
)
FLAG_FILES = {
    "pkg/__init__.py": "",
    "pkg/flags.py": "FLAGS = {}\n\n\ndef flag():\n    return FLAGS.get('on', False)\n",
    "tests/__init__.py": "",
    "tests/plugin.py": FLAG_PLUGIN,
    "tests/test_a.py": T.format("a"),
    "tests/test_b.py": "from pkg.flags import flag\n\n\ndef test_b():\n    assert not flag()\n",
}
REGISTERS = 'pytest_plugins = ["tests.plugin"]\n\n\n' + T.format("a")


@pytest.mark.parametrize("how", ["added", "edited", "removed"])
def test_evp5_registering_a_plugin_in_one_module_reaches_every_test(repo, how):
    """``pytest_plugins`` in a test module registers the plugin for the
    session: its autouse fixture now applies to test_b, whose lifecycle
    dependencies grow though nothing it depends on changed."""
    files = dict(FLAG_FILES)
    if how == "edited":
        files["tests/test_a.py"] = "pytest_plugins = []\n\n\n" + T.format("a")
    if how == "removed":
        files["tests/test_a.py"] = REGISTERS
    base = repo.commit({**BENCH, **files})
    head = repo.commit({"tests/test_a.py": T.format("a") if how == "removed" else REGISTERS})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert "lifecycle_changed" in rules(plan, "tests/test_b.py::test_b")
    assert "bench.time_noop" in unselected(plan)


def test_evp5_a_plugin_modules_import_time_code_reaches_every_test(repo):
    """A plugin module runs once per session, before any test: its
    import-time code reaches every test, even one with no fixture of it."""
    env = "import os\n\nos.environ['MODE'] = '{}'\n"
    base = repo.commit(
        {
            **BENCH,
            "tests/__init__.py": "",
            "tests/setenv.py": env.format("slow"),
            "tests/conftest.py": "pytest_plugins = ['tests.setenv']\n",
            "tests/test_a.py": T.format("a"),
            "tests/test_b.py": (
                "import os\n\n\ndef test_b():\n    assert os.environ['MODE'] == 'slow'\n"
            ),
        }
    )
    head = repo.commit({"tests/setenv.py": env.format("fast")})
    plan = _plan_with_options(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert "bench.time_noop" in unselected(plan)


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


# --------------------------------------------------------------------------- ASV benchmarks


def test_p1_deleting_an_asv_module_setup_selects_its_benchmarks(repo):
    """A module-level ``setup`` is an inert ``def``: deleting it changes
    nothing at import, only what ASV runs before the benchmark."""
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "",
            "benchmarks/b.py": (
                "import os\n\n\ndef setup():\n    os.environ['N'] = '5'\n\n\n"
                "def time_x():\n    int(os.environ['N'])\n"
            ),
            "benchmarks/c.py": "def time_y():\n    pass\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
        }
    )
    path = "benchmarks/b.py"
    head = repo.commit({path: "import os\n\n\ndef time_x():\n    int(os.environ['N'])\n"})
    plan = repo.plan(base, head, [], discover_runners=["asv", "pytest"])
    assert selected(plan) == {"b.time_x"}
    assert {"c.time_y", "tests/test_a.py::test_a"} <= unselected(plan)


def test_d10_asv_names_as_asv_does(repo):
    targets, _ = _targets_and_notes(
        repo,
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "def time_root():\n    pass\n",
            "benchmarks/_common.py": (
                "def time_private_module():\n    pass\n\n\n"
                "class Base:\n    def time_base(self):\n        pass\n"
            ),
            "benchmarks/bench_a.py": (
                "from ._common import Base\n\n\n"
                "class Suite:\n    def TimeCamel(self):\n        pass\n\n"
                "    def TrackCamel(self):\n        return 1\n\n"
                "    def time_a(self):\n        pass\n\n    time_alias = time_a\n"
            ),
        },
        runner="asv",
    )
    assert {
        "time_root",
        "_common.time_private_module",
        "_common.Base.time_base",
        "bench_a.Base.time_base",
        "bench_a.Suite.TimeCamel",
        "bench_a.Suite.TrackCamel",
        "bench_a.Suite.time_a",
        "bench_a.Suite.time_alias",
    } <= targets


def test_d16_an_imported_asv_module_setup_is_a_dependency(repo):
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "",
            "benchmarks/common.py": "def setup():\n    global N\n    N = 1\n",
            "benchmarks/bench.py": "from .common import setup\n\n\ndef time_x():\n    pass\n",
            "benchmarks/other.py": "def time_y():\n    pass\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
        }
    )
    head = repo.commit({"benchmarks/common.py": "def setup():\n    global N\n    N = 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"bench.time_x"}


def test_d25_asv_aliases_benchmark_names_and_timeraw_code(repo):
    base = repo.commit(
        {
            "lib/__init__.py": "",
            "lib/core.py": "def value():\n    return 1\n",
            "bench/asv.conf.json": '{"version": 1, "benchmark_dir": "../benchmarks"}',
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": (
                "def _impl():\n    pass\n\n\ntime_alias = _impl\n\n\n"
                "def time_named():\n    pass\n\n\n"
                "time_named.benchmark_name = 'custom.time_named'\n\n\n"
                "def timeraw_import():\n    return 'from lib.core import value; value()'\n\n\n"
                "def time_other():\n    pass\n"
            ),
        }
    )
    head = repo.commit({"lib/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["asv"])
    assert {t.runner_id for t in plan.targets} == {
        "bench._impl",
        "custom.time_named",
        "bench.timeraw_import",
        "bench.time_other",
    }
    assert selected(plan) == {"bench.timeraw_import"}


ASV_CASES = (
    "class Suite:\n"
    "    def {cls}(self):\n"
    "        self.data = list(range({n}))\n\n"
    "    def time_sum(self):\n"
    "        sum(self.data)\n\n\n"
    "def {mod}():\n    pass\n\n\n"
    "def time_env():\n    pass\n"
)


@pytest.mark.parametrize("cls, mod", [("setUp", "SetUp"), ("TearDown", "teardown")])
def test_dsc2_asv_setup_and_teardown_in_any_case_are_dependencies(repo, cls, mod):
    """asv_runner finds ``setup``/``teardown`` ignoring case (``key.lower()
    == name.lower()`` over ``dir(source)``)."""
    test = py_target("tests/test_x.py::test_x", "tests.test_x.test_x")
    files = {
        "asv.conf.json": ASV_CONF,
        "benchmarks/__init__.py": "",
        "tests/__init__.py": "",
        "tests/test_x.py": T.format("x"),
    }
    base = repo.commit({**files, "benchmarks/bench.py": ASV_CASES.format(cls=cls, mod=mod, n=10)})
    head = repo.commit({"benchmarks/bench.py": ASV_CASES.format(cls=cls, mod=mod, n=10**7)})
    plan = repo.plan(base, head, [test], discover_runners=["asv"])
    assert selected(plan) == {"bench.Suite.time_sum"}
    assert unselected(plan) == {"bench.time_env", "tests/test_x.py::test_x"}
    edited = ASV_CASES.format(cls=cls, mod=mod, n=10**7).replace(
        f"def {mod}():\n    pass", f"def {mod}():\n    print()"
    )
    module = repo.commit({"benchmarks/bench.py": edited})
    plan = repo.plan(head, module, [test], discover_runners=["asv"])
    assert selected(plan) == {"bench.Suite.time_sum", "bench.time_env"}


def test_dsc2_asv_builds_the_class_for_every_benchmark(repo):
    """``klass()`` runs ``__init__`` (inherited too) before each benchmark."""
    test = py_target("tests/test_x.py::test_x", "tests.test_x.test_x")
    base_init = "class Base:\n    def __init__(self):\n        self.n = {}\n"
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "",
            "benchmarks/common.py": base_init.format(10),
            "benchmarks/bench.py": (
                "from .common import Base\n\n\nclass Suite(Base):\n"
                "    def time_loop(self):\n        for _ in range(self.n):\n            pass\n\n\n"
                "def time_free():\n    pass\n"
            ),
            "tests/__init__.py": "",
            "tests/test_x.py": T.format("x"),
        }
    )
    head = repo.commit({"benchmarks/common.py": base_init.format(10**7)})
    plan = repo.plan(base, head, [test], discover_runners=["asv"])
    assert selected(plan) == {"bench.Suite.time_loop"}
    assert unselected(plan) == {"bench.time_free", "tests/test_x.py::test_x"}


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
    plan = _plan_with_runners(repo, base, head, runners=("pytest", "asv"))
    assert selected(plan) == {name}
    assert unselected(plan) == {"other.time_other", "tests/test_a.py::test_a"}


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
    plan = _plan_with_runners(repo, base, head, runners=("pytest", "asv"))
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


# ----------------------------------------------------------------------- modules too deeply nested
# A module too deeply nested for discovery's recursive walks (ast.unparse
# of a test name bound to a long attribute chain, ast.parse itself on a
# longer one) is left out as a file that does not parse (``unparsed_file``,
# so discovery is incomplete), and such a result is not cached.

TESTS = {"tests/__init__.py": "", "tests/test_ok.py": "def test_ok():\n    pass\n"}
CHAIN = "import a\n\n{0} = a" + ".b" * 1000 + "\n"
LONG_CHAIN = "import a\n\nX = a" + ".b" * 100_000 + "\n"


def _unparsed(result) -> set[str]:
    return {n.detail for n in result.notes if n.kind == "unparsed_file"}


@pytest.mark.parametrize(
    "module",
    [
        CHAIN.format("test_x"),
        "import a\n\n\nclass TestX:\n" + CHAIN.format("    test_y").split("\n\n", 1)[1],
        LONG_CHAIN,
    ],
    ids=["module_binding", "class_binding", "parse"],
)
def test_l1_a_too_deep_test_module_leaves_discovery_incomplete(repo, tmp_path, module):
    base = repo.commit({**BENCH, **TESTS, "tests/test_x.py": module})
    head = repo.commit({"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert {(n.kind, n.path) for n in plan.incomplete_discovery} == {
        ("unparsed_file", "tests/test_x.py")
    }
    assert any("too deeply nested" in n.detail for n in plan.incomplete_discovery)
    assert {"tests/test_ok.py::test_ok", "bench.time_noop"} <= selected(plan)
    # Running out of stack depends on the caller: not cached.
    cache = IndexCache(tmp_path / "cache")
    repo.plan(base, head, [], discover_runners=["pytest", "asv"], cache=cache)
    stored = [json.loads(p.read_text()) for p in (tmp_path / "cache" / "discovery").glob("*")]
    assert sorted(d["runner"] for d in stored) == ["asv", "asv"]


def test_l1_a_too_deep_conftest_and_benchmark_module(repo):
    rev = repo.commit(
        {
            **BENCH,
            **TESTS,
            "tests/conftest.py": LONG_CHAIN,
            "benchmarks/deep.py": LONG_CHAIN + "\n\ndef time_x():\n    pass\n",
        }
    )
    found = run_discovery(repo, rev, "pytest")
    assert {n.path for n in found.notes if n.kind == "unparsed_file"} == {"tests/conftest.py"}
    assert {t.runner_id for t in found.targets} == {"tests/test_ok.py::test_ok"}
    found = run_discovery(repo, rev, "asv")
    assert _unparsed(found) == {"benchmarks/deep.py: too deeply nested to analyse"}
    assert {t.runner_id for t in found.targets} == {"bench.time_noop"}


def test_l1_a_too_deep_doctest_or_timeraw_snippet_is_unread(repo):
    """Code in a string (a doctest example, the code a ``timeraw_``
    benchmark returns) is parsed on its own: too deep, it is code that
    cannot be read, as a syntax error is."""
    snippet = "x = a" + ".b" * 100_000
    rev = repo.commit(
        {
            **BENCH,
            "pkg/__init__.py": "",
            "pkg/mod.py": f"def f():\n    '''\n    >>> {snippet}\n    '''\n",
            "benchmarks/raw.py": f"def timeraw_x():\n    return {snippet!r}\n",
        }
    )
    found = run_discovery(repo, rev, "pytest", runner_args=("--doctest-modules",))
    target = next(t for t in found.targets if t.runner_id.endswith("pkg.mod.f"))
    assert "doctest:unparsed" in target.lifecycle_dependencies
    found = run_discovery(repo, rev, "asv")
    raw = next(t for t in found.targets if t.runner_id == "raw.timeraw_x")
    assert any("timeraw" in d for d in raw.lifecycle_dependencies)


def test_e29_an_excluded_test_file_is_reported_as_not_read(repo):
    """A file pytest collects that ``[index] exclude`` keeps out of the
    analysis has no targets: discovery is incomplete, and the note names the
    exclusion rather than the source roots. An excluded conftest is an
    unknown dependency of the tests under it."""
    rev = repo.commit(
        {
            "diffcone.toml": '[index]\nexclude = ["tests/data/**", "tests/sub/conftest.py"]\n',
            "tests/test_a.py": "def test_a():\n    pass\n",
            "tests/data/test_case.py": "def test_case():\n    pass\n",
            "tests/sub/conftest.py": "",
            "tests/sub/test_s.py": "def test_s():\n    pass\n",
        }
    )
    result = _discovered(repo, rev)
    assert {n.kind for n in result.incomplete} == {"test_file_outside_roots"}
    notes = {n.path: n.detail for n in result.notes}
    assert "[index] exclude keeps it out" in notes["tests/data/test_case.py"]
    assert "narrow the exclude pattern" in notes["tests/sub/conftest.py"]
    assert "tests/data/test_case.py::test_case" not in {t.runner_id for t in result.targets}
    assert "conftest:tests/sub/conftest.py" in _deps(result, "tests/sub/test_s.py::test_s")


def test_t2_notes_on_excluded_files_name_the_exclusion(repo):
    """A plugin, a test base class or a benchmark file ``[index] exclude``
    keeps out: the note says so and how to undo it, not that a source root
    is missing (audit round 6, snap-4)."""
    rev = repo.commit(
        {
            "diffcone.toml": ('[index]\nexclude = ["tests/support/*", "benchmarks/bench_gen*"]\n'),
            "asv.conf.json": '{"version": 1, "benchmark_dir": "benchmarks"}',
            "conftest.py": 'pytest_plugins = ["tests.support.plug"]\n',
            "tests/__init__.py": "",
            "tests/support/__init__.py": "",
            "tests/support/plug.py": "",
            "tests/support/bases.py": "class Base:\n    def test_x(self):\n        pass\n",
            "tests/test_h.py": (
                "from tests.support.bases import Base\n\n\nclass TestH(Base):\n    pass\n"
            ),
            "benchmarks/__init__.py": "",
            "benchmarks/bench_gen.py": "def time_x():\n    pass\n",
            "benchmarks/bench_ok.py": "def time_y():\n    pass\n",
        }
    )
    pytest_notes = {n.kind: n.detail for n in _discovered(repo, rev).notes}
    asv_notes = {n.kind: n.detail for n in run_discovery(repo, rev, "asv").notes}
    for detail in (
        pytest_notes["plugin_outside_roots"],
        pytest_notes["unknown_base_class"],
        asv_notes["test_file_outside_roots"],
    ):
        assert "[index] exclude" in detail and "narrow the exclude pattern" in detail
        assert "add a source root" not in detail
    assert "tests/support/bases.py" in pytest_notes["unknown_base_class"]

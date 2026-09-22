"""Static runner discovery: pytest and ASV."""

from __future__ import annotations

import json

from diffcone.cli import main
from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
from diffcone.testing import asv_target, py_target, reason, rules, selected, unselected


def run_discovery(repo, rev, runner, roots=None, **opts):
    snap = read_snapshot(repo.path, rev, roots or ["."], with_config=True)
    return discover(runner, snap, build_index(snap), DiscoveryOptions(**opts))


def by_id(result):
    return {t.runner_id: t for t in result.targets}


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
    assert {(n.kind) for n in result.notes} == {"unresolved_fixture"}
    assert "widget" in result.notes[0].detail

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
    assert [n.kind for n in result2.notes] == ["external_fixture"]
    assert "'widget' requested by 1 test(s)" in result2.notes[0].detail
    assert "declared external" in result2.notes[0].detail


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
    assert asv_target  # imported for symmetry with other scenario files


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

    rev2 = repo.commit({"asv.conf.json": '{"benchmark_dir": "perf",}\n'})
    result = run_discovery(repo, rev2, "asv")
    assert result.config["benchmark_dir"] == "benchmarks"
    assert [n.kind for n in result.notes] == ["unparsable_config", "no_benchmarks"]


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

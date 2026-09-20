"""Static runner discovery: pytest and ASV."""

from __future__ import annotations

import json

from helpers import asv_target, py_target, reason, selected, unselected

from diffcone.cli import main
from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot


def run_discovery(repo, rev, runner, roots=None, **opts):
    snap = read_snapshot(repo.path, rev, roots or ["."])
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
        "def test_with_fixtures(local, tmp_path, mocker, shared):\n    pass\n\n\n"
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
        "fixture:mocker",  # unknown: conservative
    }
    assert {(n.kind) for n in result.notes} == {"unresolved_fixture"}
    assert "mocker" in result.notes[0].detail

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

    # Declaring the plugin fixture as external removes the conservative dep.
    result2 = run_discovery(repo, rev, "pytest", external_fixtures=frozenset({"mocker"}))
    rich2 = by_id(result2)["tests/test_basic.py::test_with_fixtures"]
    assert "fixture:mocker" not in rich2.lifecycle_dependencies
    assert result2.notes == []


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
            "tests/test_m.py": "from pkg.m import f\n\n\ndef test_f(mocker):\n    assert f()\n",
        }
    )
    head = repo.commit({"pkg/m.py": "def f():\n    return 2\n"})
    code = main(["discover", "--repo", str(repo.path), "--rev", head, "--discover", "pytest"])
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert data["source_roots"] == ["."]
    assert [t["runner_id"] for t in data["targets"]] == ["tests/test_m.py::test_f"]
    assert data["targets"][0]["lifecycle_dependencies"] == ["fixture:mocker", "tests.test_m"]
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
    assert report["selected_targets"][0]["conservative"] is True  # fixture:mocker unresolved

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
            "mocker",
            "--format",
            "text",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "discovery (pytest): 1 target(s), 0 note(s)" in out
    assert "tests/test_m.py::test_f\n" in out and "(conservative)" not in out

"""Acceptance scenarios from docs/diffcone_coding_agent_handoff.md.

Every scenario mixes pytest-labelled and ASV-labelled targets so that the
engine is exercised as runner-independent. Assertions check exact target
sets and the rules/paths behind them.
"""

from __future__ import annotations

from diffcone.testing import (
    asv_target,
    changes,
    path_ids,
    py_target,
    reason,
    rules,
    selected,
    unselected,
)

OPS = """
def add(a, b):
    return a + b


def mul(a, b):
    return a * b
"""

TEST_OPS = """
from pkg.ops import add, mul


def test_add():
    assert add(1, 2) == 3


def test_mul():
    assert mul(2, 3) == 6
"""

BENCH_OPS = """
from pkg.ops import add, mul


class TimeOps:
    def time_add(self):
        add(1, 2)

    def time_mul(self):
        mul(2, 3)
"""

OPS_TARGETS = [
    py_target("tests/test_ops.py::test_add", "tests.test_ops.test_add"),
    py_target("tests/test_ops.py::test_mul", "tests.test_ops.test_mul"),
    asv_target("bench_ops.TimeOps.time_add", "benchmarks.bench_ops.TimeOps.time_add"),
    asv_target("bench_ops.TimeOps.time_mul", "benchmarks.bench_ops.TimeOps.time_mul"),
]


def test_independent_functions_in_one_file(repo):
    base = repo.commit(
        {"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS, "benchmarks/bench_ops.py": BENCH_OPS}
    )
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = repo.plan(base, head, OPS_TARGETS)

    assert changes(plan) == {"pkg.ops.add": ("body_changed",)}
    assert selected(plan) == {"tests/test_ops.py::test_add", "bench_ops.TimeOps.time_add"}
    assert unselected(plan) == {"tests/test_ops.py::test_mul", "bench_ops.TimeOps.time_mul"}
    assert rules(plan, "tests/test_ops.py::test_add") == {"dependency"}
    r = reason(plan, "tests/test_ops.py::test_add")
    assert path_ids(r) == [
        "target:pytest:tests/test_ops.py::test_add",
        "tests.test_ops.test_add",
        "pkg.ops.add",
    ]
    assert [s.kind for s in r.path] == ["entry", "references"]
    assert r.changed_symbol == "pkg.ops.add"
    assert not r.conservative
    r = reason(plan, "bench_ops.TimeOps.time_add")
    assert path_ids(r) == [
        "target:asv:bench_ops.TimeOps.time_add",
        "benchmarks.bench_ops.TimeOps.time_add",
        "pkg.ops.add",
    ]
    assert plan.fallbacks == []
    assert plan.errors == []


def test_transitive_consumers(repo):
    base = repo.commit(
        {
            "pkg/helpers.py": (
                "def norm(x):\n    return x.strip()\n\n\ndef other(x):\n    return x\n"
            ),
            "pkg/service.py": (
                "from pkg import helpers\n\n\n"
                "def run(x):\n    return helpers.norm(x)\n\n\n"
                "def run_other(x):\n    return helpers.other(x)\n"
            ),
            "tests/test_service.py": (
                "from pkg.service import run, run_other\n\n\n"
                "def test_run():\n    assert run(' a ') == 'a'\n\n\n"
                "def test_run_other():\n    assert run_other('a') == 'a'\n"
            ),
            "benchmarks/bench_service.py": (
                "import pkg.service\n\n\n"
                "def time_run():\n    pkg.service.run(' a ')\n\n\n"
                "def time_other():\n    pkg.service.run_other('a')\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/helpers.py": (
                "def norm(x):\n    return x.strip().lower()\n\n\ndef other(x):\n    return x\n"
            )
        }
    )
    targets = [
        py_target("tests/test_service.py::test_run", "tests.test_service.test_run"),
        py_target("tests/test_service.py::test_run_other", "tests.test_service.test_run_other"),
        asv_target("bench_service.time_run", "benchmarks.bench_service.time_run"),
        asv_target("bench_service.time_other", "benchmarks.bench_service.time_other"),
    ]
    plan = repo.plan(base, head, targets)

    assert changes(plan) == {"pkg.helpers.norm": ("body_changed",)}
    assert selected(plan) == {"tests/test_service.py::test_run", "bench_service.time_run"}
    assert unselected(plan) == {
        "tests/test_service.py::test_run_other",
        "bench_service.time_other",
    }
    r = reason(plan, "bench_service.time_run")
    assert path_ids(r) == [
        "target:asv:bench_service.time_run",
        "benchmarks.bench_service.time_run",
        "pkg.service.run",
        "pkg.helpers.norm",
    ]


def test_imported_function_referenced_through_alias(repo):
    base = repo.commit(
        {
            "pkg/helpers.py": (
                "def compute(x):\n    return x\n\n\ndef untouched(x):\n    return x\n"
            ),
            "tests/test_alias.py": (
                "from pkg.helpers import compute as calc\n"
                "import pkg.helpers as h\n"
                "from pkg import helpers\n\n\n"
                "def test_from_alias():\n    assert calc(1) == 1\n\n\n"
                "def test_module_alias():\n    assert h.compute(1) == 1\n\n\n"
                "def test_package_attr():\n    assert helpers.compute(1) == 1\n\n\n"
                "def test_untouched():\n    assert h.untouched(1) == 1\n"
            ),
            "benchmarks/bench_alias.py": (
                "import pkg.helpers as h\n\n\n"
                "class Suite:\n"
                "    def time_compute(self):\n        h.compute(1)\n\n"
                "    def time_untouched(self):\n        h.untouched(1)\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/helpers.py": (
                "def compute(x):\n    return x + 0\n\n\ndef untouched(x):\n    return x\n"
            )
        }
    )
    targets = [
        py_target("t::test_from_alias", "tests.test_alias.test_from_alias"),
        py_target("t::test_module_alias", "tests.test_alias.test_module_alias"),
        py_target("t::test_package_attr", "tests.test_alias.test_package_attr"),
        py_target("t::test_untouched", "tests.test_alias.test_untouched"),
        asv_target("b.Suite.time_compute", "benchmarks.bench_alias.Suite.time_compute"),
        asv_target("b.Suite.time_untouched", "benchmarks.bench_alias.Suite.time_untouched"),
    ]
    plan = repo.plan(base, head, targets)

    assert selected(plan) == {
        "t::test_from_alias",
        "t::test_module_alias",
        "t::test_package_attr",
        "b.Suite.time_compute",
    }
    assert unselected(plan) == {"t::test_untouched", "b.Suite.time_untouched"}
    for runner_id in ("t::test_from_alias", "t::test_module_alias", "t::test_package_attr"):
        r = reason(plan, runner_id)
        assert r.changed_symbol == "pkg.helpers.compute"
        assert not r.conservative, runner_id
    # The alias relationship is resolved, not guessed.
    assert not [u for u in plan.unresolved if u.matched_changed_symbols]


def test_shared_setup_function_changes(repo):
    base = repo.commit(
        {
            "pkg/db.py": "def connect():\n    return object()\n",
            "tests/conftest.py": (
                "import pytest\nfrom pkg.db import connect\n\n\n"
                "@pytest.fixture\ndef db():\n    return connect()\n\n\n"
                "@pytest.fixture\ndef tmp():\n    return {}\n"
            ),
            "tests/test_db.py": (
                "def test_uses_db(db):\n    assert db is not None\n\n\n"
                "def test_uses_tmp(tmp):\n    assert tmp == {}\n\n\n"
                "def test_no_fixture():\n    assert True\n"
            ),
            "benchmarks/bench_db.py": (
                "from pkg.db import connect\n\n\n"
                "class Suite:\n"
                "    def setup(self):\n        self.conn = connect()\n\n"
                "    def time_query(self):\n        pass\n\n"
                "    def time_insert(self):\n        pass\n\n\n"
                "class Other:\n"
                "    def setup(self):\n        pass\n\n"
                "    def time_other(self):\n        pass\n"
            ),
        }
    )
    targets = [
        py_target("t::test_uses_db", "tests.test_db.test_uses_db", "tests.conftest.db"),
        py_target("t::test_uses_tmp", "tests.test_db.test_uses_tmp", "tests.conftest.tmp"),
        py_target("t::test_no_fixture", "tests.test_db.test_no_fixture"),
        asv_target(
            "b.Suite.time_query",
            "benchmarks.bench_db.Suite.time_query",
            "benchmarks.bench_db.Suite.setup",
        ),
        asv_target(
            "b.Suite.time_insert",
            "benchmarks.bench_db.Suite.time_insert",
            "benchmarks.bench_db.Suite.setup",
        ),
        asv_target(
            "b.Other.time_other",
            "benchmarks.bench_db.Other.time_other",
            "benchmarks.bench_db.Other.setup",
        ),
    ]

    # Changing the fixture body selects exactly its declared consumers.
    head = repo.commit(
        {
            "tests/conftest.py": (
                "import pytest\nfrom pkg.db import connect\n\n\n"
                "@pytest.fixture\ndef db():\n    conn = connect()\n    return conn\n\n\n"
                "@pytest.fixture\ndef tmp():\n    return {}\n"
            )
        }
    )
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"tests.conftest.db": ("body_changed",)}
    assert selected(plan) == {"t::test_uses_db"}
    r = reason(plan, "t::test_uses_db")
    assert [s.kind for s in r.path] == ["lifecycle"]
    assert r.changed_symbol == "tests.conftest.db"

    # Changing the shared connect() helper reaches both runners through setup.
    head2 = repo.commit({"pkg/db.py": "def connect():\n    return dict()\n"})
    plan = repo.plan(base, head2, targets)
    assert selected(plan) == {"t::test_uses_db", "b.Suite.time_query", "b.Suite.time_insert"}
    assert unselected(plan) == {"t::test_uses_tmp", "t::test_no_fixture", "b.Other.time_other"}
    r = reason(plan, "b.Suite.time_insert")
    assert path_ids(r) == [
        "target:asv:b.Suite.time_insert",
        "benchmarks.bench_db.Suite.setup",
        "pkg.db.connect",
    ]


def test_deleted_function_and_redirected_dependency(repo):
    base = repo.commit(
        {
            "pkg/a.py": "def f():\n    return 'a'\n",
            "pkg/b.py": "def f():\n    return 'b'\n",
            "pkg/old.py": "def old_helper():\n    return 1\n",
            "pkg/service.py": "from pkg.a import f\n\n\ndef run():\n    return f()\n",
            "tests/test_service.py": (
                "from pkg.service import run\n\n\ndef test_run():\n    assert run() == 'a'\n"
            ),
            "tests/test_old.py": (
                "from pkg.old import old_helper\n\n\n"
                "def test_old():\n    assert old_helper() == 1\n\n\n"
                "def test_unrelated_same_module():\n    assert True\n"
            ),
            "tests/test_a.py": "from pkg.a import f\n\n\ndef test_a():\n    assert f() == 'a'\n",
            "benchmarks/bench.py": (
                "import pkg.service\nimport pkg.b\n\n\n"
                "def time_run():\n    pkg.service.run()\n\n\n"
                "def time_b():\n    pkg.b.f()\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/old.py": None,
            "pkg/service.py": "from pkg.b import f\n\n\ndef run():\n    return f()\n",
        }
    )
    targets = [
        py_target("t::test_run", "tests.test_service.test_run"),
        py_target("t::test_old", "tests.test_old.test_old"),
        py_target("t::test_unrelated_same_module", "tests.test_old.test_unrelated_same_module"),
        py_target("t::test_a", "tests.test_a.test_a"),
        asv_target("b.time_run", "benchmarks.bench.time_run"),
        asv_target("b.time_b", "benchmarks.bench.time_b"),
    ]
    plan = repo.plan(base, head, targets)

    assert changes(plan) == {
        "pkg.old": ("deleted",),
        "pkg.old.old_helper": ("deleted",),
        "pkg.service": ("definition_changed", "dependencies_changed"),
        "pkg.service.run": ("dependencies_changed",),
        # The importing module and test lost a resolvable dependency in head.
        "tests.test_old": ("dependencies_changed",),
        "tests.test_old.test_old": ("dependencies_changed",),
    }
    assert selected(plan) == {
        "t::test_run",
        "t::test_old",
        "t::test_unrelated_same_module",
        "b.time_run",
    }
    assert unselected(plan) == {"t::test_a", "b.time_b"}

    # The redirected call is explained through run's own changed dependencies.
    r = reason(plan, "b.time_run")
    assert path_ids(r) == ["target:asv:b.time_run", "benchmarks.bench.time_run", "pkg.service.run"]
    assert r.changes == ("dependencies_changed",)

    # The consumer of the deleted helper is itself a changed symbol: its base
    # edge to pkg.old.old_helper only exists in base and is gone in head.
    r = reason(plan, "t::test_old")
    assert path_ids(r) == ["target:pytest:t::test_old", "tests.test_old.test_old"]
    assert r.changes == ("dependencies_changed",)
    base_edges = {e.target for e in plan.base_index.edges if e.source == "tests.test_old.test_old"}
    head_edges = {e.target for e in plan.head_index.edges if e.source == "tests.test_old.test_old"}
    assert "pkg.old.old_helper" in base_edges and "pkg.old.old_helper" not in head_edges
    # In head the import cannot be resolved, and that is reported rather than dropped.
    assert any(
        u.symbol == "tests.test_old" and u.kind == "attribute" and u.revisions == ("head",)
        for u in plan.unresolved
    )

    # A module-level import of a deleted name breaks the whole importing module.
    r = reason(plan, "t::test_unrelated_same_module")
    assert [s.kind for s in r.path] == ["entry", "defined_in"]
    assert r.changed_symbol == "tests.test_old"
    assert r.changes == ("dependencies_changed",)


def test_new_target_and_changed_target_body(repo):
    base = repo.commit(
        {
            "pkg/ops.py": OPS,
            "tests/test_ops.py": TEST_OPS,
            "benchmarks/bench_ops.py": BENCH_OPS,
        }
    )
    head = repo.commit(
        {
            "tests/test_ops.py": TEST_OPS.replace("assert add(1, 2) == 3", "assert add(2, 1) == 3")
            + "\n\ndef test_new():\n    assert mul(1, 1) == 1\n",
            "benchmarks/bench_ops.py": BENCH_OPS + "\n    def time_new(self):\n        mul(1, 1)\n",
        }
    )
    targets = OPS_TARGETS + [
        py_target("tests/test_ops.py::test_new", "tests.test_ops.test_new"),
        asv_target("bench_ops.TimeOps.time_new", "benchmarks.bench_ops.TimeOps.time_new"),
    ]
    plan = repo.plan(base, head, targets)

    assert changes(plan) == {
        "tests.test_ops.test_add": ("body_changed",),
        "tests.test_ops.test_new": ("added",),
        "benchmarks.bench_ops.TimeOps": ("definition_changed",),
        "benchmarks.bench_ops.TimeOps.time_new": ("added",),
    }
    assert selected(plan) == {
        "tests/test_ops.py::test_add",
        "tests/test_ops.py::test_new",
        "bench_ops.TimeOps.time_new",
        # Adding a method changes the class structure; its methods are
        # conservatively invalidated.
        "bench_ops.TimeOps.time_add",
        "bench_ops.TimeOps.time_mul",
    }
    assert unselected(plan) == {"tests/test_ops.py::test_mul"}
    assert reason(plan, "tests/test_ops.py::test_new").changes == ("added",)
    assert reason(plan, "tests/test_ops.py::test_add").changes == ("body_changed",)
    r = reason(plan, "bench_ops.TimeOps.time_mul")
    assert [s.kind for s in r.path] == ["entry", "defined_in"]
    assert r.changed_symbol == "benchmarks.bench_ops.TimeOps"


def test_unresolvable_relationship_broadens_selection(repo):
    base = repo.commit(
        {
            "pkg/models.py": (
                "class Model:\n"
                "    def save(self):\n        return 'saved'\n\n"
                "    def load(self):\n        return 'loaded'\n"
            ),
            "pkg/service.py": (
                "import importlib\n\n\n"
                "def persist(obj):\n    return obj.save()\n\n\n"
                "def fetch(obj):\n    return obj.load()\n\n\n"
                "def plugin(name):\n    return importlib.import_module(name)\n\n\n"
                "def constant():\n    return 42\n"
            ),
            "tests/test_service.py": (
                "from pkg.service import persist, fetch, plugin, constant\n\n\n"
                "def test_persist():\n    assert persist(object()) == 'saved'\n\n\n"
                "def test_fetch():\n    assert fetch(object()) == 'loaded'\n\n\n"
                "def test_plugin():\n    assert plugin('json')\n\n\n"
                "def test_constant():\n    assert constant() == 42\n"
            ),
            "benchmarks/bench.py": (
                "from pkg import service\n\n\n"
                "def time_persist():\n    service.persist(object())\n\n\n"
                "def time_constant():\n    service.constant()\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/models.py": (
                "class Model:\n"
                "    def save(self):\n        return 'saved!'\n\n"
                "    def load(self):\n        return 'loaded'\n"
            )
        }
    )
    targets = [
        py_target("t::test_persist", "tests.test_service.test_persist"),
        py_target("t::test_fetch", "tests.test_service.test_fetch"),
        py_target("t::test_plugin", "tests.test_service.test_plugin"),
        py_target("t::test_constant", "tests.test_service.test_constant"),
        asv_target("b.time_persist", "benchmarks.bench.time_persist"),
        asv_target("b.time_constant", "benchmarks.bench.time_constant"),
    ]
    plan = repo.plan(base, head, targets)

    assert changes(plan) == {"pkg.models.Model.save": ("body_changed",)}
    assert selected(plan) == {"t::test_persist", "t::test_plugin", "b.time_persist"}
    assert unselected(plan) == {"t::test_fetch", "t::test_constant", "b.time_constant"}

    r = reason(plan, "t::test_persist", "unresolved_name_match")
    assert r.conservative
    assert path_ids(r) == [
        "target:pytest:t::test_persist",
        "tests.test_service.test_persist",
        "pkg.service.persist",
        "pkg.models.Model.save",
    ]
    assert r.path[-1].kind == "unresolved_name_match"

    r = reason(plan, "t::test_plugin", "dynamic_reference")
    assert r.conservative
    assert path_ids(r) == [
        "target:pytest:t::test_plugin",
        "tests.test_service.test_plugin",
        "pkg.service.plugin",
    ]

    matched = {u.symbol: u for u in plan.unresolved if u.matched_changed_symbols}
    assert set(matched) == {"pkg.service.persist"}
    assert matched["pkg.service.persist"].matched_changed_symbols == ("pkg.models.Model.save",)
    assert matched["pkg.service.persist"].revisions == ("base", "head")
    dynamic = [u for u in plan.unresolved if u.kind == "dynamic"]
    assert [u.symbol for u in dynamic] == ["pkg.service.plugin"]


def test_blank_lines_preserve_identity(repo):
    base = repo.commit(
        {"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS, "benchmarks/bench_ops.py": BENCH_OPS}
    )
    head = repo.commit(
        {
            "pkg/ops.py": "\n\n\n# a comment\n\n" + OPS.replace("def mul", "\n\n\ndef mul"),
            "tests/test_ops.py": "\n\n" + TEST_OPS,
        }
    )
    plan = repo.plan(base, head, OPS_TARGETS)

    assert plan.changes == []
    assert selected(plan) == set()
    assert unselected(plan) == {t.runner_id for t in OPS_TARGETS}
    assert plan.fallbacks == []
    base_add = plan.base_index.symbols["pkg.ops.add"]
    head_add = plan.head_index.symbols["pkg.ops.add"]
    assert base_add.lineno != head_add.lineno
    assert base_add.body_hash == head_add.body_hash
    assert base_add.id == head_add.id


def test_analysis_error_selects_everything(repo):
    base = repo.commit(
        {"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS, "benchmarks/bench_ops.py": BENCH_OPS}
    )
    head = repo.commit({"pkg/broken.py": "def broken(:\n    pass\n"})
    plan = repo.plan(base, head, OPS_TARGETS)

    assert plan.degraded
    assert [(e.revision, e.path) for e in plan.errors] == [(head, "pkg/broken.py")]
    assert selected(plan) == {t.runner_id for t in OPS_TARGETS}
    assert all(rules(plan, t.runner_id) == {"analysis_error"} for t in OPS_TARGETS)
    assert [f.rule for f in plan.fallbacks] == ["analysis_error"]
    assert plan.fallbacks[0].scope == "all_targets"
    # The unparsable module does not masquerade as an added symbol.
    assert plan.changes == []


def test_unknown_entry_symbol_is_selected_by_fallback(repo):
    base = repo.commit({"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS})
    targets = [
        py_target("t::ghost", "tests.test_ops.test_ghost"),
        py_target("t::test_add", "tests.test_ops.test_add"),
        asv_target("b::bad_setup", "tests.test_ops.test_mul", "benchmarks.missing.setup"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::ghost", "b::bad_setup"}
    assert rules(plan, "t::ghost") == {"entry_symbol_unresolved"}
    assert rules(plan, "b::bad_setup") == {"lifecycle_dependency_unresolved"}
    assert {(f.rule, f.target) for f in plan.fallbacks} == {
        ("entry_symbol_unresolved", "target:pytest:t::ghost"),
        ("lifecycle_dependency_unresolved", "target:asv:b::bad_setup"),
    }
    assert not plan.degraded


# --- regression scenarios added after code review ---------------------------


def test_call_result_method_access_is_name_bounded(repo):
    base = repo.commit(
        {
            "pkg/svc.py": (
                "class Foo:\n"
                "    def run(self):\n        return 1\n\n"
                "    def stop(self):\n        return 0\n\n\n"
                "def make():\n    return Foo()\n"
            ),
            "tests/test_svc.py": (
                "from pkg.svc import Foo, make\n\n\n"
                "def test_ctor_call():\n    assert Foo().run() == 1\n\n\n"
                "def test_factory_call():\n    assert make().run() == 1\n\n\n"
                "def test_stop():\n    assert Foo().stop() == 0\n"
            ),
            "benchmarks/bench.py": (
                "from pkg.svc import Foo\n\n\n"
                "def time_run():\n    Foo().run()\n\n\n"
                "def time_stop():\n    Foo().stop()\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/svc.py": (
                "class Foo:\n"
                "    def run(self):\n        return 2\n\n"
                "    def stop(self):\n        return 0\n\n\n"
                "def make():\n    return Foo()\n"
            )
        }
    )
    targets = [
        py_target("t::test_ctor_call", "tests.test_svc.test_ctor_call"),
        py_target("t::test_factory_call", "tests.test_svc.test_factory_call"),
        py_target("t::test_stop", "tests.test_svc.test_stop"),
        asv_target("b.time_run", "benchmarks.bench.time_run"),
        asv_target("b.time_stop", "benchmarks.bench.time_stop"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.svc.Foo.run": ("body_changed",)}
    assert selected(plan) == {"t::test_ctor_call", "t::test_factory_call", "b.time_run"}
    assert unselected(plan) == {"t::test_stop", "b.time_stop"}
    r = reason(plan, "t::test_factory_call", "unresolved_name_match")
    assert r.path[-1].detail == "<expr>.run"
    assert r.conservative


def test_class_attribute_change_invalidates_methods(repo):
    base = repo.commit(
        {
            "benchmarks/bench.py": (
                "class TimeOps:\n"
                "    params = [10, 100]\n\n"
                "    def time_add(self, n):\n        pass\n\n"
                "    def time_mul(self, n):\n        pass\n\n\n"
                "class Other:\n"
                "    def time_other(self):\n        pass\n"
            ),
            "tests/test_marks.py": (
                "import pytest\n\n"
                "pytestmark = pytest.mark.slow\n\n\n"
                "def test_a():\n    assert True\n"
            ),
            "tests/test_plain.py": "def test_b():\n    assert True\n",
        }
    )
    head = repo.commit(
        {
            "benchmarks/bench.py": (
                "class TimeOps:\n"
                "    params = [10, 1000]\n\n"
                "    def time_add(self, n):\n        pass\n\n"
                "    def time_mul(self, n):\n        pass\n\n\n"
                "class Other:\n"
                "    def time_other(self):\n        pass\n"
            ),
            "tests/test_marks.py": (
                "import pytest\n\n"
                "pytestmark = pytest.mark.skip\n\n\n"
                "def test_a():\n    assert True\n"
            ),
        }
    )
    targets = [
        asv_target("b.TimeOps.time_add", "benchmarks.bench.TimeOps.time_add"),
        asv_target("b.TimeOps.time_mul", "benchmarks.bench.TimeOps.time_mul"),
        asv_target("b.Other.time_other", "benchmarks.bench.Other.time_other"),
        # A runner integration declares the module as a lifecycle dependency
        # when module-level state (pytestmark, importorskip) governs the test.
        py_target("t::test_a", "tests.test_marks.test_a", "tests.test_marks"),
        py_target("t::test_b", "tests.test_plain.test_b", "tests.test_plain"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {
        "benchmarks.bench.TimeOps": ("body_changed",),
        "tests.test_marks": ("body_changed",),
    }
    assert selected(plan) == {"b.TimeOps.time_add", "b.TimeOps.time_mul", "t::test_a"}
    assert unselected(plan) == {"b.Other.time_other", "t::test_b"}
    r = reason(plan, "b.TimeOps.time_mul")
    assert [s.kind for s in r.path] == ["entry", "defined_in"]
    assert r.changed_symbol == "benchmarks.bench.TimeOps"
    r = reason(plan, "t::test_a")
    assert [s.kind for s in r.path] == ["lifecycle"]
    assert r.changed_symbol == "tests.test_marks"


def test_conditional_definition_does_not_invalidate_module(repo):
    base = repo.commit(
        {
            "pkg/m.py": (
                "import sys\n\n\n"
                "def a():\n    return 'a'\n\n\n"
                "def b():\n    return 'b'\n\n\n"
                "if sys.platform:\n"
                "    def c():\n        return a()\n"
            ),
            "tests/test_m.py": (
                "from pkg.m import b, c\n\n\n"
                "def test_b():\n    assert b() == 'b'\n\n\n"
                "def test_c():\n    assert c() == 'a'\n"
            ),
            "benchmarks/bench.py": "from pkg import m\n\n\ndef time_b():\n    m.b()\n",
        }
    )
    head = repo.commit(
        {
            "pkg/m.py": (
                "import sys\n\n\n"
                "def a():\n    return 'a'\n\n\n"
                "def b():\n    return 'b'\n\n\n"
                "if sys.platform:\n"
                "    def c():\n        return b()\n"
            )
        }
    )
    targets = [
        py_target("t::test_b", "tests.test_m.test_b"),
        py_target("t::test_c", "tests.test_m.test_c"),
        asv_target("b.time_b", "benchmarks.bench.time_b"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.m.c": ("body_changed", "dependencies_changed")}
    assert selected(plan) == {"t::test_c"}
    assert unselected(plan) == {"t::test_b", "b.time_b"}
    # Names local to c() are not reported as unresolved references of the module.
    assert not [u for u in plan.unresolved if u.symbol == "pkg.m"]


def test_nested_scope_bindings_do_not_shadow_references(repo):
    base = repo.commit(
        {
            "pkg/h.py": "def helper(x=0):\n    return x\n",
            "tests/test_h.py": (
                "from pkg.h import helper\n\n\n"
                "def test_comprehension():\n"
                "    xs = [helper for helper in range(3)]\n"
                "    return helper(), xs\n\n\n"
                "def test_nested_def():\n"
                "    def inner(helper):\n        return helper\n"
                "    return inner(1), helper()\n\n\n"
                "def test_lambda():\n"
                "    f = lambda helper: helper\n"
                "    return f(1), helper()\n\n\n"
                "def test_really_shadowed():\n"
                "    helper = 1\n"
                "    return helper\n"
            ),
            "benchmarks/bench.py": (
                "from pkg.h import helper\n\n\n"
                "def time_gen():\n    return sum(helper for helper in range(3)) + helper()\n"
            ),
        }
    )
    head = repo.commit({"pkg/h.py": "def helper(x=0):\n    return x + 1\n"})
    targets = [
        py_target("t::test_comprehension", "tests.test_h.test_comprehension"),
        py_target("t::test_nested_def", "tests.test_h.test_nested_def"),
        py_target("t::test_lambda", "tests.test_h.test_lambda"),
        py_target("t::test_really_shadowed", "tests.test_h.test_really_shadowed"),
        asv_target("b.time_gen", "benchmarks.bench.time_gen"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {
        "t::test_comprehension",
        "t::test_nested_def",
        "t::test_lambda",
        "b.time_gen",
    }
    assert unselected(plan) == {"t::test_really_shadowed"}
    assert all(not r.conservative for d in plan.selected for r in d.reasons)


def test_star_import_after_external_star_import(repo):
    base = repo.commit(
        {
            "pkg/helpers.py": "def helper():\n    return 1\n",
            "tests/test_star.py": (
                "from os.path import *\n"
                "from pkg.helpers import *\n\n\n"
                "def test_helper():\n    assert helper() == 1\n\n\n"
                "def test_join():\n    assert join('a', 'b')\n"
            ),
            "benchmarks/bench.py": (
                "from json import *\nfrom pkg.helpers import *\n\n\n"
                "def time_helper():\n    helper()\n"
            ),
        }
    )
    head = repo.commit({"pkg/helpers.py": "def helper():\n    return 2\n"})
    targets = [
        py_target("t::test_helper", "tests.test_star.test_helper"),
        py_target("t::test_join", "tests.test_star.test_join"),
        asv_target("b.time_helper", "benchmarks.bench.time_helper"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_helper", "b.time_helper"}
    assert unselected(plan) == {"t::test_join"}
    assert not reason(plan, "t::test_helper").conservative


def test_multiple_source_roots_src_layout(repo):
    base = repo.commit(
        {
            "src/calc/__init__.py": "",
            "src/calc/ops.py": OPS,
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef db():\n    return {}\n",
            "tests/test_calc.py": (
                "from calc.ops import add, mul\n\n\n"
                "def test_add(db):\n    assert add(1, 2) == 3\n\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n"
            ),
            "benchmarks/bench_calc.py": (
                "from calc.ops import add\n\n\n"
                "class TimeCalc:\n"
                "    def setup(self):\n        pass\n\n"
                "    def time_add(self):\n        add(1, 2)\n"
            ),
        }
    )
    head = repo.commit({"src/calc/ops.py": OPS.replace("a + b", "b + a")})
    targets = [
        py_target("tests/test_calc.py::test_add", "tests.test_calc.test_add", "tests.conftest.db"),
        py_target("tests/test_calc.py::test_mul", "tests.test_calc.test_mul"),
        asv_target(
            "bench_calc.TimeCalc.time_add",
            "benchmarks.bench_calc.TimeCalc.time_add",
            "benchmarks.bench_calc.TimeCalc.setup",
        ),
    ]
    plan = repo.plan(base, head, targets, source_roots=["src", "."])
    assert set(plan.head_index.modules) >= {"calc", "calc.ops", "tests.test_calc", "tests.conftest"}
    assert "src.calc.ops" not in plan.head_index.modules
    assert changes(plan) == {"calc.ops.add": ("body_changed",)}
    assert selected(plan) == {"tests/test_calc.py::test_add", "bench_calc.TimeCalc.time_add"}
    assert unselected(plan) == {"tests/test_calc.py::test_mul"}
    assert plan.fallbacks == []

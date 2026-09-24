"""Acceptance scenarios from docs/diffcone_coding_agent_handoff.md.

Every scenario mixes pytest-labelled and ASV-labelled targets so that the
engine is exercised as runner-independent. Assertions check exact target
sets and the rules/paths behind them.
"""

from __future__ import annotations

import os

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
    assert not [u for u in plan.unresolved if u.matched_affected_symbols]


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
                "def plugin(name):\n    return importlib.import_module(name.strip())\n\n\n"
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

    matched = {u.symbol: u for u in plan.unresolved if u.matched_affected_symbols}
    assert set(matched) == {"pkg.service.persist"}
    assert matched["pkg.service.persist"].matched_affected_symbols == ("pkg.models.Model.save",)
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
        # A runner integration declares the module and its pytestmark variable
        # as lifecycle dependencies when module-level state governs the test.
        py_target(
            "t::test_a",
            "tests.test_marks.test_a",
            "tests.test_marks",
            "tests.test_marks.pytestmark",
        ),
        py_target("t::test_b", "tests.test_plain.test_b", "tests.test_plain"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {
        "benchmarks.bench.TimeOps": ("body_changed",),
        "tests.test_marks.pytestmark": ("body_changed",),
    }
    assert selected(plan) == {"b.TimeOps.time_add", "b.TimeOps.time_mul", "t::test_a"}
    assert unselected(plan) == {"b.Other.time_other", "t::test_b"}
    r = reason(plan, "b.TimeOps.time_mul")
    assert [s.kind for s in r.path] == ["entry", "defined_in"]
    assert r.changed_symbol == "benchmarks.bench.TimeOps"
    r = reason(plan, "t::test_a")
    assert [s.kind for s in r.path] == ["lifecycle"]
    assert r.changed_symbol == "tests.test_marks.pytestmark"


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


def test_inherited_method_change_reaches_subclass_consumers(repo):
    base = repo.commit(
        {
            "pkg/base.py": (
                "class Base:\n"
                "    def encode(self, x):\n        return x\n\n"
                "    def decode(self, x):\n        return x\n"
            ),
            "pkg/json_codec.py": (
                "from pkg.base import Base\n\n\n"
                "class JsonCodec(Base):\n"
                "    def roundtrip(self, x):\n        return self.decode(self.encode(x))\n\n"
                "    def decode(self, x):\n        return super().decode(x)\n\n\n"
                "class Other(Base):\n"
                "    def only_decode(self, x):\n        return self.decode(x)\n"
            ),
            "tests/test_codec.py": (
                "from pkg.json_codec import JsonCodec, Other\n\n\n"
                "def test_roundtrip():\n    assert JsonCodec().roundtrip(1) == 1\n\n\n"
                "def test_class_attr():\n    assert JsonCodec.encode\n\n\n"
                "def test_other():\n    assert Other().only_decode(1) == 1\n"
            ),
            "benchmarks/bench.py": (
                "from pkg.json_codec import JsonCodec, Other\n\n\n"
                "class Suite:\n"
                "    def setup(self):\n        self.c = JsonCodec()\n\n"
                "    def time_roundtrip(self):\n        self.c.roundtrip(1)\n\n\n"
                "def time_other():\n    Other().only_decode(1)\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/base.py": (
                "class Base:\n"
                "    def encode(self, x):\n        return [x]\n\n"
                "    def decode(self, x):\n        return x\n"
            )
        }
    )
    targets = [
        py_target("t::test_roundtrip", "tests.test_codec.test_roundtrip"),
        py_target("t::test_class_attr", "tests.test_codec.test_class_attr"),
        py_target("t::test_other", "tests.test_codec.test_other"),
        asv_target(
            "b.Suite.time_roundtrip",
            "benchmarks.bench.Suite.time_roundtrip",
            "benchmarks.bench.Suite.setup",
        ),
        asv_target("b.time_other", "benchmarks.bench.time_other"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.base.Base.encode": ("body_changed",)}
    assert selected(plan) == {
        "t::test_roundtrip",
        "t::test_class_attr",
        "b.Suite.time_roundtrip",
    }
    assert unselected(plan) == {"t::test_other", "b.time_other"}
    r = reason(plan, "t::test_class_attr")
    assert not r.conservative
    assert path_ids(r) == [
        "target:pytest:t::test_class_attr",
        "tests.test_codec.test_class_attr",
        "pkg.base.Base.encode",
    ]
    # ``JsonCodec().roundtrip`` is a call-result access, hence conservative,
    # but roundtrip -> self.encode -> Base.encode is a resolved path.
    r = reason(plan, "t::test_roundtrip", "unresolved_name_match")
    assert path_ids(r)[-2:] == ["pkg.json_codec.JsonCodec.roundtrip", "pkg.base.Base.encode"]
    assert r.path[-1].kind == "references"
    # The ASV benchmark reaches it through self.c (instance attribute, name-bounded).
    assert "b.Suite.time_roundtrip" in selected(plan)

    # Changing the overriding decode in JsonCodec must not select Other's consumers.
    head2 = repo.commit(
        {
            "pkg/base.py": (  # restore: head2 must differ from base only in JsonCodec.decode
                "class Base:\n"
                "    def encode(self, x):\n        return x\n\n"
                "    def decode(self, x):\n        return x\n"
            ),
            "pkg/json_codec.py": (
                "from pkg.base import Base\n\n\n"
                "class JsonCodec(Base):\n"
                "    def roundtrip(self, x):\n        return self.decode(self.encode(x))\n\n"
                "    def decode(self, x):\n        return super().decode(x) or x\n\n\n"
                "class Other(Base):\n"
                "    def only_decode(self, x):\n        return self.decode(x)\n"
            ),
        }
    )
    plan2 = repo.plan(base, head2, targets)
    assert changes(plan2) == {"pkg.json_codec.JsonCodec.decode": ("body_changed",)}
    assert "t::test_other" in unselected(plan2) and "b.time_other" in unselected(plan2)
    assert "t::test_roundtrip" in selected(plan2)


def test_constructor_changes_reach_callers_and_dunders_are_not_name_matched(repo):
    base = repo.commit(
        {
            "pkg/models.py": (
                "from external import Thing\n\n\n"
                "class Model(Thing):\n"
                "    def __init__(self):\n        super().__init__()\n\n\n"
                "class Widget:\n"
                "    def __init__(self):\n        self.size = helper()\n\n\n"
                "def helper():\n    return 1\n"
            ),
            "tests/test_models.py": (
                "from pkg.models import Model, Widget\n\n\n"
                "def test_model():\n    assert Model()\n\n\n"
                "def test_widget():\n    assert Widget().size == 1\n"
            ),
            "benchmarks/bench.py": (
                "from pkg.models import Model, Widget\n\n\n"
                "def time_model():\n    Model()\n\n\n"
                "def time_widget():\n    Widget()\n"
            ),
        }
    )
    targets = [
        py_target("t::test_model", "tests.test_models.test_model"),
        py_target("t::test_widget", "tests.test_models.test_widget"),
        asv_target("b.time_model", "benchmarks.bench.time_model"),
        asv_target("b.time_widget", "benchmarks.bench.time_widget"),
    ]
    # A change inside Widget's constructor chain reaches Widget's callers only:
    # Model.__init__ must not be dragged in by name-matching ``__init__``.
    head = repo.commit(
        {
            "pkg/models.py": (
                "from external import Thing\n\n\n"
                "class Model(Thing):\n"
                "    def __init__(self):\n        super().__init__()\n\n\n"
                "class Widget:\n"
                "    def __init__(self):\n        self.size = helper()\n\n\n"
                "def helper():\n    return 2\n"
            )
        }
    )
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.models.helper": ("body_changed",)}
    assert selected(plan) == {"t::test_widget", "b.time_widget"}
    assert unselected(plan) == {"t::test_model", "b.time_model"}
    r = reason(plan, "b.time_widget")
    assert path_ids(r) == [
        "target:asv:b.time_widget",
        "benchmarks.bench.time_widget",
        "pkg.models.Widget.__init__",
        "pkg.models.helper",
    ]
    assert r.path[1].detail == "constructor"
    assert not r.conservative


def test_adding_an_import_binding_is_not_structural(repo):
    base = repo.commit(
        {
            "pkg/tools.py": (
                "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n"
            ),
            "tests/test_tools.py": (
                "from pkg.tools import (\n    a,\n    b,\n)\n\n\n"
                "class Helper:\n    def items(self):\n        return []\n\n\n"
                "def test_a():\n    assert a() == 1\n\n\n"
                "def test_b():\n    assert b() == 2\n"
            ),
            "tests/test_other.py": "def test_items(d):\n    return d.items()\n",
            "benchmarks/bench.py": "from pkg.tools import a\n\n\ndef time_a():\n    a()\n",
        }
    )
    # Add ``c`` to the multi-line import and a test using it.
    head = repo.commit(
        {
            "tests/test_tools.py": (
                "from pkg.tools import (\n    a,\n    b,\n    c,\n)\n\n\n"
                "class Helper:\n    def items(self):\n        return []\n\n\n"
                "def test_a():\n    assert a() == 1\n\n\n"
                "def test_b():\n    assert b() == 2\n\n\n"
                "def test_c():\n    assert c() == 3\n"
            )
        }
    )
    targets = [
        py_target("t::test_a", "tests.test_tools.test_a"),
        py_target("t::test_b", "tests.test_tools.test_b"),
        py_target("t::test_c", "tests.test_tools.test_c"),
        py_target("t::test_items", "tests.test_other.test_items"),
        asv_target("b.time_a", "benchmarks.bench.time_a"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {
        "tests.test_tools": ("imports_added", "dependencies_added"),
        "tests.test_tools.test_c": ("added",),
    }
    assert selected(plan) == {"t::test_c"}
    # Neither the sibling tests nor ``d.items()`` (name-matching Helper.items,
    # which is no longer invalidated) are dragged in.
    assert unselected(plan) == {"t::test_a", "t::test_b", "t::test_items", "b.time_a"}

    # Removing an import binding stays structural: every test in the module.
    head2 = repo.commit(
        {
            "tests/test_tools.py": (
                "from pkg.tools import (\n    a,\n)\n\n\n"
                "class Helper:\n    def items(self):\n        return []\n\n\n"
                "def test_a():\n    assert a() == 1\n\n\n"
                "def test_b():\n    assert b() == 2\n"
            )
        }
    )
    plan2 = repo.plan(base, head2, targets[:2] + targets[3:])
    assert changes(plan2)["tests.test_tools"] == ("definition_changed", "dependencies_changed")
    assert {"t::test_a", "t::test_b"} <= selected(plan2)
    assert "b.time_a" in unselected(plan2)


def test_additive_module_change_does_not_seed_lifecycle_dependents(repo):
    base = repo.commit(
        {
            "pkg/tools.py": "def a():\n    return 1\n\n\ndef c():\n    return 3\n",
            "tests/test_tools.py": (
                "from pkg.tools import (\n    a,\n)\n\n\ndef test_a():\n    assert a() == 1\n"
            ),
        }
    )
    head = repo.commit(
        {
            "tests/test_tools.py": (
                "from pkg.tools import (\n    a,\n    c,\n)\n\n\n"
                "def test_a():\n    assert a() == 1\n\n\n"
                "def test_c():\n    assert c() == 3\n"
            )
        }
    )
    # Discovery lists the test module as a lifecycle dependency of each test.
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    module_dep = next(
        d for d in plan.decisions if d.target.runner_id == "tests/test_tools.py::test_a"
    )
    assert "tests.test_tools" in module_dep.target.lifecycle_dependencies
    assert changes(plan) == {
        "tests.test_tools": ("imports_added", "dependencies_added"),
        "tests.test_tools.test_c": ("added",),
    }
    # The added import is not structural, and the added ``def`` is inert (no
    # decorators, defaults or annotations): it runs nothing at import.
    assert selected(plan) == {"tests/test_tools.py::test_c"}
    assert unselected(plan) == {"tests/test_tools.py::test_a"}

    # A module *body* change (pytestmark) still reaches every test through it.
    head2 = repo.commit(
        {
            "tests/test_tools.py": (
                "import pytest\nfrom pkg.tools import (\n    a,\n)\n\n"
                "pytestmark = pytest.mark.slow\n\n\n"
                "def test_a():\n    assert a() == 1\n"
            )
        }
    )
    plan2 = repo.plan(base, head2, [], discover_runners=["pytest"])
    assert selected(plan2) == {"tests/test_tools.py::test_a"}
    r2 = reason(plan2, "tests/test_tools.py::test_a")
    assert [s.kind for s in r2.path] == ["lifecycle"]
    assert r2.changed_symbol == "tests.test_tools.pytestmark"


def test_dynamic_references_are_bounded_by_the_import_closure(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def f():\n    return 1\n",
            "pkg/other.py": "def g():\n    return 2\n",
            "tests/test_exec.py": (
                "from pkg import core\n\n\n"
                "def make(body):\n    d = {}\n    exec(f'def fn():\\n    {body}', globals(), d)\n"
                "    return d['fn']\n\n\n"
                "def test_made():\n    assert make('return core.f()')() == 1\n"
            ),
            "tests/test_import.py": (
                "import importlib\n\n\n"
                "def test_dyn(name='pkg.other'):\n    assert importlib.import_module(name)\n"
            ),
            "tests/test_other.py": (
                "from pkg.other import g\n\n\ndef test_g():\n    assert g() == 2\n"
            ),
        }
    )
    targets = [
        py_target("t::test_made", "tests.test_exec.test_made"),
        py_target("t::test_dyn", "tests.test_import.test_dyn"),
        py_target("t::test_g", "tests.test_other.test_g"),
    ]
    # A change in pkg.other: not importable from test_exec's module -> exec is
    # not affected; the dynamic import is always affected; test_g resolves.
    head = repo.commit({"pkg/other.py": "def g():\n    return 3\n"})
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_dyn", "t::test_g"}
    assert unselected(plan) == {"t::test_made"}
    # A change in pkg.core, which test_exec imports: exec is affected.
    head2 = repo.commit(
        {"pkg/other.py": "def g():\n    return 2\n", "pkg/core.py": "def f():\n    return 11\n"}
    )
    plan2 = repo.plan(base, head2, targets)
    assert selected(plan2) == {"t::test_made", "t::test_dyn"}
    assert rules(plan2, "t::test_made") == {"dynamic_reference"}
    # A tests-only change elsewhere reaches neither dynamic user.
    head3 = repo.commit(
        {
            "pkg/core.py": "def f():\n    return 1\n",
            "pkg/other.py": "def g():\n    return 2\n",
            "tests/test_other.py": (
                "from pkg.other import g\n\n\ndef test_g():\n    assert g() == 2 or True\n"
            ),
        }
    )
    plan3 = repo.plan(base, head3, targets)
    assert selected(plan3) == {"t::test_g", "t::test_dyn"}


def test_override_change_reaches_callers_of_the_base_template(repo):
    base = repo.commit(
        {
            "pkg/engine.py": (
                "class Base:\n"
                "    def run(self):\n        return self.step() + 1\n\n"
                "    def step(self):\n        return 0\n\n\n"
                "class Sub(Base):\n"
                "    def step(self):\n        return 10\n\n\n"
                "class Other(Base):\n"
                "    def extra(self):\n        return 5\n"
            ),
            "tests/test_engine.py": (
                "from pkg.engine import Sub, Other\n\n\n"
                "def test_sub():\n    assert Sub().run() == 11\n\n\n"
                "def test_other():\n    assert Other().run() == 1\n\n\n"
                "def test_other_extra():\n    assert Other().extra() == 5\n"
            ),
            "benchmarks/bench.py": (
                "from pkg.engine import Sub\n\n\ndef time_sub():\n    Sub().run()\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/engine.py": (
                "class Base:\n"
                "    def run(self):\n        return self.step() + 1\n\n"
                "    def step(self):\n        return 0\n\n\n"
                "class Sub(Base):\n"
                "    def step(self):\n        return 20\n\n\n"
                "class Other(Base):\n"
                "    def extra(self):\n        return 5\n"
            )
        }
    )
    targets = [
        py_target("t::test_sub", "tests.test_engine.test_sub"),
        py_target("t::test_other", "tests.test_engine.test_other"),
        py_target("t::test_other_extra", "tests.test_engine.test_other_extra"),
        asv_target("b.time_sub", "benchmarks.bench.time_sub"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.engine.Sub.step": ("body_changed",)}
    # Sub().run() is a call-result access (name-bounded on ``run``), so the
    # test reaches Base.run conservatively; from there the resolved override
    # edge Base.run -> Sub.step is a real dependency, not a guess.
    assert selected(plan) == {"t::test_sub", "b.time_sub", "t::test_other"}
    assert unselected(plan) == {"t::test_other_extra"}
    r = reason(plan, "t::test_sub", "unresolved_name_match")
    assert path_ids(r)[-2:] == ["pkg.engine.Base.run", "pkg.engine.Sub.step"]
    assert r.path[-1].detail == "override"


def test_docstring_only_changes_carry_no_impact(repo):
    base = repo.commit(
        {
            "pkg/core.py": (
                '"""Core module."""\n\n\n'
                "def hub(x):\n"
                '    """Old doc."""\n'
                "    return x\n\n\n"
                "class K:\n"
                '    """Class doc."""\n\n'
                "    def m(self):\n        return 1\n"
            ),
            "tests/test_core.py": (
                "from pkg.core import hub, K\n\n\n"
                "def test_hub():\n    assert hub(1) == 1\n\n\n"
                "def test_k():\n    assert K().m() == 1\n"
            ),
            "benchmarks/bench.py": "from pkg.core import hub\n\n\ndef time_hub():\n    hub(1)\n",
        }
    )
    head = repo.commit(
        {
            "pkg/core.py": (
                '"""Core module, documented better."""\n\n\n'
                "def hub(x):\n"
                '    """New, longer doc."""\n'
                "    return x\n\n\n"
                "class K:\n"
                '    """Class doc, edited."""\n\n'
                "    def m(self):\n        return 1\n"
            )
        }
    )
    targets = [
        py_target("t::test_hub", "tests.test_core.test_hub"),
        py_target("t::test_k", "tests.test_core.test_k"),
        asv_target("b.time_hub", "benchmarks.bench.time_hub"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {
        "pkg.core": ("docstring_changed",),
        "pkg.core.hub": ("docstring_changed",),
        "pkg.core.K": ("docstring_changed",),
    }
    assert selected(plan) == set()
    # A real body change next to a docstring change is still a body change.
    head2 = repo.commit(
        {
            "pkg/core.py": (
                '"""Core module."""\n\n\n'
                "def hub(x):\n"
                '    """Old doc."""\n'
                "    return x + 0\n\n\n"
                "class K:\n"
                '    """Class doc."""\n\n'
                "    def m(self):\n        return 1\n"
            )
        }
    )
    plan2 = repo.plan(base, head2, targets)
    assert changes(plan2) == {"pkg.core.hub": ("body_changed",)}
    assert selected(plan2) == {"t::test_hub", "b.time_hub"}


def test_module_constant_change_reaches_only_its_users(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "from pkg._make import attrib\n\nib = attrib\nLAZY = {'a'}\n",
            "pkg/_make.py": "def attrib():\n    return 1\n\n\ndef other():\n    return 2\n",
            "tests/test_pkg.py": (
                "import pkg\nfrom pkg._make import other\n\n\n"
                "def test_ib():\n    assert pkg.ib() == 1\n\n\n"
                "def test_lazy():\n    assert 'a' in pkg.LAZY\n\n\n"
                "def test_other():\n    assert other() == 2\n"
            ),
            "benchmarks/bench.py": "import pkg\n\n\ndef time_ib():\n    pkg.ib()\n",
        }
    )
    targets = [
        py_target("t::test_ib", "tests.test_pkg.test_ib"),
        py_target("t::test_lazy", "tests.test_pkg.test_lazy"),
        py_target("t::test_other", "tests.test_pkg.test_other"),
        asv_target("b.time_ib", "benchmarks.bench.time_ib"),
    ]
    # Editing one constant in the package __init__ reaches only its users.
    head = repo.commit(
        {"pkg/__init__.py": "from pkg._make import attrib\n\nib = attrib\nLAZY = {'a', 'b'}\n"}
    )
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.LAZY": ("body_changed",)}
    assert selected(plan) == {"t::test_lazy"}
    # Changing the aliased function reaches users of the alias through the variable.
    head2 = repo.commit(
        {
            "pkg/__init__.py": "from pkg._make import attrib\n\nib = attrib\nLAZY = {'a'}\n",
            "pkg/_make.py": "def attrib():\n    return 11\n\n\ndef other():\n    return 2\n",
        }
    )
    plan2 = repo.plan(base, head2, targets)
    assert selected(plan2) == {"t::test_ib", "b.time_ib"}
    r = reason(plan2, "t::test_ib")
    assert path_ids(r) == [
        "target:pytest:t::test_ib",
        "tests.test_pkg.test_ib",
        "pkg.ib",
        "pkg._make.attrib",
    ]
    # Re-pointing the alias is a body change of the variable: alias users only.
    head3 = repo.commit(
        {
            "pkg/__init__.py": "from pkg._make import attrib, other\n\nib = other\nLAZY = {'a'}\n",
            "pkg/_make.py": "def attrib():\n    return 1\n\n\ndef other():\n    return 2\n",
        }
    )
    plan3 = repo.plan(base, head3, targets)
    assert changes(plan3) == {
        "pkg": ("imports_added", "dependencies_added"),
        "pkg.ib": ("body_changed", "dependencies_changed"),
    }
    assert selected(plan3) == {"t::test_ib", "b.time_ib"}


def test_in_place_mutation_of_a_module_registry_reaches_its_users(repo):
    base = repo.commit(
        {
            "pkg/reg.py": (
                "REGISTRY = {}\n"
                "OTHER = {}\n\n"
                "REGISTRY['a'] = 1\n"
                "if True:\n    OTHER['x'] = 1\n\n\n"
                "def lookup(k):\n    return REGISTRY[k]\n\n\n"
                "def other(k):\n    return OTHER[k]\n"
            ),
            "tests/test_reg.py": (
                "from pkg.reg import lookup, other\n\n\n"
                "def test_lookup():\n    assert lookup('a') == 1\n\n\n"
                "def test_other():\n    assert other('x') == 1\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/reg.py": (
                "REGISTRY = {}\n"
                "OTHER = {}\n\n"
                "REGISTRY['a'] = 1\n"
                "REGISTRY['b'] = 2\n"
                "if True:\n    OTHER['x'] = 1\n\n\n"
                "def lookup(k):\n    return REGISTRY[k]\n\n\n"
                "def other(k):\n    return OTHER[k]\n"
            )
        }
    )
    targets = [
        py_target("t::test_lookup", "tests.test_reg.test_lookup"),
        py_target("t::test_other", "tests.test_reg.test_other"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {
        "pkg.reg": ("body_changed",),
        "pkg.reg.REGISTRY": ("body_changed",),
    }
    assert selected(plan) == {"t::test_lookup"}
    assert unselected(plan) == {"t::test_other"}


def test_readers_depend_on_functions_that_mutate_a_variable(repo):
    base = repo.commit(
        {
            "pkg/reg.py": (
                "INFO = {'a': 1}\n"
                "REGISTRY = {}\n"
                "NAMES = []\n\n\n"
                "def build():\n"
                "    for k, v in INFO.items():\n        REGISTRY[k] = v\n"
                "    NAMES.append('x')\n\n\n"
                "build()\n\n\n"
                "def lookup(k):\n    return REGISTRY[k]\n\n\n"
                "def names():\n    return list(NAMES)\n\n\n"
                "def reader_only():\n    return REGISTRY.get('zzz')\n"
            ),
            "tests/test_reg.py": (
                "from pkg.reg import lookup, names, reader_only\n\n\n"
                "def test_lookup():\n    assert lookup('a') == 1\n\n\n"
                "def test_names():\n    assert names() == ['x']\n\n\n"
                "def test_reader():\n    assert reader_only() is None\n"
            ),
        }
    )
    # Only INFO (read by the writer of REGISTRY) changes.
    head = repo.commit(
        {
            "pkg/reg.py": (
                "INFO = {'a': 1, 'b': 2}\n"
                "REGISTRY = {}\n"
                "NAMES = []\n\n\n"
                "def build():\n"
                "    for k, v in INFO.items():\n        REGISTRY[k] = v\n"
                "    NAMES.append('x')\n\n\n"
                "build()\n\n\n"
                "def lookup(k):\n    return REGISTRY[k]\n\n\n"
                "def names():\n    return list(NAMES)\n\n\n"
                "def reader_only():\n    return REGISTRY.get('zzz')\n"
            )
        }
    )
    targets = [
        py_target("t::test_lookup", "tests.test_reg.test_lookup"),
        py_target("t::test_names", "tests.test_reg.test_names"),
        py_target("t::test_reader", "tests.test_reg.test_reader"),
    ]
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.reg.INFO": ("body_changed",)}
    # REGISTRY readers reach INFO through build(); NAMES readers do too (the
    # writer is shared); ``.get`` is a read, so reader_only is only affected
    # through REGISTRY's writer as well.
    assert selected(plan) == {"t::test_lookup", "t::test_names", "t::test_reader"}
    r = reason(plan, "t::test_lookup")
    assert path_ids(r) == [
        "target:pytest:t::test_lookup",
        "tests.test_reg.test_lookup",
        "pkg.reg.lookup",
        "pkg.reg.REGISTRY",
        "pkg.reg.build",
        "pkg.reg.INFO",
    ]
    assert r.path[3].detail == "mutated_by"


def test_lazy_loader_with_prefixed_import_is_not_an_always_on_seed(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": (
                "import importlib\n\n"
                "_LAZY = {'validators'}\n\n\n"
                "def __getattr__(name):\n"
                "    if name in _LAZY:\n        return importlib.import_module(f'pkg.{name}')\n"
                "    raise AttributeError(name)\n"
            ),
            "pkg/validators.py": "def ne(v):\n    return v\n",
            "pkg/other.py": "def helper():\n    return 1\n",
            "tests/test_pkg.py": (
                "import pkg\nfrom pkg.other import helper\n\n\n"
                "def test_lazy():\n    assert pkg.validators.ne(1) == 1\n\n\n"
                "def test_helper():\n    assert helper() == 1\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "def helper():\n    return 2\n"})
    targets = [
        py_target("t::test_lazy", "tests.test_pkg.test_lazy"),
        py_target("t::test_helper", "tests.test_pkg.test_helper"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_helper"}
    assert not [u for u in plan.unresolved if u.kind == "dynamic"]


def test_prefixed_source_roots_keep_same_named_test_trees_apart(repo):
    """Two packages with their own tests/ trees whose files share names:
    a per-root prefix gives each tree its own module namespace, so one plan
    covers the session that collects both (importlib import mode)."""
    same_test = "from {pkg} import core\n\n\ndef test_run():\n    assert core.run() == {v}\n"
    base = repo.commit(
        {
            "a/src/pa/__init__.py": "",
            "a/src/pa/core.py": "def run():\n    return 1\n",
            "a/tests/test_core.py": same_test.format(pkg="pa", v=1),
            "b/src/pb/__init__.py": "",
            "b/src/pb/core.py": "def run():\n    return 2\n",
            "b/tests/test_core.py": same_test.format(pkg="pb", v=2),
        }
    )
    head = repo.commit({"b/src/pb/core.py": "def run():\n    return 2 + 0\n"})
    roots = ["a/src", "b/src", "a/tests=a_tests", "b/tests=b_tests"]
    plan = repo.plan(base, head, [], source_roots=roots, discover_runners=["pytest"])
    assert not plan.degraded and plan.errors == []
    assert selected(plan) == {"b/tests/test_core.py::test_run"}
    assert unselected(plan) == {"a/tests/test_core.py::test_run"}
    entries = {d.target.runner_id: d.target.entry_symbol for d in plan.decisions}
    assert entries == {
        "a/tests/test_core.py::test_run": "a_tests.test_core.test_run",
        "b/tests/test_core.py::test_run": "b_tests.test_core.test_run",
    }
    # Without the prefixes the two test modules collide and the plan degrades.
    plain = repo.plan(base, head, [], source_roots=["a/src", "b/src", "a/tests", "b/tests"])
    assert plain.degraded
    assert any("also defined by" in e.message for e in plain.errors)


def test_instance_attribute_name_bounds_a_registry_getattr(repo):
    """hatch's plugin registry: ``getattr(hooks, self.identifier)`` where
    every construction passes a literal. The attribute bounds the lookup, so
    a change elsewhere in the registry's import closure (a version string)
    no longer reaches every user of the registry."""
    registry = (
        "from pkg import about, hooks\n\n\n"
        "class Register:\n"
        "    def __init__(self, identifier):\n"
        "        self.identifier = identifier\n\n"
        "    def collect(self):\n"
        "        return getattr(hooks, self.identifier)()\n\n\n"
        "def version():\n    return about.VERSION\n\n\n"
        "def builder():\n    return Register('wheel').collect()\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/about.py": "VERSION = '1.0'\n",
            "pkg/hooks.py": "def wheel():\n    return 'w'\n\n\ndef sdist():\n    return 's'\n",
            "pkg/registry.py": registry,
            "tests/test_registry.py": (
                "from pkg.registry import builder, version\n\n\n"
                "def test_builder():\n    assert builder() == 'w'\n\n\n"
                "def test_version():\n    assert version()\n"
            ),
            "benchmarks/bench_registry.py": (
                "from pkg.registry import builder\n\n\ndef time_builder():\n    builder()\n"
            ),
        }
    )
    targets = [
        py_target("t::test_builder", "tests.test_registry.test_builder"),
        py_target("t::test_version", "tests.test_registry.test_version"),
        asv_target("bench.time_builder", "benchmarks.bench_registry.time_builder"),
    ]
    head = repo.commit({"pkg/about.py": "VERSION = '1.1'\n"})
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_version"}
    assert unselected(plan) == {"t::test_builder", "bench.time_builder"}
    # The hook the attribute names is a real dependency.
    head2 = repo.commit(
        {"pkg/hooks.py": "def wheel():\n    return 'W'\n\n\ndef sdist():\n    return 's'\n"}
    )
    plan2 = repo.plan(head, head2, targets)
    assert selected(plan2) == {"t::test_builder", "bench.time_builder"}
    # ``Register('wheel').collect()`` reaches collect by name (untyped
    # receiver); collect reaches the hook through the bounded getattr.
    assert path_ids(reason(plan2, "t::test_builder", "unresolved_name_match"))[-2:] == [
        "pkg.registry.Register.collect",
        "pkg.hooks.wheel",
    ]
    # Rebinding the attribute outside __init__ makes the lookup dynamic again.
    rename = "\n\n    def rename(self, name):\n        self.identifier = name\n"
    head3 = repo.commit(
        {"pkg/registry.py": registry.replace("\n\n\ndef version()", rename + "\n\ndef version()")}
    )
    head4 = repo.commit({"pkg/about.py": "VERSION = '1.2'\n"})
    plan4 = repo.plan(head3, head4, targets)
    assert selected(plan4) == {"t::test_builder", "t::test_version", "bench.time_builder"}
    assert rules(plan4, "bench.time_builder") == {"dynamic_reference"}


def test_class_defined_in_both_branches_of_an_if_is_one_symbol(repo):
    """A class defined per Python version (anyio) or twice in a data file
    (black) used to collide on its repeated methods and degrade the plan to
    select-all. Each method is one symbol over every definition."""
    worker = (
        "import sys\n\n"
        "if sys.version_info >= (3, 13):\n"
        "    class Worker:\n"
        "        def __init__(self):\n            self.n = 13\n\n"
        "        def run(self):\n            return self.n\n"
        "else:\n"
        "    class Worker:\n"
        "        def __init__(self):\n            self.n = {old}\n\n"
        "        def run(self):\n            return self.n\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/worker.py": worker.format(old=12),
            "pkg/other.py": "def other():\n    return 1\n",
            "tests/test_worker.py": (
                "from pkg.worker import Worker\n\n\ndef test_run():\n    assert Worker().run()\n"
            ),
            "tests/test_other.py": (
                "from pkg.other import other\n\n\ndef test_other():\n    assert other()\n"
            ),
            "benchmarks/bench_worker.py": (
                "from pkg.worker import Worker\n\n\ndef time_run():\n    Worker().run()\n"
            ),
        }
    )
    targets = [
        py_target("t::test_run", "tests.test_worker.test_run"),
        py_target("t::test_other", "tests.test_other.test_other"),
        asv_target("bench.time_run", "benchmarks.bench_worker.time_run"),
    ]
    head = repo.commit({"pkg/worker.py": worker.format(old=11)})
    plan = repo.plan(base, head, targets)
    assert not plan.degraded and plan.errors == []
    assert changes(plan) == {"pkg.worker.Worker.__init__": ("body_changed",)}
    assert selected(plan) == {"t::test_run", "bench.time_run"}
    assert unselected(plan) == {"t::test_other"}


def test_package_binding_that_shadows_a_submodule(repo):
    """tenacity, pip, poetry: ``pkg/__init__.py`` binds ``retry`` next to
    ``pkg/retry.py``. Both used to claim the identity ``pkg.retry`` and the
    plan degraded to select-all. The module keeps ``pkg.retry``, the binding
    is ``pkg.__init__.retry``, and the attribute ``pkg.retry`` denotes both.
    scrapy's variant is a test function shadowing a test module."""
    retry = "import os\n\nos.environ.setdefault('DELAY', '{delay}')\n\n\n"
    retry += "def backoff(f):\n    return f\n\n\ndef jitter():\n    return {value}\n"
    init = "from pkg.retry import backoff\n\n\ndef retry(f):\n    return {body}\n"
    base = repo.commit(
        {
            "pkg/__init__.py": init.format(body="backoff(f)"),
            "pkg/retry.py": retry.format(delay=1, value=0),
            "tests/test_attr.py": "import pkg\n\n\ndef test_attr():\n    assert pkg.retry(1)\n",
            "tests/test_jitter.py": (
                "from pkg.retry import jitter\n\n\ndef test_jitter():\n    assert jitter() == 0\n"
            ),
            "tests/test_walk/__init__.py": "def test_walk():\n    assert True\n",
            "tests/test_walk/test_walk.py": "def test_inner():\n    assert True\n",
            "benchmarks/bench_retry.py": (
                "from pkg import retry\n\n\ndef time_retry():\n    retry(1)\n"
            ),
        }
    )
    targets = [
        py_target("t::test_attr", "tests.test_attr.test_attr"),
        py_target("t::test_jitter", "tests.test_jitter.test_jitter"),
        asv_target("bench.time_retry", "benchmarks.bench_retry.time_retry"),
    ]
    # The binding changes: its users are selected, the submodule's are not.
    head = repo.commit({"pkg/__init__.py": init.format(body="f")})
    plan = repo.plan(base, head, targets)
    assert not plan.degraded and plan.errors == []
    assert set(changes(plan)) == {"pkg.__init__.retry"}
    assert selected(plan) == {"t::test_attr", "bench.time_retry"}
    assert unselected(plan) == {"t::test_jitter"}
    # A function of the submodule changes: only its own users.
    head2 = repo.commit({"pkg/retry.py": retry.format(delay=1, value=1)})
    assert selected(repo.plan(head, head2, targets)) == {"t::test_jitter"}
    # The submodule's own init code changes: ``pkg.retry`` may denote the
    # module, so the attribute's users are selected; test_jitter only imports
    # a function that does not read module state (design.md, module bodies).
    head3 = repo.commit({"pkg/retry.py": retry.format(delay=2, value=1)})
    plan3 = repo.plan(head2, head3, targets)
    assert changes(plan3) == {"pkg.retry": ("body_changed",)}
    assert selected(plan3) == {"t::test_attr", "bench.time_retry"}
    # scrapy: a test function in ``tests/test_walk/__init__.py`` (which pytest
    # does not collect) shadows the test module next to it.
    discovered = repo.plan(base, head, [], discover_runners=["pytest"])
    assert not discovered.degraded and discovered.errors == []
    entries = {d.target.runner_id: d.target.entry_symbol for d in discovered.decisions}
    inner = "tests/test_walk/test_walk.py::test_inner"
    assert entries[inner] == "tests.test_walk.test_walk.test_inner"


def test_subclassing_runs_the_base_init_subclass(repo):
    """flask's ``MethodView.__init_subclass__`` reads ``http_method_funcs``
    whenever a test defines a view class; the census found those tests
    selected only through a dynamic fallback. Class creation now depends on
    the ``__init_subclass__`` its bases provide and on a metaclass's
    ``__new__``/``__init__``."""
    views = (
        "HTTP = frozenset({{{methods}}})\n\n\n"
        "class MethodView:\n"
        "    def __init_subclass__(cls, **kwargs):\n"
        "        super().__init_subclass__(**kwargs)\n"
        "        cls.methods = {{m for m in HTTP if m in cls.__dict__}}\n\n\n"
        "class Registry(type):\n"
        "    def __init__(cls, name, bases, ns):\n"
        "        super().__init__(name, bases, ns)\n"
        "        cls.registered = {registered}\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/views.py": views.format(methods="'get', 'post'", registered=True),
            "tests/test_nested.py": (
                "from pkg.views import MethodView\n\n\n"
                "def test_nested():\n"
                "    class Index(MethodView):\n"
                "        def get(self):\n            return 'x'\n\n"
                "    assert Index.methods == {'get'}\n"
            ),
            "tests/test_meta.py": (
                "from pkg.views import Registry\n\n\n"
                "class Plugin(metaclass=Registry):\n    pass\n\n\n"
                "def test_plugin():\n    assert Plugin.registered\n"
            ),
            "tests/test_other.py": "def test_other():\n    assert True\n",
            "benchmarks/bench_views.py": (
                "from pkg.views import MethodView\n\n\n"
                "def time_define():\n"
                "    class V(MethodView):\n        pass\n"
            ),
        }
    )
    targets = [
        py_target("t::test_nested", "tests.test_nested.test_nested"),
        # Discovery makes a test depend on its module; the class statement runs there.
        py_target("t::test_plugin", "tests.test_meta.test_plugin", "tests.test_meta"),
        py_target("t::test_other", "tests.test_other.test_other"),
        asv_target("bench.time_define", "benchmarks.bench_views.time_define"),
    ]
    head = repo.commit({"pkg/views.py": views.format(methods="'get'", registered=True)})
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.views.HTTP": ("body_changed",)}
    # HTTP runs at import: test_meta imports pkg.views, so test_plugin (which
    # depends on its module) is selected too; test_other imports nothing.
    assert selected(plan) == {"t::test_nested", "bench.time_define", "t::test_plugin"}
    assert unselected(plan) == {"t::test_other"}
    assert path_ids(reason(plan, "t::test_nested"))[-3:] == [
        "tests.test_nested.test_nested",
        "pkg.views.MethodView.__init_subclass__",
        "pkg.views.HTTP",
    ]
    head2 = repo.commit({"pkg/views.py": views.format(methods="'get'", registered=False)})
    plan2 = repo.plan(head, head2, targets)
    assert selected(plan2) == {"t::test_plugin"}
    assert "pkg.views.Registry.__init__" in path_ids(reason(plan2, "t::test_plugin"))


def test_special_methods_reach_users_of_their_class(repo):
    """Special methods run without being named: ``==`` runs ``__eq__``,
    ``len()`` runs ``__len__``, calling an instance runs ``__call__``
    (tenacity's ``@retry`` returns a wrapper that calls a ``Retrying``
    instance). A class depends on its special methods, so their changes
    reach every user of the class, including through factories."""
    retrying = (
        "class Retrying:\n"
        "    def __call__(self, fn, *args):\n        return fn(*args) {op}\n\n"
        "    def __eq__(self, other):\n        return isinstance(other, Retrying)\n\n"
        "    def __hash__(self):\n        return 0\n\n"
        "    def wraps(self, fn):\n"
        "        def wrapped(*args):\n"
        "            copy = Retrying()\n"
        "            return copy(fn, *args)\n"
        "        return wrapped\n\n\n"
        "def retry(fn):\n    return Retrying().wraps(fn)\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/retrying.py": retrying.format(op="+ 0"),
            "pkg/other.py": "def other():\n    return 1\n",
            "tests/test_retry.py": (
                "from pkg.retrying import retry\n\n\n"
                "@retry\ndef answer():\n    return 42\n\n\n"
                "def test_answer():\n    assert answer() == 42\n"
            ),
            "tests/test_other.py": (
                "from pkg.other import other\n\n\ndef test_other():\n    assert other()\n"
            ),
            "benchmarks/bench_retry.py": (
                "from pkg.retrying import retry\n\n\ndef time_retry():\n    retry(len)('x')\n"
            ),
        }
    )
    targets = [
        py_target("t::test_answer", "tests.test_retry.test_answer"),
        py_target("t::test_other", "tests.test_other.test_other"),
        asv_target("bench.time_retry", "benchmarks.bench_retry.time_retry"),
    ]
    head = repo.commit({"pkg/retrying.py": retrying.format(op="- 0")})
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.retrying.Retrying.__call__": ("body_changed",)}
    assert selected(plan) == {"t::test_answer", "bench.time_retry"}
    assert unselected(plan) == {"t::test_other"}
    assert path_ids(reason(plan, "t::test_answer"))[-2:] == [
        "pkg.retrying.Retrying",
        "pkg.retrying.Retrying.__call__",
    ]
    # Another special method: the same users, nothing else.
    head2 = repo.commit(
        {"pkg/retrying.py": retrying.format(op="- 0").replace("return 0\n", "return 1\n")}
    )
    plan2 = repo.plan(head, head2, targets)
    assert changes(plan2) == {"pkg.retrying.Retrying.__hash__": ("body_changed",)}
    assert selected(plan2) == {"t::test_answer", "bench.time_retry"}


def test_methods_reached_through_unknown_or_opaque_values_are_name_bounded(repo):
    """``client.session.send()`` on an unknown ``client`` used to record only
    ``session``, and ``DEFAULT.send()`` on a module-level instance nothing
    after the variable; a change to ``send`` was missed. Every name after
    the point where resolution stops is a name-bounded reference."""
    transport = "class Transport:\n    def send(self):\n        return {value}\n"
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/transport.py": transport.format(value=1),
            "pkg/defaults.py": "from pkg.transport import Transport\n\nDEFAULT = Transport()\n",
            "tests/test_chain.py": ("def test_chain(client):\n    assert client.session.send()\n"),
            "tests/test_default.py": (
                "from pkg.defaults import DEFAULT\n\n\n"
                "def test_default():\n    assert DEFAULT.send()\n"
            ),
            "benchmarks/bench_chain.py": "def time_chain(client):\n    client.session.send()\n",
        }
    )
    targets = [
        py_target("t::test_chain", "tests.test_chain.test_chain"),
        py_target("t::test_default", "tests.test_default.test_default"),
        asv_target("bench.time_chain", "benchmarks.bench_chain.time_chain"),
    ]
    head = repo.commit({"pkg/transport.py": transport.format(value=2)})
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.transport.Transport.send": ("body_changed",)}
    assert selected(plan) == {"t::test_chain", "t::test_default", "bench.time_chain"}
    for rid in ("t::test_chain", "t::test_default", "bench.time_chain"):
        assert "unresolved_name_match" in rules(plan, rid), rid


def test_tests_in_a_symlinked_directory_are_discovered_and_selected(repo):
    """pydantic: ``tests/pydantic_core -> ../pydantic-core/tests``. pytest
    collects through the link; the snapshot readers used to skip the link
    (git stores it as a blob), so those tests were never targets."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/tz.py": "def offset():\n    return 0\n",
            "pkg-core/tests/test_tz.py": (
                "from pkg.tz import offset\n\n\ndef test_offset():\n    assert offset() == 0\n"
            ),
            "tests/test_other.py": "def test_other():\n    assert True\n",
        }
    )
    os.symlink("../pkg-core/tests", repo.path / "tests" / "pkg_core")
    base = repo.commit({})
    linked = "tests/pkg_core/test_tz.py::test_offset"
    head = repo.commit({"pkg/tz.py": "def offset():\n    return 1\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert not plan.degraded and plan.errors == []
    assert selected(plan) == {linked}
    assert "tests/test_other.py::test_other" in unselected(plan)
    entries = {d.target.runner_id: d.target.entry_symbol for d in plan.decisions}
    assert entries[linked] == "tests.pkg_core.test_tz.test_offset"
    # The same through the staged index and the working tree.
    (repo.path / "pkg" / "tz.py").write_text("def offset():\n    return 2\n", "utf-8")
    assert selected(repo.plan(head, "WORKTREE", [], discover_runners=["pytest"])) == {linked}
    repo.git("add", "pkg/tz.py")
    assert selected(repo.plan(head, "INDEX", [], discover_runners=["pytest"])) == {linked}


def test_code_the_runner_itself_runs_selects_all_of_its_targets(repo):
    """pluggy: pytest runs pluggy's hook machinery for every test, so a
    change there can affect any test although no test references it. The
    same repository's ASV benchmarks are unaffected, and a packaging module
    pytest does not import is planned normally."""
    base = repo.commit(
        {
            "src/pluggy/__init__.py": "from pluggy._hooks import HookCaller\n",
            "src/pluggy/_hooks.py": (
                "class HookCaller:\n    def __call__(self):\n        return self.verify()\n\n"
                "    def verify(self):\n        return True\n"
            ),
            "src/pluggy/_version.py": "VERSION = '1'\n",
            "src/packaging/__init__.py": "",
            "src/packaging/version.py": "def parse(v):\n    return v\n",
            "src/packaging/tags.py": "def sys_tags():\n    return []\n",
            "tests/test_version.py": (
                "from pluggy._version import VERSION\n\n\ndef test_version():\n    assert VERSION\n"
            ),
            "tests/test_tags.py": (
                "from packaging.tags import sys_tags\n\n\n"
                "def test_tags():\n    assert sys_tags() == []\n"
            ),
            "benchmarks/bench_version.py": (
                "from pluggy._version import VERSION\n\n\ndef time_version():\n    return VERSION\n"
            ),
        }
    )
    targets = [
        py_target("t::test_version", "tests.test_version.test_version"),
        py_target("t::test_tags", "tests.test_tags.test_tags"),
        asv_target("bench.time_version", "benchmarks.bench_version.time_version"),
    ]
    roots = ["src", "."]
    head = repo.commit(
        {
            "src/pluggy/_hooks.py": (
                "class HookCaller:\n    def __call__(self):\n        return self.verify()\n\n"
                "    def verify(self):\n        return 1\n"
            )
        }
    )
    plan = repo.plan(base, head, targets, source_roots=roots)
    assert selected(plan) == {"t::test_version", "t::test_tags"}
    assert unselected(plan) == {"bench.time_version"}
    assert rules(plan, "t::test_tags") == {"runner_dependency"}
    # A packaging module pytest does not import: only its own users.
    head2 = repo.commit({"src/packaging/tags.py": "def sys_tags():\n    return [1]\n"})
    plan2 = repo.plan(head, head2, targets, source_roots=roots)
    assert selected(plan2) == {"t::test_tags"}
    assert rules(plan2, "t::test_tags") == {"dependency"}
    # packaging.version is imported by pytest itself.
    head3 = repo.commit({"src/packaging/version.py": "def parse(v):\n    return str(v)\n"})
    plan3 = repo.plan(head2, head3, targets, source_roots=roots)
    assert selected(plan3) == {"t::test_version", "t::test_tags"}


def test_methods_of_classes_with_external_bases_are_reached_through_the_class(repo):
    """starlette: ``TestClient`` is an ``httpx.Client`` built around
    ``_TestClientTransport(httpx.BaseTransport)``, and httpx calls
    ``handle_request`` for every request; nothing in the source roots calls
    it. A class with a base outside the source roots depends on every
    method it defines; structural bases (``abc.ABC``, ``Generic[T]``,
    builtins) do not count."""
    client = (
        "import abc\n\nimport httpx\n\n\n"
        "class Transport(httpx.BaseTransport):\n"
        "    def handle_request(self, request):\n        return {value}\n\n\n"
        "class Client(httpx.Client):\n"
        "    def __init__(self):\n        super().__init__(transport=Transport())\n\n\n"
        "class Shape(abc.ABC):\n"
        "    def area(self):\n        return {area}\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/client.py": client.format(value=1, area=0),
            "tests/test_client.py": (
                "from pkg.client import Client\n\n\n"
                "def test_get():\n    assert Client().get('http://x/')\n"
            ),
            "tests/test_shape.py": (
                "from pkg.client import Shape\n\n\ndef test_shape():\n    assert Shape\n"
            ),
            "benchmarks/bench_client.py": (
                "from pkg.client import Client\n\n\ndef time_get():\n    Client().get('http://x/')\n"
            ),
        }
    )
    targets = [
        py_target("t::test_get", "tests.test_client.test_get"),
        py_target("t::test_shape", "tests.test_shape.test_shape"),
        asv_target("bench.time_get", "benchmarks.bench_client.time_get"),
    ]
    head = repo.commit({"pkg/client.py": client.format(value=2, area=0)})
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.client.Transport.handle_request": ("body_changed",)}
    assert selected(plan) == {"t::test_get", "bench.time_get"}
    assert unselected(plan) == {"t::test_shape"}
    assert path_ids(reason(plan, "t::test_get"))[-2:] == [
        "pkg.client.Transport",
        "pkg.client.Transport.handle_request",
    ]
    # An abc.ABC subclass's plain method is not reached through the class.
    head2 = repo.commit({"pkg/client.py": client.format(value=2, area=1)})
    assert selected(repo.plan(head, head2, targets)) == set()


def test_import_time_changes_reach_every_transitive_importer(repo):
    """httpx and trio re-import their package in a test, and every test
    imports at collection: a change to code that runs at import (a
    module-level statement, a constant, a function that import-time code
    calls) reaches every target whose module imports the changed module,
    directly or through other modules. A plain function body change does
    not run at import and reaches only its callers."""
    registry = (
        "REGISTRY = {{}}\n\n\n"
        "def register(name):\n    REGISTRY[name] = {value}\n\n\n"
        "def lookup(name):\n    return REGISTRY.get(name)\n\n\n"
        "register('default')\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/registry.py": registry.format(value=1),
            "pkg/api.py": "from pkg import registry\n\n\ndef ping():\n    return 'pong'\n",
            "tests/test_api.py": (
                "from pkg.api import ping\n\n\ndef test_ping():\n    assert ping() == 'pong'\n"
            ),
            "tests/test_plain.py": "def test_plain():\n    assert True\n",
            "benchmarks/bench_api.py": (
                "from pkg.api import ping\n\n\ndef time_ping():\n    ping()\n"
            ),
        }
    )
    targets = [
        # Discovery makes each test depend on its own module.
        py_target("t::test_ping", "tests.test_api.test_ping", "tests.test_api"),
        py_target("t::test_plain", "tests.test_plain.test_plain", "tests.test_plain"),
        asv_target("bench.time_ping", "benchmarks.bench_api.time_ping", "benchmarks.bench_api"),
    ]
    # ``register`` runs at import: test_api imports pkg.api, which imports
    # pkg.registry, so its tests are selected though ping never touches it.
    head = repo.commit({"pkg/registry.py": registry.format(value=2)})
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.registry.register": ("body_changed",)}
    assert selected(plan) == {"t::test_ping", "bench.time_ping"}
    assert unselected(plan) == {"t::test_plain"}
    assert path_ids(reason(plan, "t::test_ping"))[-3:] == [
        "pkg.api",
        "pkg.registry",
        "pkg.registry.register",
    ]
    # ``lookup`` does not run at import and nothing calls it.
    head2 = repo.commit(
        {"pkg/registry.py": registry.format(value=2).replace("REGISTRY.get(name)", "None")}
    )
    assert selected(repo.plan(head, head2, targets)) == set()


def test_annotation_only_changes_do_not_run_at_import_under_future_annotations(repo):
    """click: a type-hint change in a module that ``click/__init__.py``
    imports selected every test under the import-time rule. With ``from
    __future__ import annotations`` annotations are never evaluated at
    import, so an annotation-only change reaches the function's callers
    (typer and pydantic read annotations when called) but not every
    importer. Without the future import, or with a decorator that could
    read them, it still runs at import."""
    lazy = (
        "from __future__ import annotations\n\n\n"
        "def edit(text: {hint}) -> str:\n    return text\n\n\n"
        "def other() -> int:\n    return 1\n"
    )
    eager = lazy.replace("from __future__ import annotations\n\n\n", "")
    decorated = lazy.replace("def edit", "def register(f):\n    return f\n\n\n@register\ndef edit")
    tests = {
        "tests/test_edit.py": (
            "from pkg.termui import edit\n\n\ndef test_edit():\n    assert edit('x') == 'x'\n"
        ),
        "tests/test_other.py": (
            "from pkg.termui import other\n\n\ndef test_other():\n    assert other() == 1\n"
        ),
        "benchmarks/bench_edit.py": (
            "from pkg.termui import edit\n\n\ndef time_edit():\n    edit('x')\n"
        ),
    }
    targets = [
        py_target("t::test_edit", "tests.test_edit.test_edit", "tests.test_edit"),
        py_target("t::test_other", "tests.test_other.test_other", "tests.test_other"),
        asv_target("bench.time_edit", "benchmarks.bench_edit.time_edit", "benchmarks.bench_edit"),
    ]
    everything = {"t::test_edit", "t::test_other", "bench.time_edit"}
    for source, expected, kind in (
        (lazy, {"t::test_edit", "bench.time_edit"}, "annotations_changed"),
        (eager, everything, "definition_changed"),
        (decorated, everything, "definition_changed"),
    ):
        base = repo.commit(
            {"pkg/__init__.py": "", "pkg/termui.py": source.format(hint="str"), **tests}
        )
        head = repo.commit({"pkg/termui.py": source.format(hint="str | bytes")})
        plan = repo.plan(base, head, targets)
        assert changes(plan) == {"pkg.termui.edit": (kind,)}, kind
        assert selected(plan) == expected, kind


def test_an_inert_def_runs_nothing_at_import_but_a_default_does(repo):
    """click 2103e15 added a plain helper and changed another's parameters in
    a module every test imports. A ``def`` with inert decorators, literal
    defaults and no evaluated annotations only binds its name, so it does
    not seed the module; a default expression runs at import and does."""
    base = repo.commit(
        {
            "pkg/__init__.py": "from pkg import pager\n",
            "pkg/pager.py": "def page(text):\n    return text\n",
            "tests/test_page.py": (
                "from pkg.pager import page\n\n\ndef test_page():\n    assert page('x') == 'x'\n"
            ),
            "tests/test_pkg.py": "import pkg\n\n\ndef test_pkg():\n    assert pkg\n",
            "benchmarks/bench_page.py": (
                "from pkg.pager import page\n\n\ndef time_page():\n    page('x')\n"
            ),
        }
    )
    targets = [
        py_target("t::test_page", "tests.test_page.test_page", "tests.test_page"),
        py_target("t::test_pkg", "tests.test_pkg.test_pkg", "tests.test_pkg"),
        asv_target("bench.time_page", "benchmarks.bench_page.time_page", "benchmarks.bench_page"),
    ]
    # A new plain helper and a new literal-default parameter: callers only.
    head = repo.commit(
        {
            "pkg/pager.py": (
                "def page(text, raw=False):\n    return text\n\n\n"
                "def uses_raw_mode():\n    return False\n"
            )
        }
    )
    plan = repo.plan(base, head, targets)
    assert set(changes(plan)) == {"pkg.pager.page", "pkg.pager.uses_raw_mode"}
    assert selected(plan) == {"t::test_page", "bench.time_page"}
    assert unselected(plan) == {"t::test_pkg"}
    # A default that calls something runs at import: every importer.
    head2 = repo.commit(
        {"pkg/pager.py": "import os\n\n\ndef page(text, raw=os.getpid()):\n    return text\n"}
    )
    assert selected(repo.plan(head, head2, targets)) == {
        "t::test_page",
        "t::test_pkg",
        "bench.time_page",
    }


# Review findings (a714b78..01cd1c4): each is a way a change reached a test
# that the plan did not select. The changed symbol is always ``pkg.mod.evil``
# and the test reaches it only through the construct under test.

_EVIL = "def a():\n    return 1\n\n\ndef evil():\n    return {}\n"


def _evil_reaches(repo, core: str, test: str, extra: dict | None = None) -> set[str]:
    files = {
        "pkg/__init__.py": "",
        "pkg/mod.py": _EVIL.format(2),
        "pkg/core.py": core,
        "tests/test_s.py": test,
        "benchmarks/bench_s.py": "def time_nothing():\n    pass\n",
        **(extra or {}),
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/mod.py": _EVIL.format(3)})
    targets = [
        py_target("t::test_s", "tests.test_s.test_s"),
        asv_target("bench.time_nothing", "benchmarks.bench_s.time_nothing"),
    ]
    plan = repo.plan(base, head, targets)
    assert "bench.time_nothing" in unselected(plan)
    return selected(plan)


def test_attribute_writes_of_a_subclass_of_an_escaping_class_unbound_it(repo):
    core = (
        "from pkg import mod\n\n\nclass Foo:\n    def __init__(self):\n        self.name = 'a'\n\n"
        "    def run(self):\n        return getattr(mod, self.name)()\n\n\n"
        "class Bar:\n    pass\n\n\nBase = Foo if True else Bar\n\n\n"
        "class S(Base):\n    def __init__(self):\n        self.name = 'evil'\n"
    )
    test = "from pkg.core import S\n\n\ndef test_s():\n    assert S().run() == 2\n"
    assert _evil_reaches(repo, core, test) == {"t::test_s"}


def test_names_recorded_by_one_expansion_unbound_another(repo):
    core = (
        "from pkg import mod\n\n\nclass Handler:\n    def handle(self, name):\n"
        "        return getattr(mod, name)()\n\n\n"
        "class Disp:\n    def __init__(self):\n        self.meth = 'handle'\n\n"
        "    def go(self, h):\n        return getattr(h, self.meth)('evil')\n\n\n"
        "def main():\n    return Handler.handle(Handler(), 'a')\n"
    )
    test = (
        "from pkg.core import Disp, Handler\n\n\n"
        "def test_s():\n    assert Disp().go(Handler()) == 2\n"
    )
    assert _evil_reaches(repo, core, test) == {"t::test_s"}


def test_a_project_function_named_cast_is_not_a_type_position(repo):
    core = (
        "from pkg import mod\n\n\ndef cast(kind, value):\n    return kind(value)\n\n\n"
        "class Foo:\n    def __init__(self, name):\n        self.name = name\n\n"
        "    def run(self):\n        return getattr(mod, self.name)()\n\n\n"
        "def default():\n    return Foo('a')\n\n\n"
        "def special():\n    return cast(Foo, 'evil')\n"
    )
    test = "from pkg.core import special\n\n\ndef test_s():\n    assert special().run() == 2\n"
    assert _evil_reaches(repo, core, test) == {"t::test_s"}


def test_writes_through_another_objects_dict_unbound_attributes(repo):
    test = (
        "from pkg.core import Foo, tweak\n\n\ndef test_s():\n    assert tweak(Foo()).run() == 2\n"
    )
    for body in (
        "    d = obj.__dict__\n    d['name'] = 'evil'\n",
        "    obj.__dict__ = {'name': 'evil'}\n",
        "    obj.__dict__ |= {'name': 'evil'}\n",
    ):
        core = (
            "from pkg import mod\n\n\nclass Foo:\n"
            "    def __init__(self):\n        self.name = 'a'\n\n"
            "    def run(self):\n        return getattr(mod, self.name)()\n\n\n"
            f"def tweak(obj):\n{body}    return obj\n"
        )
        assert _evil_reaches(repo, core, test) == {"t::test_s"}, body


def test_deferred_getattr_records_names_after_a_stopped_chain(repo):
    core = (
        "from pkg import mod\n\n\nclass Holder:\n"
        "    def __init__(self):\n        self.inner = mod\n\n\n"
        "def call(h, name):\n    return getattr(h.inner, name)()\n\n\n"
        "def special():\n    return call(Holder(), 'evil')\n"
    )
    test = "from pkg.core import special\n\n\ndef test_s():\n    assert special() == 2\n"
    assert _evil_reaches(repo, core, test) == {"t::test_s"}


def test_a_package_re_export_of_its_submodules_function_is_both(repo):
    """``pkg/__init__.py: from .main import main``: ``pkg.main`` and ``from
    pkg import main`` are the function (and possibly the module)."""
    main = "def main():\n    return {}\n"
    base = repo.commit(
        {
            "pkg/__init__.py": "from .main import main\n",
            "pkg/main.py": main.format(1),
            "tests/test_a.py": "from pkg import main\n\n\ndef test_a():\n    assert main() == 1\n",
            "tests/test_c.py": "import pkg\n\n\ndef test_c():\n    assert pkg.main() == 1\n",
            "benchmarks/bench_m.py": "import pkg\n\n\ndef time_main():\n    pkg.main()\n",
        }
    )
    head = repo.commit({"pkg/main.py": main.format(2)})
    targets = [
        py_target("t::test_a", "tests.test_a.test_a"),
        py_target("t::test_c", "tests.test_c.test_c"),
        asv_target("bench.time_main", "benchmarks.bench_m.time_main"),
    ]
    assert selected(repo.plan(base, head, targets)) == {"t::test_a", "t::test_c", "bench.time_main"}


def test_constructing_a_class_reaches_its_new(repo):
    core = (
        "class Foo:\n    def __new__(cls):\n        inst = super().__new__(cls)\n"
        "        inst.v = {}\n        return inst\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/core.py": core.format(1),
            "tests/test_f.py": (
                "from pkg.core import Foo\n\n\ndef test_f():\n    assert Foo().v == 1\n"
            ),
            "benchmarks/bench_f.py": "from pkg.core import Foo\n\n\ndef time_f():\n    Foo()\n",
        }
    )
    head = repo.commit({"pkg/core.py": core.format(2)})
    targets = [
        py_target("t::test_f", "tests.test_f.test_f"),
        asv_target("bench.time_f", "benchmarks.bench_f.time_f"),
    ]
    assert selected(repo.plan(base, head, targets)) == {"t::test_f", "bench.time_f"}


def test_a_project_decorator_named_final_is_not_inert(repo):
    reg = (
        "REG = {}\n\n\ndef final(f):\n"
        "    REG[f.__name__] = (f.__defaults__, f.__annotations__)\n    return f\n"
    )
    plugins = (
        "from __future__ import annotations\n\nfrom pkg.reg import final\n\n\n"
        "@final\ndef handler(x: {hint} = {default}):\n    return x\n"
    )
    test = (
        "import pkg.plugins\nfrom pkg.reg import REG\n\n\n"
        "def test_r():\n    assert REG['handler'][0] == (1,)\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/reg.py": reg,
            "pkg/plugins.py": plugins.format(hint="int", default=1),
            "tests/test_r.py": test,
        }
    )
    targets = [py_target("t::test_r", "tests.test_r.test_r", "tests.test_r")]
    for hint, default in (("int", 2), ("str", 1)):
        head = repo.commit({"pkg/plugins.py": plugins.format(hint=hint, default=default)})
        assert selected(repo.plan(base, head, targets)) == {"t::test_r"}, (hint, default)


def test_a_package_importing_a_missing_submodule_of_itself_does_not_recurse(repo):
    """``pkg/__init__.py: from pkg import ext`` with no ``ext`` module (a
    compiled extension, as in pandas) used to recurse without end."""
    base = repo.commit(
        {
            "pkg/__init__.py": "from pkg import ext\n",
            "pkg/mod.py": "def f():\n    return 1\n",
            "tests/test_f.py": "from pkg.mod import f\n\n\ndef test_f():\n    assert f()\n",
        }
    )
    head = repo.commit({"pkg/mod.py": "def f():\n    return 2\n"})
    plan = repo.plan(base, head, [py_target("t::test_f", "tests.test_f.test_f")])
    assert not plan.degraded and selected(plan) == {"t::test_f"}


def test_a_class_named_in_an_annotation_may_be_built_by_a_framework(repo):
    """injector builds ``Service()`` from ``App.__init__``'s annotation with
    the default argument, which no visible call passes; bounding
    ``self.name`` by the visible ``Service('a')`` missed ``mod.evil``."""
    core = (
        "from pkg import mod\n\n\n"
        "class Service:\n    def __init__(self, name: str = 'evil'):\n        self.name = name\n\n"
        "    def run(self):\n        return getattr(mod, self.name)()\n\n\n"
        "class App:\n    def __init__(self, service: Service):\n"
        "        self.service = service\n\n\n"
        "def manual():\n    return Service('a')\n"
    )
    test = (
        "from injector import Injector\nfrom pkg.core import App\n\n\n"
        "def test_s():\n    assert Injector().get(App).service.run() == 2\n"
    )
    assert _evil_reaches(repo, core, test) == {"t::test_s"}


def test_doctests_collected_by_pytest_are_targets(repo):
    """injector runs ``--doctest-modules --doctest-glob=*.md``: pytest
    collects docstring examples and README.md, discovery did not, so they
    could never be selected. A doctest runs with its module's globals, so it
    depends on the module's import closure; its docstring is the test; a
    text file is not read by the index, so it is always selected."""
    helpers = (
        '"""Helpers.\n\n>>> from pkg.helpers import double\n>>> double(2)\n4\n"""\n\n'
        "from pkg import core\n\n\n"
        'def double(x):\n    """Double it.\n\n    >>> double(3)\n    6\n    """\n'
        "    return core.times(x, 2)\n\n\n"
        'def plain():\n    """No examples."""\n    return 1\n'
    )
    base = repo.commit(
        {
            "pytest.ini": "[pytest]\naddopts = --doctest-modules --doctest-glob=*.md\n",
            "pkg/__init__.py": "",
            "pkg/core.py": "def times(a, b):\n    return a * b\n",
            "pkg/other.py": "def other():\n    return 1\n",
            "pkg/helpers.py": helpers,
            "README.md": "Usage:\n\n>>> 1 + 1\n2\n",
            "NOTES.md": "No examples here.\n",
            "tests/test_other.py": (
                "from pkg.other import other\n\n\ndef test_other():\n    assert other()\n"
            ),
        }
    )
    discovered = {
        d.target.runner_id: d.target
        for d in repo.plan(base, base, [], discover_runners=["pytest"]).decisions
    }
    assert set(discovered) == {
        "pkg/helpers.py::pkg.helpers",
        "pkg/helpers.py::pkg.helpers.double",
        "README.md::README.md",
        "tests/test_other.py::test_other",
    }
    assert discovered["pkg/helpers.py::pkg.helpers.double"].entry_symbol == "pkg.helpers.double"

    def plan_for(head):
        return repo.plan(
            base,
            head,
            [asv_target("bench.time_x", "pkg.other.other")],
            discover_runners=["pytest"],
        )

    # A change in the doctests' import closure: both module doctests, and
    # README (always); not the unrelated test or benchmark.
    head = repo.commit({"pkg/core.py": "def times(a, b):\n    return b * a\n"})
    plan = plan_for(head)
    assert selected(plan) == {
        "pkg/helpers.py::pkg.helpers",
        "pkg/helpers.py::pkg.helpers.double",
        "README.md::README.md",
    }
    # A docstring-only change: the doctest whose docstring it is.
    head2 = repo.commit(
        {
            "pkg/core.py": "def times(a, b):\n    return a * b\n",
            "pkg/helpers.py": helpers.replace("double(3)\n    6", "double(4)\n    8"),
        }
    )
    plan2 = repo.plan(head, head2, [], discover_runners=["pytest"])
    assert "pkg/helpers.py::pkg.helpers.double" in selected(plan2)
    assert "tests/test_other.py::test_other" not in selected(plan2)


def test_test_functions_imported_into_a_test_module_are_collected(repo):
    """fastapi's tutorial tests do ``from docs_src.app import client,
    test_read_main``: pytest collects the imported function as a test of the
    importing module, discovery did not, so it could never be selected."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def handler():\n    return 1\n",
            "docs_src/__init__.py": "",
            "docs_src/app.py": (
                "from pkg.core import handler\n\n\n"
                "def test_read_main():\n    assert handler() == 1\n\n\n"
                "class TestApp:\n    def test_app(self):\n        assert handler()\n"
            ),
            "tests/test_tutorial.py": (
                "from docs_src.app import TestApp, test_read_main\n"
                "from external_lib import test_external\n\n\n"
                "def test_local():\n    assert True\n"
            ),
        }
    )
    head = repo.commit({"pkg/core.py": "def handler():\n    return 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    entries = {d.target.runner_id: d.target.entry_symbol for d in plan.decisions}
    assert entries["tests/test_tutorial.py::test_read_main"] == "docs_src.app.test_read_main"
    assert entries["tests/test_tutorial.py::TestApp::test_app"] == "docs_src.app.TestApp.test_app"
    assert selected(plan) == {
        "tests/test_tutorial.py::test_read_main",
        "tests/test_tutorial.py::TestApp::test_app",
    }
    assert unselected(plan) == {"tests/test_tutorial.py::test_local"}
    # A name from outside the source roots is reported, not guessed.
    notes = [n for d in plan.discovery for n in d.notes if n.kind == "imported_test_out_of_scope"]
    assert [n.detail.split(":")[0] for n in notes] == ["tests/test_tutorial.py"]


def test_code_a_decorator_runs_at_import_reaches_the_modules_importers(repo):
    """fastapi: ``@app.get("/")`` builds the route handler while the module is
    imported (``add_api_route`` -> ``get_request_handler``). Decorators,
    defaults and class bodies run at import, so the module depends on what
    they call and its importers are reached; ``runpy.run_module`` imports by
    name like ``import_module``."""
    framework = (
        "def build_handler(f):\n    return {body}\n\n\n"
        "class App:\n"
        "    def __init__(self):\n        self.routes = {{}}\n\n"
        "    def get(self, path):\n"
        "        def deco(f):\n"
        "            self.routes[path] = build_handler(f)\n            return f\n"
        "        return deco\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/framework.py": framework.format(body="f"),
            "docs_src/__init__.py": "",
            "docs_src/app.py": (
                "from pkg.framework import App\n\napp = App()\n\n\n"
                "@app.get('/')\ndef root():\n    return 'ok'\n"
            ),
            "tests/test_app.py": (
                "from docs_src.app import app\n\n\n"
                "def test_routes():\n    assert '/' in app.routes\n"
            ),
            "tests/test_main.py": (
                "import runpy\n\n\n"
                "def test_main():\n    runpy.run_module('docs_src.app', run_name='__main__')\n"
            ),
            "tests/test_other.py": "def test_other():\n    assert True\n",
            "benchmarks/bench_app.py": (
                "from docs_src.app import app\n\n\ndef time_routes():\n    app.routes\n"
            ),
        }
    )
    targets = [
        py_target("t::test_routes", "tests.test_app.test_routes", "tests.test_app"),
        py_target("t::test_main", "tests.test_main.test_main", "tests.test_main"),
        py_target("t::test_other", "tests.test_other.test_other", "tests.test_other"),
        asv_target("bench.time_routes", "benchmarks.bench_app.time_routes", "benchmarks.bench_app"),
    ]
    head = repo.commit({"pkg/framework.py": framework.format(body="(f, 'wrapped')")})
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.framework.build_handler": ("body_changed",)}
    assert selected(plan) == {"t::test_routes", "t::test_main", "bench.time_routes"}
    assert unselected(plan) == {"t::test_other"}


def test_a_literal_constant_runs_nothing_at_import(repo):
    """hatch's releases change only ``__version__ = "1.32.1"``: binding a
    literal runs no code when the module is imported, so only readers of the
    value are selected. A computed value runs at import, and ``__all__``
    decides what ``from m import *`` binds at the importer's import."""
    about = 'VERSION = "{version}"\nNAMES = ["a"]\n__all__ = [{names}]\nSTAMP = {stamp}\n'
    files = {
        "pkg/__init__.py": "from pkg.about import VERSION\n",
        "pkg/about.py": about.format(version="1.0", names='"VERSION"', stamp="1"),
        "tests/test_version.py": (
            "from pkg.about import VERSION\n\n\ndef test_version():\n    assert VERSION\n"
        ),
        "tests/test_other.py": "import pkg\n\n\ndef test_other():\n    assert pkg\n",
        "benchmarks/bench_v.py": "import pkg\n\n\ndef time_import():\n    pkg\n",
    }
    base = repo.commit(files)
    targets = [
        py_target("t::test_version", "tests.test_version.test_version", "tests.test_version"),
        py_target("t::test_other", "tests.test_other.test_other", "tests.test_other"),
        asv_target("bench.time_import", "benchmarks.bench_v.time_import", "benchmarks.bench_v"),
    ]
    # A literal constant: its readers only.
    head = repo.commit({"pkg/about.py": about.format(version="1.1", names='"VERSION"', stamp="1")})
    plan = repo.plan(base, head, targets)
    assert changes(plan) == {"pkg.about.VERSION": ("body_changed",)}
    assert selected(plan) == {"t::test_version"}
    # ``__all__`` and a computed value run at import: every importer.
    for changed in (
        about.format(version="1.1", names='"VERSION", "NAMES"', stamp="1"),
        about.format(version="1.1", names='"VERSION"', stamp="len('ab')"),
    ):
        head2 = repo.commit({"pkg/about.py": changed})
        assert selected(repo.plan(head, head2, targets)) == {
            "t::test_version",
            "t::test_other",
            "bench.time_import",
        }, changed


def test_getfixturevalue_with_a_literal_is_a_fixture_request(repo):
    """pytest-django's autouse ``_django_db_marker`` reaches
    ``_django_db_helper`` through ``request.getfixturevalue(...)``: 49 tests
    executed the helper without being selected."""
    conftest = (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef helper():\n    return {value}\n\n\n"
        "@pytest.fixture\ndef unused():\n    return 0\n\n\n"
        "@pytest.fixture(autouse=True)\ndef marker(request):\n"
        "    if request.node.get_closest_marker('needs_db'):\n"
        "        request.getfixturevalue('helper')\n"
    )
    base = repo.commit(
        {
            "tests/conftest.py": conftest.format(value=1),
            "tests/test_db.py": (
                "import pytest\n\n\n@pytest.mark.needs_db\ndef test_db():\n    assert True\n"
            ),
            "tests/test_plain.py": "def test_plain():\n    assert True\n",
        }
    )
    head = repo.commit({"tests/conftest.py": conftest.format(value=2)})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert changes(plan) == {"tests.conftest.helper": ("body_changed",)}
    # Every test has the autouse fixture, so both are selected; the point is
    # that the helper is a dependency at all (an unused fixture is not).
    assert selected(plan) == {"tests/test_db.py::test_db", "tests/test_plain.py::test_plain"}
    deps = {d.target.runner_id: d.target.lifecycle_dependencies for d in plan.decisions}
    assert "tests.conftest.helper" in deps["tests/test_db.py::test_db"]
    assert "tests.conftest.unused" not in deps["tests/test_db.py::test_db"]


def test_a_mutated_container_is_not_the_literal_it_was_assigned(repo):
    """``REGISTRY = {}`` filled by a decorator elsewhere was read as the
    empty literal, so ``for name in REGISTRY: getattr(mod, name)`` bounded
    to no names at all and a change to a registered function was missed."""
    core = (
        "from pkg import mod\n\n"
        "REGISTRY = {}\n"
        "FIXED = {'a': 1}\n\n\n"
        "def register(name):\n    REGISTRY[name] = 1\n\n\n"
        "def run():\n    for name in REGISTRY:\n        getattr(mod, name)()\n\n\n"
        "def run_fixed():\n    for name in FIXED:\n        getattr(mod, name)()\n"
    )
    test = (
        "from pkg.core import register, run, run_fixed\n\n\n"
        "def test_s():\n    register('evil')\n    run()\n    run_fixed()\n"
    )
    assert _evil_reaches(repo, core, test) == {"t::test_s"}
    # The unmutated literal still bounds ``run_fixed`` to ``mod.a``.
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": _EVIL.format(2),
            "pkg/core.py": core,
            "tests/test_s.py": test,
        }
    )
    head = repo.commit({"pkg/mod.py": "def a():\n    return 9\n\n\ndef evil():\n    return 2\n"})
    plan = repo.plan(base, head, [py_target("t::test_s", "tests.test_s.test_s")])
    assert changes(plan) == {"pkg.mod.a": ("body_changed",)}
    # ``run`` is dynamic (its registry is mutated), so it is selected through
    # the import closure; ``run_fixed`` keeps a resolved edge to ``mod.a``
    # from the literal it iterates, and none to ``mod.evil``.
    assert rules(plan, "t::test_s") == {"dynamic_reference"}
    edges = {(e.source, e.target) for e in plan.head_index.edges}
    assert ("pkg.core.run_fixed", "pkg.mod.a") in edges
    assert ("pkg.core.run_fixed", "pkg.mod.evil") not in edges


def test_dict_literal_values_bound_in_an_items_loop(repo):
    """``for name, module in TABLE.items(): import_module(module)`` over a
    dict display reaches only that display's values, not every module."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/cli.py": (
                "import importlib\n\n"
                "_COMMANDS = {'build': 'pkg.build', 'serve': 'pkg.serve'}\n\n\n"
                "def load_all():\n"
                "    for command, module_name in _COMMANDS.items():\n"
                "        importlib.import_module(module_name)\n"
            ),
            "pkg/build.py": "STAMP = len('build')\n",
            "pkg/serve.py": "STAMP = len('serve')\n",
            "pkg/other.py": "STAMP = len('other')\n",
            "tests/test_cli.py": (
                "from pkg.cli import load_all\n\n\ndef test_cli():\n    assert load_all() is None\n"
            ),
            "benchmarks/bench_cli.py": (
                "from pkg.other import STAMP\n\n\n"
                "class Other:\n    def time_other(self):\n        return STAMP\n"
            ),
        }
    )
    targets = [
        py_target("t::test_cli", "tests.test_cli.test_cli"),
        asv_target("bench_cli.Other.time_other", "benchmarks.bench_cli.Other.time_other"),
    ]
    other = repo.commit({"pkg/other.py": "STAMP = len('other!')\n"})
    plan = repo.plan(base, other, targets)
    # pkg.other is not one of the table's values, so the dynamic import does
    # not reach it: only the benchmark importing it directly is selected.
    assert selected(plan) == {"bench_cli.Other.time_other"}
    assert not [u for u in plan.unresolved if u.kind == "dynamic"]

    serve = repo.commit({"pkg/serve.py": "STAMP = len('serve!')\n"})
    plan = repo.plan(other, serve, targets)
    assert selected(plan) == {"t::test_cli"}
    r = reason(plan, "t::test_cli", "dynamic_reference")
    assert path_ids(r)[-2:] == ["pkg.cli.load_all", "pkg.serve"]


def test_a_mutated_table_keeps_its_items_loop_dynamic(repo):
    """The values are bound only while the table is the display it was
    assigned: a table another module fills stays unbounded."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/cli.py": (
                "import importlib\n\n"
                "_COMMANDS = {'build': 'pkg.build'}\n\n\n"
                "def load_all():\n"
                "    for command, module_name in _COMMANDS.items():\n"
                "        importlib.import_module(module_name)\n"
            ),
            "pkg/plugins.py": (
                "from pkg.cli import _COMMANDS\n\n\n"
                "def register():\n    _COMMANDS['other'] = 'pkg.other'\n"
            ),
            "pkg/build.py": "STAMP = len('build')\n",
            "pkg/other.py": "STAMP = len('other')\n",
            "tests/test_cli.py": (
                "from pkg.cli import load_all\n\n\ndef test_cli():\n    assert load_all() is None\n"
            ),
            "benchmarks/bench_cli.py": (
                "from pkg.cli import load_all\n\n\n"
                "class Load:\n    def time_load(self):\n        return load_all()\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "STAMP = len('other!')\n"})
    targets = [
        py_target("t::test_cli", "tests.test_cli.test_cli"),
        asv_target("bench_cli.Load.time_load", "benchmarks.bench_cli.Load.time_load"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_cli", "bench_cli.Load.time_load"}
    assert [u for u in plan.unresolved if u.kind == "dynamic"]


def test_a_module_is_read_in_the_encoding_it_declares(repo):
    """A coding cookie (PEP 263) is how Python reads a file that is not
    UTF-8; such a module is analysed, not an analysis error that selects
    everything (pip's latin-1 test package)."""
    latin = (
        "# -*- coding: latin-1 -*-\nSUFFIX = 'ú'\n\n\ndef label(name):\n    return name + SUFFIX\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/latin.py": latin.encode("latin-1"),
            "pkg/other.py": "def helper():\n    return 1\n",
            "tests/test_latin.py": (
                "from pkg.latin import label\n\n\ndef test_latin():\n    assert label('a')\n"
            ),
            "benchmarks/bench_other.py": (
                "from pkg.other import helper\n\n\n"
                "class Other:\n    def time_other(self):\n        return helper()\n"
            ),
        }
    )
    head = repo.commit(
        {"pkg/latin.py": latin.replace("name + SUFFIX", "SUFFIX + name").encode("latin-1")}
    )
    targets = [
        py_target("t::test_latin", "tests.test_latin.test_latin"),
        asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
    ]
    plan = repo.plan(base, head, targets)
    assert not plan.degraded and plan.errors == []
    assert changes(plan) == {"pkg.latin.label": ("body_changed",)}
    assert selected(plan) == {"t::test_latin"}


def test_a_runtime_named_import_says_so_in_its_reason(repo):
    """The two dynamic fallbacks are different rules and say which fired: a
    name looked up on an object reaches what its module imports, while an
    import of a runtime name reaches any module in scope."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/loader.py": (
                "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
            ),
            "pkg/probe.py": (
                "from pkg import model\n\n\ndef probe(name):\n    return getattr(model, name)\n"
            ),
            "pkg/model.py": "def value():\n    return 1\n",
            "pkg/far.py": "def far():\n    return 2\n",
            "tests/test_load.py": (
                "import os\n\nfrom pkg.loader import load\n\n\n"
                "def test_load():\n    assert load(os.environ['MODULE'])\n"
            ),
            "benchmarks/bench_probe.py": (
                "from pkg.probe import probe\n\n\n"
                "class Probe:\n    def time_probe(self):\n        return probe('value')\n"
            ),
        }
    )
    head = repo.commit({"pkg/far.py": "def far():\n    return 3\n"})
    targets = [
        py_target("t::test_load", "tests.test_load.test_load"),
        asv_target("bench_probe.Probe.time_probe", "benchmarks.bench_probe.Probe.time_probe"),
    ]
    plan = repo.plan(base, head, targets)
    # pkg.far is not imported by pkg.probe's module, so the getattr seed does
    # not fire; the runtime-named import may name any module, so it does.
    assert selected(plan) == {"t::test_load"}
    detail = reason(plan, "t::test_load", "dynamic_reference").detail
    # The seed sits on the caller that could not name the module, not on the
    # helper: another caller passing a literal is bounded to what it named.
    assert detail.startswith(
        "tests.test_load.test_load imports a module named at runtime (base, head)"
    )
    assert "any module in scope may be behind it" in detail


def test_a_class_a_plugin_may_collect_is_reported(repo):
    """pytest's own rules skip a class that does not match ``python_classes``,
    but a plugin may collect it (SQLAlchemy's testing plugin collects
    ``<Name>Test``). Discovery reports it instead of guessing either way."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class TestAdd:\n    def test_two(self):\n        assert add(1, 1) == 2\n\n\n"
                "class AddRoundTripTest:\n"
                "    def test_round(self):\n        assert add(2, 2) == 4\n"
            ),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    pytest_discovery = next(d for d in plan.discovery if d.runner == "pytest")
    assert {t.runner_id for t in pytest_discovery.targets} == {
        "tests/test_ops.py::TestAdd::test_two"
    }
    notes = [n for n in pytest_discovery.notes if n.kind == "uncollected_test_class"]
    assert [n.detail.split(":", 2)[0] for n in notes] == ["tests/test_ops.py"]
    assert notes[0].detail.startswith("tests/test_ops.py::AddRoundTripTest: defines test methods")
    assert "does not match python_classes" in notes[0].detail
    assert selected(plan) == {"tests/test_ops.py::TestAdd::test_two", "bench_ops.Add.time_add"}


def test_star_imported_tests_are_targets(repo):
    """``from tests.test_install import *`` re-runs another module's tests
    under this module's fixtures (poetry's sync command). The imported names
    are targets whose entry is where they are defined; a name this module
    defines itself wins."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_install.py": (
                "from pkg.ops import add\n\n\n"
                "def test_shared():\n    assert add(1, 1) == 2\n\n\n"
                "def test_only_install():\n    assert add(2, 2) == 4\n\n\n"
                "def _helper():\n    return 1\n"
            ),
            "tests/test_sync.py": (
                "from tests.test_install import *  # noqa: F403\n\n\n"
                "def test_only_install():\n    assert True\n"
            ),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    pytest_targets = {
        t.runner_id: t.entry_symbol
        for d in plan.discovery
        if d.runner == "pytest"
        for t in d.targets
    }
    assert pytest_targets == {
        "tests/test_install.py::test_shared": "tests.test_install.test_shared",
        "tests/test_install.py::test_only_install": "tests.test_install.test_only_install",
        # Imported by the star: the entry is where it is defined.
        "tests/test_sync.py::test_shared": "tests.test_install.test_shared",
        # Redefined here, so this module's own definition is the entry.
        "tests/test_sync.py::test_only_install": "tests.test_sync.test_only_install",
    }
    assert selected(plan) == {
        "tests/test_install.py::test_shared",
        "tests/test_install.py::test_only_install",
        "tests/test_sync.py::test_shared",
        "bench_ops.Add.time_add",
    }


def test_a_base_class_in_another_test_module_contributes_its_tests(repo):
    """networkx's ``TestDiGraph(BaseGraphTester)`` inherits its tests from
    another module; the inherited methods are targets whose entry is where
    they are defined, and the base's own bases resolve in the base's module."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": (
                "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
            ),
            "tests/__init__.py": "",
            "tests/base_tester.py": (
                "from pkg.ops import mul\n\n\n"
                "class RootTester:\n    def test_root(self):\n        assert mul(2, 2) == 4\n"
            ),
            "tests/test_graph.py": (
                "from pkg.ops import add\n"
                "from tests.base_tester import RootTester\n\n\n"
                "class BaseGraphTester(RootTester):\n"
                "    def test_shared(self):\n        assert add(1, 1) == 2\n"
            ),
            "tests/test_digraph.py": (
                "from tests.test_graph import BaseGraphTester\n\n\n"
                "class TestDiGraph(BaseGraphTester):\n"
                "    def test_own(self):\n        assert True\n"
            ),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
        }
    )
    head = repo.commit(
        {"pkg/ops.py": "def add(a, b):\n    return b + a\n\n\ndef mul(a, b):\n    return a * b\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    discovery = next(d for d in plan.discovery if d.runner == "pytest")
    assert not [n for n in discovery.notes if n.kind == "unknown_base_class"]
    targets = {t.runner_id: t.entry_symbol for t in discovery.targets}
    assert targets["tests/test_digraph.py::TestDiGraph::test_shared"] == (
        "tests.test_graph.BaseGraphTester.test_shared"
    )
    assert targets["tests/test_digraph.py::TestDiGraph::test_root"] == (
        "tests.base_tester.RootTester.test_root"
    )
    # Only what reaches the changed ``add``: the inherited ``test_root`` calls
    # ``mul``, which did not change.
    # ``BaseGraphTester`` does not match python_classes, so pytest collects it
    # only through the subclass -- and it is a base, so it is not reported as
    # a class something else may collect.
    assert "tests/test_graph.py::BaseGraphTester::test_shared" not in targets
    assert not [n for n in discovery.notes if n.kind == "uncollected_test_class"]
    assert selected(plan) == {
        "tests/test_digraph.py::TestDiGraph::test_shared",
        "bench_ops.Add.time_add",
    }


def test_a_testcase_subclass_is_collected_whatever_it_is_called(repo):
    """pytest's unittest plugin collects a ``TestCase`` subclass whatever its
    name, and the base that brings ``TestCase`` in may be several classes and
    modules away (django-rest-framework's ``XffSpoofingTests``)."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/__init__.py": "",
            "tests/bases.py": (
                "import unittest\n\n\n"
                "class ThrottleTestBase(unittest.TestCase):\n"
                "    def setUp(self):\n        self.n = 1\n"
            ),
            "tests/test_throttling.py": (
                "from pkg.ops import add\n"
                "from tests.bases import ThrottleTestBase\n\n\n"
                "class XffSpoofingTests(ThrottleTestBase):\n"
                "    def test_spoofing(self):\n        assert add(self.n, 1) == 2\n"
            ),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    discovery = next(d for d in plan.discovery if d.runner == "pytest")
    assert not [n for n in discovery.notes if n.kind == "uncollected_test_class"]
    assert "tests/test_throttling.py::XffSpoofingTests::test_spoofing" in {
        t.runner_id for t in discovery.targets
    }
    assert selected(plan) == {
        "tests/test_throttling.py::XffSpoofingTests::test_spoofing",
        "bench_ops.Add.time_add",
    }


def test_a_python_files_pattern_with_a_directory_matches_like_pytest(repo):
    """pytest matches ``python_files`` against absolute paths, so a pattern
    with a separator is effectively prefixed with ``*/``: scrapy's
    ``test_*/__init__.py`` collects ``tests/test_settings/__init__.py``."""
    base = repo.commit(
        {
            "pyproject.toml": (
                '[tool.pytest.ini_options]\npython_files = ["test_*.py", "test_*/__init__.py"]\n'
            ),
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_settings/__init__.py": (
                "from pkg.ops import add\n\n\n"
                "class TestBaseSettings:\n"
                "    def test_copy(self):\n        assert add(1, 1) == 2\n"
            ),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {
        "tests/test_settings/__init__.py::TestBaseSettings::test_copy",
        "bench_ops.Add.time_add",
    }


def test_a_conftest_that_collects_files_makes_the_plan_incomplete(repo):
    """``pytest_collect_file`` makes tests out of files by a plugin's own
    rules (scrapy's docs are Sybil doctests), so the target list is not the
    suite and the plan says so."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "docs/conftest.py": (
                "from sybil import Sybil\n\n\npytest_collect_file = Sybil(patterns=['*.rst'])\n"
            ),
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
            ),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    notes = [n for n in plan.incomplete_discovery if n.kind == "plugin_collects_files"]
    assert len(notes) == 1
    assert notes[0].detail.startswith("docs/conftest.py: binds pytest_collect_file")
    # The targets it does know are still planned normally.
    assert selected(plan) == {"tests/test_ops.py::test_add", "bench_ops.Add.time_add"}


def test_a_test_file_that_cannot_be_named_is_reported_and_nameable(repo):
    """pytest imports a test file by its basename, so it collects one under a
    directory whose name is not a Python identifier (pytest-asyncio's
    ``docs/how-to-guides``). Such a file cannot be named from a plain source
    root: the plan says so, and a ``DIR=PREFIX`` root fixes it."""
    tree = {
        "pyproject.toml": (
            "[tool.pytest.ini_options]\n"
            'python_files = ["test_*.py", "*_example.py"]\n'
            'testpaths = ["docs", "tests"]\n'
        ),
        "pkg/__init__.py": "",
        "pkg/ops.py": "def add(a, b):\n    return a + b\n",
        "docs/how-to-guides/loop_example.py": (
            "from pkg.ops import add\n\n\ndef test_example():\n    assert add(1, 1) == 2\n"
        ),
        "tests/test_ops.py": (
            "from pkg.ops import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
        ),
        "benchmarks/bench_ops.py": (
            "from pkg.ops import add\n\n\n"
            "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
        ),
    }
    base = repo.commit(tree)
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})

    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    notes = [n for n in plan.incomplete_discovery if n.kind == "unparsed_file"]
    assert len(notes) == 1
    assert "cannot be named from any source root" in notes[0].detail
    assert "DIR=PREFIX" in notes[0].detail
    assert selected(plan) == {"tests/test_ops.py::test_add", "bench_ops.Add.time_add"}

    named = repo.plan(
        base,
        head,
        [],
        source_roots=["docs/how-to-guides=docs_howto", "."],
        discover_runners=["pytest", "asv"],
    )
    assert not named.incomplete_discovery
    assert selected(named) == {
        "docs/how-to-guides/loop_example.py::test_example",
        "tests/test_ops.py::test_add",
        "bench_ops.Add.time_add",
    }


def test_asv_benchmarks_inherited_from_a_base_class(repo):
    """ASV reads a benchmark class's attributes, inherited ones included: a
    benchmark defined on a base class in another module is a target of the
    subclass, with the base's method as its entry symbol."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": (
                "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
            ),
            "pkg/benchbase.py": (
                "from pkg.ops import mul\n\n\n"
                "class MulBase:\n"
                "    def setup(self):\n        self.n = 2\n\n"
                "    def time_mul(self):\n        return mul(self.n, self.n)\n"
            ),
            "benchmarks/__init__.py": "",
            "benchmarks/bench_ops.py": (
                "from pkg.benchbase import MulBase\n"
                "from pkg.ops import add\n\n\n"
                "class Ops(MulBase):\n"
                "    def time_add(self):\n        return add(self.n, self.n)\n"
            ),
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
            ),
        }
    )
    plan = repo.plan(base, base, [], discover_runners=["pytest", "asv"])
    asv = next(d for d in plan.discovery if d.runner == "asv")
    assert {t.runner_id: t.entry_symbol for t in asv.targets} == {
        "bench_ops.Ops.time_add": "benchmarks.bench_ops.Ops.time_add",
        # Inherited: named after the subclass, entered where it is defined.
        "bench_ops.Ops.time_mul": "pkg.benchbase.MulBase.time_mul",
    }
    inherited = next(t for t in asv.targets if t.runner_id == "bench_ops.Ops.time_mul")
    assert "pkg.benchbase.MulBase.setup" in inherited.lifecycle_dependencies
    assert not [n for n in asv.notes if n.kind == "unknown_base_class"]

    # Only the benchmark that reaches the changed function is selected.
    head = repo.commit(
        {"pkg/ops.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return b * a\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"bench_ops.Ops.time_mul"}


def test_an_asv_base_class_outside_the_source_roots_is_reported(repo):
    """A base class diffcone cannot see may contribute benchmarks it cannot
    list, so the plan says so rather than looking complete."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "benchmarks/bench_ops.py": (
                "from external_suite import BenchBase\n"
                "from pkg.ops import add\n\n\n"
                "class Ops(BenchBase):\n"
                "    def time_add(self):\n        return add(1, 1)\n"
            ),
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    notes = [n for n in plan.incomplete_discovery if n.kind == "unknown_base_class"]
    assert len(notes) == 1
    assert notes[0].detail.startswith("bench_ops.Ops: base class 'BenchBase'")
    assert selected(plan) == {"bench_ops.Ops.time_add", "tests/test_ops.py::test_add"}


def test_an_asv_config_beside_the_benchmarks_names_them_as_asv_does(repo):
    """ASV resolves ``benchmark_dir`` against the directory holding
    ``asv.conf.json``, which is usually not the repository root (numpy and
    networkx keep both under ``benchmarks/``). The benchmark id is relative
    to the real benchmark directory, or ``--bench`` would never match it."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "benchmarks/asv.conf.json": (
                '{\n  // the suite lives beside this file\n  "benchmark_dir": "benchmarks"\n}\n'
            ),
            "benchmarks/benchmarks/__init__.py": "",
            "benchmarks/benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Ops:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    asv = next(d for d in plan.discovery if d.runner == "asv")
    assert asv.config["source"] == "benchmarks/asv.conf.json"
    assert asv.config["benchmark_dir"] == "benchmarks/benchmarks"
    assert {t.runner_id: t.entry_symbol for t in asv.targets} == {
        "bench_ops.Ops.time_add": "benchmarks.benchmarks.bench_ops.Ops.time_add"
    }
    assert selected(plan) == {"bench_ops.Ops.time_add", "tests/test_ops.py::test_add"}


def test_an_imported_test_class_brings_its_inherited_tests(repo):
    """A test class imported into another module is collected there with
    everything it inherits: urllib3's ``test_pyopenssl.py`` imports
    ``TestHTTPS_TLSv1``, whose tests are nearly all defined on its bases."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": (
                "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
            ),
            "tests/__init__.py": "",
            "tests/test_base.py": (
                "from pkg.ops import add\n\n\n"
                "class TestAddBase:\n"
                "    def test_inherited(self):\n        assert add(1, 1) == 2\n"
            ),
            "tests/test_https.py": (
                "from pkg.ops import mul\n"
                "from tests.test_base import TestAddBase\n\n\n"
                "class TestHTTPS(TestAddBase):\n"
                "    def test_own(self):\n        assert mul(2, 2) == 4\n"
            ),
            "tests/test_openssl.py": ("from tests.test_https import TestHTTPS  # noqa: F401\n"),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import mul\n\n\n"
                "class Mul:\n    def time_mul(self):\n        return mul(2, 2)\n"
            ),
        }
    )
    head = repo.commit(
        {"pkg/ops.py": "def add(a, b):\n    return b + a\n\n\ndef mul(a, b):\n    return a * b\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    discovery = next(d for d in plan.discovery if d.runner == "pytest")
    imported = {
        t.runner_id: t.entry_symbol
        for t in discovery.targets
        if t.runner_id.startswith("tests/test_openssl.py")
    }
    assert imported == {
        "tests/test_openssl.py::TestHTTPS::test_own": "tests.test_https.TestHTTPS.test_own",
        # Inherited by the imported class, from a third module.
        "tests/test_openssl.py::TestHTTPS::test_inherited": (
            "tests.test_base.TestAddBase.test_inherited"
        ),
    }
    # Only what reaches the changed ``add``: the inherited test, everywhere it
    # is collected.
    assert selected(plan) == {
        "tests/test_base.py::TestAddBase::test_inherited",
        # ``TestAddBase`` is imported into test_https.py, so pytest collects it
        # there as well as through the subclass.
        "tests/test_https.py::TestAddBase::test_inherited",
        "tests/test_https.py::TestHTTPS::test_inherited",
        "tests/test_openssl.py::TestHTTPS::test_inherited",
    }


def test_a_declared_dependency_selects_and_says_who_declared_it(repo):
    """A registry filled at import time is a real dependency no static rule
    can find. ``diffcone.toml`` states it; the plan follows it and explains
    the selection with the project's own words, not as an ordinary
    dependency."""
    base = repo.commit(
        {
            "diffcone.toml": (
                "[[edges]]\n"
                'from = "pkg.registry.dispatch"\n'
                'to = "pkg.handlers.json_handler"\n'
                'why = "handlers register themselves through entry points"\n'
            ),
            "pkg/__init__.py": "",
            "pkg/registry.py": (
                "HANDLERS = {}\n\n\ndef dispatch(name, payload):\n"
                "    return HANDLERS[name](payload)\n"
            ),
            "pkg/handlers.py": "def json_handler(payload):\n    return payload\n",
            "pkg/other.py": "def helper():\n    return 1\n",
            "tests/test_dispatch.py": (
                "from pkg.registry import dispatch\n\n\n"
                "def test_dispatch():\n    assert dispatch('json', {}) == {}\n"
            ),
            "benchmarks/bench_other.py": (
                "from pkg.other import helper\n\n\n"
                "class Other:\n    def time_other(self):\n        return helper()\n"
            ),
        }
    )
    head = repo.commit(
        {"pkg/handlers.py": "def json_handler(payload):\n    return dict(payload)\n"}
    )
    targets = [
        py_target("t::dispatch", "tests.test_dispatch.test_dispatch"),
        asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
    ]
    plan = repo.plan(base, head, targets)
    assert not plan.degraded and plan.errors == []
    assert [(d.source, d.target) for d in plan.declarations] == [
        ("pkg.registry.dispatch", "pkg.handlers.json_handler")
    ]
    assert selected(plan) == {"t::dispatch"}
    r = reason(plan, "t::dispatch", "declared_dependency")
    assert path_ids(r) == [
        "target:pytest:t::dispatch",
        "tests.test_dispatch.test_dispatch",
        "pkg.registry.dispatch",
        "pkg.handlers.json_handler",
    ]
    assert r.path[-1].detail == (
        "declared in diffcone.toml: handlers register themselves through entry points"
    )


def test_a_declaration_that_names_nothing_is_an_analysis_error(repo):
    """A typo in a declaration would silently declare nothing, so it fails
    the plan instead."""
    base = repo.commit(
        {
            "diffcone.toml": (
                '[[edges]]\nfrom = "pkg.registry.dispatch"\nto = "pkg.handlers.jsno_handler"\n'
            ),
            "pkg/__init__.py": "",
            "pkg/registry.py": "def dispatch(name):\n    return name\n",
            "pkg/handlers.py": "def json_handler(payload):\n    return payload\n",
            "tests/test_dispatch.py": (
                "from pkg.registry import dispatch\n\n\n"
                "def test_dispatch():\n    assert dispatch('json') == 'json'\n"
            ),
            "benchmarks/bench_d.py": (
                "from pkg.registry import dispatch\n\n\n"
                "class D:\n    def time_d(self):\n        return dispatch('json')\n"
            ),
        }
    )
    head = repo.commit(
        {"pkg/handlers.py": "def json_handler(payload):\n    return dict(payload)\n"}
    )
    targets = [
        py_target("t::dispatch", "tests.test_dispatch.test_dispatch"),
        asv_target("bench_d.D.time_d", "benchmarks.bench_d.D.time_d"),
    ]
    plan = repo.plan(base, head, targets)
    assert plan.degraded
    assert [e.path for e in plan.errors] == ["diffcone.toml"]
    assert "'pkg.handlers.jsno_handler'" in plan.errors[0].message
    # Degraded means everything is selected, not a quietly empty plan.
    assert selected(plan) == {"t::dispatch", "bench_d.D.time_d"}


def test_a_declaration_may_name_a_module_or_a_class(repo):
    """``to = "pkg.handlers"`` means everything in it: a change to any member
    is what such a declaration is about, and the container node alone would
    never see it."""
    base = repo.commit(
        {
            "diffcone.toml": (
                '[[edges]]\nfrom = "pkg.registry.dispatch"\nto = "pkg.handlers"\n'
                'why = "every handler registers itself"\n'
            ),
            "pkg/__init__.py": "",
            "pkg/registry.py": "def dispatch(name):\n    return name\n",
            "pkg/handlers.py": (
                "def json_handler(payload):\n    return payload\n\n\n"
                "class Xml:\n    def handle(self, payload):\n        return payload\n"
            ),
            "pkg/other.py": "def helper():\n    return 1\n",
            "tests/test_dispatch.py": (
                "from pkg.registry import dispatch\n\n\n"
                "def test_dispatch():\n    assert dispatch('json') == 'json'\n"
            ),
            "benchmarks/bench_other.py": (
                "from pkg.other import helper\n\n\n"
                "class Other:\n    def time_other(self):\n        return helper()\n"
            ),
        }
    )
    targets = [
        py_target("t::dispatch", "tests.test_dispatch.test_dispatch"),
        asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
    ]
    # A function in the declared module, and a method of a class in it.
    for change in (
        "def json_handler(payload):\n    return dict(payload)\n\n\n"
        "class Xml:\n    def handle(self, payload):\n        return payload\n",
        "def json_handler(payload):\n    return payload\n\n\n"
        "class Xml:\n    def handle(self, payload):\n        return str(payload)\n",
    ):
        head = repo.commit({"pkg/handlers.py": change})
        plan = repo.plan(base, head, targets)
        assert not plan.degraded
        assert selected(plan) == {"t::dispatch"}
        assert (
            reason(plan, "t::dispatch", "declared_dependency")
            .path[-1]
            .detail.startswith("declared in diffcone.toml")
        )


def test_a_declaration_deleted_by_the_head_commit_still_counts(repo):
    """Declarations are read from both revisions, as every other edge is: a
    commit that removes the file while changing what it pointed at must still
    select what depended on it."""
    base = repo.commit(
        {
            "diffcone.toml": (
                '[[edges]]\nfrom = "pkg.registry.dispatch"\nto = "pkg.handlers.json_handler"\n'
            ),
            "pkg/__init__.py": "",
            "pkg/registry.py": "def dispatch(name):\n    return name\n",
            "pkg/handlers.py": "def json_handler(payload):\n    return payload\n",
            "tests/test_dispatch.py": (
                "from pkg.registry import dispatch\n\n\n"
                "def test_dispatch():\n    assert dispatch('json') == 'json'\n"
            ),
            "benchmarks/bench_d.py": (
                "from pkg.registry import dispatch\n\n\n"
                "class D:\n    def time_d(self):\n        return dispatch('json')\n"
            ),
        }
    )
    head = repo.commit(
        {
            "diffcone.toml": None,
            "pkg/handlers.py": "def json_handler(payload):\n    return dict(payload)\n",
        }
    )
    plan = repo.plan(
        base,
        head,
        [
            py_target("t::dispatch", "tests.test_dispatch.test_dispatch"),
            asv_target("bench_d.D.time_d", "benchmarks.bench_d.D.time_d"),
        ],
    )
    assert not plan.degraded
    assert selected(plan) == {"t::dispatch", "bench_d.D.time_d"}


def test_a_declaration_file_that_declares_nothing_is_an_error(repo):
    """A singular ``[[edge]]`` would otherwise parse to no declarations at
    all, quietly."""
    base = repo.commit(
        {
            "diffcone.toml": '[[edge]]\nfrom = "pkg.registry.dispatch"\nto = "pkg.handlers.j"\n',
            "pkg/__init__.py": "",
            "pkg/registry.py": "def dispatch(name):\n    return name\n",
            "tests/test_dispatch.py": (
                "from pkg.registry import dispatch\n\n\n"
                "def test_dispatch():\n    assert dispatch('json') == 'json'\n"
            ),
            "benchmarks/bench_d.py": (
                "from pkg.registry import dispatch\n\n\n"
                "class D:\n    def time_d(self):\n        return dispatch('json')\n"
            ),
        }
    )
    head = repo.commit({"pkg/registry.py": "def dispatch(name):\n    return str(name)\n"})
    plan = repo.plan(
        base,
        head,
        [
            py_target("t::dispatch", "tests.test_dispatch.test_dispatch"),
            asv_target("bench_d.D.time_d", "benchmarks.bench_d.D.time_d"),
        ],
    )
    assert plan.degraded
    assert "unknown top-level key(s) edge" in plan.errors[0].message


def test_a_declaration_is_read_from_the_index_and_the_working_tree(repo):
    """A declaration written but not yet committed applies to the snapshot
    that has it: staged for ``INDEX``, on disk for ``WORKTREE``."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/registry.py": "def dispatch(name):\n    return name\n",
            "pkg/handlers.py": "def json_handler(payload):\n    return payload\n",
            "tests/test_dispatch.py": (
                "from pkg.registry import dispatch\n\n\n"
                "def test_dispatch():\n    assert dispatch('json') == 'json'\n"
            ),
            "benchmarks/bench_other.py": (
                "from pkg.handlers import json_handler\n\n\n"
                "class Other:\n    def time_other(self):\n        return json_handler({})\n"
            ),
        }
    )
    targets = [
        py_target("t::dispatch", "tests.test_dispatch.test_dispatch"),
        asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
    ]
    # Without the declaration the test does not depend on the handler.
    (repo.path / "pkg" / "handlers.py").write_text(
        "def json_handler(payload):\n    return dict(payload)\n", "utf-8"
    )
    plan = repo.plan(base, "WORKTREE", targets)
    assert selected(plan) == {"bench_other.Other.time_other"}

    declaration = '[[edges]]\nfrom = "pkg.registry.dispatch"\nto = "pkg.handlers.json_handler"\n'
    (repo.path / "diffcone.toml").write_text(declaration, "utf-8")
    plan = repo.plan(base, "WORKTREE", targets)
    assert not plan.degraded
    assert selected(plan) == {"t::dispatch", "bench_other.Other.time_other"}

    repo.git("add", "-A")
    plan = repo.plan(base, "INDEX", targets)
    assert not plan.degraded
    assert [(d.source, d.target) for d in plan.declarations] == [
        ("pkg.registry.dispatch", "pkg.handlers.json_handler")
    ]
    assert selected(plan) == {"t::dispatch", "bench_other.Other.time_other"}


def test_getattr_on_an_object_a_caller_supplied_reaches_anything(repo):
    """``def invoke(obj, name): getattr(obj, name)()`` reads an object the
    helper's own module never names, so bounding it by that module's imports
    misses the caller's object entirely."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/helper.py": "def invoke(obj, name):\n    return getattr(obj, name)()\n",
            "pkg/provider.py": "class Provider:\n    def action(self):\n        return 1\n",
            "pkg/other.py": "def helper():\n    return 1\n",
            "tests/test_x.py": (
                "import os\n\nfrom pkg.helper import invoke\n"
                "from pkg.provider import Provider\n\n\n"
                "def test_x():\n    assert invoke(Provider(), os.environ['NAME']) == 1\n"
            ),
            "benchmarks/bench_other.py": (
                "from pkg.other import helper\n\n\n"
                "class Other:\n    def time_other(self):\n        return helper()\n"
            ),
        }
    )
    head = repo.commit(
        {"pkg/provider.py": "class Provider:\n    def action(self):\n        return 2\n"}
    )
    targets = [
        py_target("t::x", "tests.test_x.test_x"),
        asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
    ]
    plan = repo.plan(base, head, targets)
    # The benchmark never reaches the helper, so this is not a select-all.
    assert selected(plan) == {"t::x"}
    # The call sites say what ``obj`` is, so the read is bounded to that
    # class's members rather than to every module.
    r = reason(plan, "t::x")
    assert path_ids(r) == [
        "target:pytest:t::x",
        "tests.test_x.test_x",
        "pkg.helper.invoke",
        "pkg.provider.Provider.action",
    ]
    assert r.path[-1].detail == "attribute read dynamically"


def test_getattr_on_the_module_s_own_import_stays_bounded(repo):
    """The bound still holds where it is sound: an object the seeding module
    itself names holds attributes from its own import closure."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/reader.py": (
                "import os\n\nfrom pkg import provider\n\n\n"
                "def read():\n    return getattr(provider, os.environ['NAME'])()\n"
            ),
            "pkg/provider.py": "def action():\n    return 1\n",
            "pkg/far.py": "def far():\n    return 2\n",
            "tests/test_r.py": (
                "from pkg.reader import read\n\n\ndef test_r():\n    assert read() == 1\n"
            ),
            "benchmarks/bench_far.py": (
                "from pkg.far import far\n\n\n"
                "class Far:\n    def time_far(self):\n        return far()\n"
            ),
        }
    )
    # pkg.far is outside pkg.reader's import closure, so the seed does not fire.
    head = repo.commit({"pkg/far.py": "def far():\n    return 3\n"})
    targets = [
        py_target("t::r", "tests.test_r.test_r"),
        asv_target("bench_far.Far.time_far", "benchmarks.bench_far.Far.time_far"),
    ]
    assert selected(repo.plan(base, head, targets)) == {"bench_far.Far.time_far"}


def test_a_target_the_base_did_not_have_is_selected(repo):
    """A new test is selected whatever its entry symbol did. Importing an
    existing test under a new name adds a target while touching no symbol the
    graph would carry impact along, so discovery runs at the base too."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/__init__.py": "",
            "tests/support.py": (
                "from pkg.ops import add\n\n\ndef test_shared():\n    assert add(1, 1) == 2\n"
            ),
            "tests/test_a.py": "from tests.support import test_shared  # noqa: F401\n",
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
        }
    )
    head = repo.commit(
        {
            "tests/test_a.py": (
                "from tests.support import test_shared  # noqa: F401\n"
                "from tests.support import test_shared as test_new  # noqa: F401\n"
            ),
        }
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_a.py::test_new"}
    r = reason(plan, "tests/test_a.py::test_new", "new_target")
    assert r.detail.startswith("tests/test_a.py::test_new is not in the base snapshot")
    # A benchmark that existed before and changed in no way is left alone.
    assert "bench_ops.Add.time_add" not in selected(plan)


def test_a_new_benchmark_is_selected_too(repo):
    """The rule is runner-independent: an ASV benchmark the base did not have
    is selected even though nothing it depends on changed."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n    def time_add(self):\n        return add(1, 1)\n"
            ),
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
            ),
        }
    )
    head = repo.commit(
        {
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class Add:\n"
                "    def time_add(self):\n        return add(1, 1)\n\n"
                "    def time_add_twice(self):\n        return add(1, 1) + add(2, 2)\n"
            ),
        }
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "bench_ops.Add.time_add_twice" in selected(plan)
    assert reason(plan, "bench_ops.Add.time_add_twice", "new_target")


def test_an_object_a_factory_makes_is_bound_by_what_it_returns(repo):
    """The object never appears at a call site as a construction: the test
    holds what ``make()`` gave it. Typing the factory's return is what binds
    ``Provider`` to its members, so a change to one of them is reached.

    This case used to be the project's accepted exception to the governing
    rule; closing it is the reason the return type is computed at all."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/helper.py": "def invoke(obj, name):\n    return getattr(obj, name)()\n",
            "pkg/provider.py": (
                "class Provider:\n    def action(self):\n        return 1\n\n\n"
                "def make():\n    return Provider()\n"
            ),
            "pkg/other.py": "def helper():\n    return 1\n",
            "tests/test_factory.py": (
                "import os\n\nfrom pkg.helper import invoke\nfrom pkg.provider import make\n\n\n"
                "def test_factory():\n"
                "    obj = make()\n    assert invoke(obj, os.environ['NAME']) == 1\n"
            ),
            "benchmarks/bench_other.py": (
                "from pkg.other import helper\n\n\n"
                "class Other:\n    def time_other(self):\n        return helper()\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/provider.py": (
                "class Provider:\n    def action(self):\n        return 2\n\n\n"
                "def make():\n    return Provider()\n"
            )
        }
    )
    targets = [
        py_target("t::factory", "tests.test_factory.test_factory"),
        asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
    ]
    # The benchmark reaches neither the factory nor the class: not a select-all.
    assert selected(repo.plan(base, head, targets)) == {"t::factory"}


def test_a_factory_that_picks_between_classes_binds_both(repo):
    """Every return yields a class, so the object is one of them and both are
    bound. (A return that yields something else still says nothing: binding a
    class that may never reach the caller would be a guess.)"""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/helper.py": "def invoke(obj, name):\n    return getattr(obj, name)()\n",
            "pkg/provider.py": (
                "class Provider:\n    def action(self):\n        return 1\n\n\n"
                "class Other:\n    def action(self):\n        return 2\n\n\n"
                "def make(flag):\n"
                "    if flag:\n        return Provider()\n    return Other()\n"
            ),
            "tests/test_factory.py": (
                "import os\n\nfrom pkg.helper import invoke\nfrom pkg.provider import make\n\n\n"
                "def test_factory():\n"
                "    obj = make(True)\n    assert invoke(obj, os.environ['NAME']) == 1\n"
            ),
            "benchmarks/bench_p.py": (
                "from pkg.provider import make\n\n\n"
                "class P:\n    def time_make(self):\n        return make(True)\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/provider.py": (
                "class Provider:\n    def action(self):\n        return 3\n\n\n"
                "class Other:\n    def action(self):\n        return 2\n\n\n"
                "def make(flag):\n"
                "    if flag:\n        return Provider()\n    return Other()\n"
            )
        }
    )
    targets = [
        py_target("t::factory", "tests.test_factory.test_factory"),
        asv_target("bench_p.P.time_make", "benchmarks.bench_p.P.time_make"),
    ]
    # Both reach ``make``, whose body changed with the class it returns.
    assert selected(repo.plan(base, head, targets)) == {"t::factory", "bench_p.P.time_make"}


def test_the_builtin_import_names_a_module_like_import_module(repo):
    """``__import__(name)`` names a module exactly as
    ``importlib.import_module`` does, so a bounded name bounds it. pandas
    imports its hard dependencies this way, in a loop over a literal tuple,
    and treating that as unbounded selected its whole suite."""
    base = repo.commit(
        {
            "pkg/__init__.py": (
                "_deps = ('pkg.needed',)\n\nfor _d in _deps:\n    __import__(_d)\n\ndel _deps, _d\n"
            ),
            "pkg/needed.py": "STAMP = len('needed')\n",
            "pkg/other.py": "STAMP = len('other')\n",
            "tests/test_pkg.py": "import pkg\n\n\ndef test_pkg():\n    assert pkg is not None\n",
            "benchmarks/bench_other.py": (
                "from pkg.other import STAMP\n\n\n"
                "class Other:\n    def time_other(self):\n        return STAMP\n"
            ),
        }
    )
    targets = [
        py_target("t::pkg", "tests.test_pkg.test_pkg"),
        asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
    ]
    # The import reaches pkg.needed and nothing else: a change elsewhere does
    # not select the test through it.
    other = repo.commit({"pkg/other.py": "STAMP = len('other!')\n"})
    plan = repo.plan(base, other, targets)
    assert selected(plan) == {"bench_other.Other.time_other"}
    assert not [u for u in plan.unresolved if u.kind == "dynamic"]

    needed = repo.commit({"pkg/needed.py": "STAMP = len('needed!')\n"})
    assert selected(repo.plan(other, needed, targets)) == {"t::pkg"}


def test_a_builtin_import_of_an_unbounded_name_still_reaches_anything(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "import os\n\n__import__(os.environ['MODULE'])\n",
            "pkg/other.py": "def helper():\n    return 1\n",
            "tests/test_pkg.py": "import pkg\n\n\ndef test_pkg():\n    assert pkg is not None\n",
            "benchmarks/bench_other.py": (
                "from pkg.other import helper\n\n\n"
                "class Other:\n    def time_other(self):\n        return helper()\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "def helper():\n    return 2\n"})
    plan = repo.plan(
        base,
        head,
        [
            py_target("t::pkg", "tests.test_pkg.test_pkg"),
            asv_target("bench_other.Other.time_other", "benchmarks.bench_other.Other.time_other"),
        ],
    )
    assert selected(plan) == {"t::pkg", "bench_other.Other.time_other"}
    assert [u.detail for u in plan.unresolved if u.kind == "dynamic"] == [
        "__import__(<non-literal>)"
    ]


def test_a_by_name_import_belongs_to_the_caller_that_named_it(repo):
    """One caller passing something unbounded made the whole helper an
    unbounded seed, and everything reaching it was selected: pandas'
    ``import_optional_dependency`` has 124 call sites, 121 of them literal.
    The import now belongs to the caller that named the module."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/optional.py": (
                "import importlib\n\n\ndef need(name):\n    return importlib.import_module(name)\n"
            ),
            "pkg/arrow.py": "STAMP = len('arrow')\n",
            "pkg/plot.py": "STAMP = len('plot')\n",
            "pkg/users.py": (
                "import os\n\nfrom pkg.optional import need\n\n\n"
                "def use_arrow():\n    return need('pkg.arrow')\n\n\n"
                "def use_anything():\n    return need(os.environ['NAME'])\n"
            ),
            "tests/test_arrow.py": (
                "from pkg.users import use_arrow\n\n\ndef test_arrow():\n    assert use_arrow()\n"
            ),
            "benchmarks/bench_any.py": (
                "from pkg.users import use_anything\n\n\n"
                "class Any:\n    def time_any(self):\n        return use_anything()\n"
            ),
        }
    )
    targets = [
        py_target("t::arrow", "tests.test_arrow.test_arrow"),
        asv_target("bench_any.Any.time_any", "benchmarks.bench_any.Any.time_any"),
    ]
    # pkg.plot is named by nobody: only the caller that cannot name its module
    # is selected conservatively, not everything that reaches the helper.
    plot = repo.commit({"pkg/plot.py": "STAMP = len('plot!')\n"})
    plan = repo.plan(base, plot, targets)
    assert selected(plan) == {"bench_any.Any.time_any"}
    assert [u.symbol for u in plan.unresolved if u.kind == "dynamic"] == ["pkg.users.use_anything"]

    # The caller that named pkg.arrow is selected when that module changes.
    arrow = repo.commit({"pkg/arrow.py": "STAMP = len('arrow!')\n"})
    assert selected(repo.plan(plot, arrow, targets)) == {
        "t::arrow",
        "bench_any.Any.time_any",
    }


def test_a_name_passed_on_is_followed_to_the_caller_that_knows_it(repo):
    """pandas' ``skip_if_no(name)`` hands its parameter to the importer, so
    the answer is one level further out; the search follows it."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/optional.py": (
                "import importlib\n\n\n"
                "def need(name):\n    return importlib.import_module(name)\n\n\n"
                "def skip_if_no(name):\n    return need(name)\n"
            ),
            "pkg/arrow.py": "STAMP = len('arrow')\n",
            "pkg/plot.py": "STAMP = len('plot')\n",
            "tests/test_arrow.py": (
                "from pkg.optional import skip_if_no\n\n\n"
                "def test_arrow():\n    assert skip_if_no('pkg.arrow')\n"
            ),
            "benchmarks/bench_plot.py": (
                "from pkg.plot import STAMP\n\n\n"
                "class P:\n    def time_plot(self):\n        return STAMP\n"
            ),
        }
    )
    targets = [
        py_target("t::arrow", "tests.test_arrow.test_arrow"),
        asv_target("bench_plot.P.time_plot", "benchmarks.bench_plot.P.time_plot"),
    ]
    plot = repo.commit({"pkg/plot.py": "STAMP = len('plot!')\n"})
    assert selected(repo.plan(base, plot, targets)) == {"bench_plot.P.time_plot"}
    arrow = repo.commit({"pkg/arrow.py": "STAMP = len('arrow!')\n"})
    assert selected(repo.plan(plot, arrow, targets)) == {"t::arrow"}

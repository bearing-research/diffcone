"""Evidence-mode scenarios (docs/evidence_design.md).

Each records real evidence in a fixture repository (``diffcone collect``
runs its suite under the recorder), commits a change and plans with that
evidence. pytest targets come from static discovery; ASV targets from a
manifest, which evidence does not cover, so they keep their static
selection. Assertions check exact target sets and the rules behind them.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.testing import asv_target, reason, rules, selected

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

OPS = """\
def add(a, b):
    return a + b


def mul(a, b):
    return a * b
"""

TEST_OPS = """\
from pkg.ops import add, mul


def test_add():
    assert add(1, 2) == 3


def test_mul():
    assert mul(2, 3) == 6
"""

BENCH = """\
from pkg.ops import add, mul


class TimeOps:
    def time_add(self):
        add(1, 2)

    def time_mul(self):
        mul(2, 3)
"""

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TEST_OPS,
    "benchmarks/__init__.py": "",
    "benchmarks/bench_ops.py": BENCH,
}
ASV = [
    asv_target("bench_ops.TimeOps.time_add", "benchmarks.bench_ops.TimeOps.time_add"),
    asv_target("bench_ops.TimeOps.time_mul", "benchmarks.bench_ops.TimeOps.time_mul"),
]
ADD, MUL = "tests/test_ops.py::test_add", "tests/test_ops.py::test_mul"
BENCHES = {"bench_ops.TimeOps.time_add", "bench_ops.TimeOps.time_mul"}


def _plan(repo, base, head, evidence, **kwargs):
    return repo.plan(base, head, ASV, discover_runners=["pytest"], evidence=evidence, **kwargs)


def _collected(repo, files):
    base = repo.commit(files)
    return base, repo.collect()


def test_a_body_change_selects_the_tests_that_executed_it(repo):
    base, ev = _collected(repo, BASE)
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {ADD, "bench_ops.TimeOps.time_add"}
    assert rules(plan, ADD) == {"executed_changed"}
    assert "executed pkg.ops.add" in reason(plan, ADD, "executed_changed").detail
    assert rules(plan, "bench_ops.TimeOps.time_add") == {"dependency"}  # static, for ASV
    assert plan.evidence["planned"] == [f"{base[:12]} -> head"]


DISPATCH = {
    **BASE,
    "pkg/api.py": """\
class Ops:
    def a(self):
        return 1

    def b(self):
        return 2


def call(obj, name):
    return getattr(obj, name)()


def has(obj, name):
    return hasattr(obj, name)
""",
    "tests/test_api.py": """\
from pkg.api import Ops, call, has


def test_call_a():
    assert call(Ops(), "a") == 1


def test_b():
    assert Ops().b() == 2


def test_has():
    assert not has(Ops(), "c")
""",
}
CALL_A, B, HAS = ("tests/test_api.py::" + n for n in ("test_call_a", "test_b", "test_has"))


def test_a_method_reached_only_by_a_dynamic_lookup_is_in_the_record(repo):
    base, ev = _collected(repo, DISPATCH)
    head = repo.commit(
        {"pkg/api.py": DISPATCH["pkg/api.py"].replace("return 1", "return 1 + 0", 1)}
    )
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {CALL_A}
    # Static planning binds the getattr to every member of the class handed
    # to it, so every test holding an Ops is selected.
    static = repo.plan(base, head, ASV, discover_runners=["pytest"])
    assert selected(static) == {CALL_A, B, HAS}


def test_an_added_method_selects_the_tests_that_ran_an_unbounded_lookup(repo):
    base, ev = _collected(repo, DISPATCH)
    api = DISPATCH["pkg/api.py"].replace(
        "    def b(self):", "    def c(self):\n        return 3\n\n    def b(self):"
    )
    head = repo.commit({"pkg/api.py": api})
    plan = _plan(repo, base, head, ev)
    # test_has would now find c. call() is bound: its callers name "a".
    assert selected(plan) == {HAS}
    assert rules(plan, HAS) == {"lookup_site"}


def test_a_changed_signature_reaches_callers_through_a_lookup(repo):
    base, ev = _collected(repo, DISPATCH)
    api = DISPATCH["pkg/api.py"].replace("    def b(self):", "    def b(self, extra=0):")
    head = repo.commit({"pkg/api.py": api})
    plan = _plan(repo, base, head, ev)
    # test_b executed Ops.b; test_has ran a lookup that could reach it.
    # call() is bound to the names its callers pass ("a").
    assert selected(plan) == {B, HAS}
    assert rules(plan, B) >= {"executed_changed"}


DATA = {
    **BASE,
    "pkg/data.json": "[1]",
    "pkg/files.py": """\
import json
import os


def load():
    with open("pkg/data.json") as f:
        return json.load(f)


def names():
    return sorted(os.listdir("pkg"))


def has_extra():
    return os.path.exists("pkg/extra.txt")
""",
    "tests/test_files.py": """\
from pkg.files import has_extra, load, names


def test_load():
    assert load() == [1]


def test_names():
    assert "data.json" in names()


def test_extra():
    assert not has_extra()
""",
}
LOAD, NAMES, EXTRA = (
    "tests/test_files.py::" + n for n in ("test_load", "test_names", "test_extra")
)


def test_a_changed_data_file_selects_the_tests_that_touched_it(repo):
    base, ev = _collected(repo, DATA)
    head = repo.commit({"pkg/data.json": "[2]"})
    plan = _plan(repo, base, head, ev)
    # test_names only listed the directory holding it, which an edit does not
    # change. ASV targets keep static planning, which reads no data file.
    assert selected(plan) == {LOAD, *BENCHES}
    assert rules(plan, LOAD) == {"touched_file"}
    assert plan.fallbacks == []


def test_an_added_file_selects_a_test_that_checked_it_was_absent(repo):
    base, ev = _collected(repo, DATA)
    head = repo.commit({"pkg/extra.txt": "x"})
    plan = _plan(repo, base, head, ev)
    # One test checked for it, one listed its directory.
    assert selected(plan) == {EXTRA, NAMES, *BENCHES}
    assert rules(plan, EXTRA) == {"touched_file"}


def test_compiled_source_and_configuration_select_everything(repo):
    base, ev = _collected(repo, {**DATA, "pkg/_speed.pyx": "def f(): pass\n"})
    head = repo.commit({"pkg/_speed.pyx": "def f(): return 1\n"})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) >= {LOAD, NAMES, EXTRA, ADD, MUL}
    assert rules(plan, ADD) == {"unobserved_file_changed"}
    head2 = repo.commit({"pytest.ini": "[pytest]\n"})
    plan2 = _plan(repo, base, head2, ev)
    assert rules(plan2, ADD) == {"unobserved_file_changed"}


IMPORT_TIME = {
    **BASE,
    "pkg/registry.py": "def make():\n    return 1\n",
    "pkg/table.py": "from pkg.registry import make\n\nVALUE = make()\n",
    "tests/test_table.py": "from pkg.table import VALUE\n\n\n"
    "def test_value():\n    assert VALUE == 1\n",
}
VALUE = "tests/test_table.py::test_value"


def test_code_run_at_import_escalates_the_importing_module(repo):
    base, ev = _collected(repo, IMPORT_TIME)
    head = repo.commit({"pkg/registry.py": "def make():\n    return 2\n"})
    plan = _plan(repo, base, head, ev)
    # test_value never executed make: pkg.table's import did, and built VALUE.
    assert selected(plan) == {VALUE}
    assert rules(plan, VALUE) == {"escalated"}
    assert plan.evidence["escalated_modules"] == ["pkg.table"]


FIXTURES = {
    **BASE,
    "tests/conftest.py": """\
import pytest

from pkg.ops import mul


@pytest.fixture(scope="session")
def shared():
    return mul(2, 2)
""",
    "tests/test_shared.py": """\
def test_x(shared):
    assert shared == 4


def test_y(shared):
    assert shared == 4


def test_z():
    assert True
""",
}
X, Y, Z = ("tests/test_shared.py::" + n for n in ("test_x", "test_y", "test_z"))


def test_a_shared_fixture_is_credited_to_every_user(repo):
    base, ev = _collected(repo, FIXTURES)
    head = repo.commit({"pkg/ops.py": OPS.replace("return a * b", "return b * a")})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {X, Y, MUL, "bench_ops.TimeOps.time_mul"}


def test_a_fixture_decorator_change_selects_every_test_in_its_scope(repo):
    base, ev = _collected(repo, FIXTURES)
    conftest = FIXTURES["tests/conftest.py"].replace('scope="session"', 'scope="module"')
    head = repo.commit({"tests/conftest.py": conftest})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {X, Y, Z, ADD, MUL}
    assert "test_scope" in rules(plan, Z)


def test_a_pytest_hook_change_selects_everything(repo):
    base, ev = _collected(
        repo, {**FIXTURES, "conftest.py": "def pytest_configure(config):\n    pass\n"}
    )
    head = repo.commit(
        {"conftest.py": "def pytest_configure(config):\n    config.option.verbose = 0\n"}
    )
    plan = _plan(repo, base, head, ev)
    assert selected(plan) >= {X, Y, Z, ADD, MUL}
    assert "pytest_hook_changed" in rules(plan, Z)


def test_tests_without_a_usable_record_are_always_selected(repo):
    files = {
        **BASE,
        "pkg/memo.py": "_V = None\n\n\ndef compute():\n    return 1\n\n\n"
        "def get():\n    global _V\n    if _V is None:\n        _V = compute()\n    return _V\n",
        "tests/test_misc.py": """\
import subprocess
import sys

from pkg.memo import get


def test_one():
    assert get() == 1


def test_two():
    assert get() == 1


def test_sub():
    subprocess.run([sys.executable, "-c", "pass"], check=True)
""",
    }
    base = repo.commit(files)
    ev = repo.collect(reverse_check=True)
    head = repo.commit(
        {"tests/test_new.py": "def test_new():\n    assert True\n", "pkg/unused.py": "X = 1\n"}
    )
    plan = _plan(repo, base, head, ev)
    one, two, sub = ("tests/test_misc.py::" + n for n in ("test_one", "test_two", "test_sub"))
    assert selected(plan) == {one, two, sub, "tests/test_new.py::test_new"}
    assert rules(plan, one) == {"unstable"}
    assert rules(plan, sub) == {"subprocess"}
    assert {"no_evidence", "new_target"} <= rules(plan, "tests/test_new.py::test_new")


def test_evidence_older_than_the_base_plans_both_sides(repo):
    c, ev = _collected(repo, BASE)
    base = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    head = repo.commit({"pkg/ops.py": OPS})  # reverts to C's content
    plan = _plan(repo, base, head, ev)
    # C -> head is empty, but base -> head changes add: C -> base finds it.
    assert selected(plan) == {ADD, "bench_ops.TimeOps.time_add"}
    assert plan.evidence["planned"] == [f"{c[:12]} -> head", f"{c[:12]} -> base"]


VALUES = {
    **BASE,
    "pkg/cfg.py": "LIMIT = 3\n",
    "pkg/use.py": """\
from pkg.cfg import LIMIT

TABLE = {"x": LIMIT}


def limit():
    return LIMIT


def lookup():
    return TABLE["x"]


def other():
    return 0
""",
    "tests/test_use.py": """\
from pkg.use import limit, lookup, other


def test_limit():
    assert limit() == 3


def test_lookup():
    assert lookup() == 3


def test_other():
    assert other() == 0
""",
}


def test_a_variable_is_followed_through_the_values_that_captured_it(repo):
    base, ev = _collected(repo, VALUES)
    head = repo.commit({"pkg/cfg.py": "LIMIT = 4\n"})
    plan = _plan(repo, base, head, ev)
    t = "tests/test_use.py::"
    assert selected(plan) == {t + "test_limit", t + "test_lookup"}
    assert rules(plan, t + "test_lookup") == {"executed_reader"}


CLASSES = {
    **BASE,
    "pkg/shapes.py": """\
class Shape:
    sides = 0

    def area(self):
        return 0


DEFAULT = Shape()


def sides(obj):
    return obj.sides


def area(obj):
    return obj.area()
""",
    "tests/test_shapes.py": """\
from pkg.shapes import DEFAULT, area, sides


def test_sides():
    assert sides(DEFAULT) == 0


def test_area():
    assert area(DEFAULT) == 0
""",
}
SIDES, AREA = "tests/test_shapes.py::test_sides", "tests/test_shapes.py::test_area"


def test_a_class_attribute_change_reaches_its_readers_by_name(repo):
    base, ev = _collected(repo, CLASSES)
    head = repo.commit(
        {"pkg/shapes.py": CLASSES["pkg/shapes.py"].replace("sides = 0", "sides = 4")}
    )
    plan = _plan(repo, base, head, ev)
    # sides() reads .sides off an object from elsewhere. test_area holds the
    # same Shape but never reads the attribute.
    assert selected(plan) == {SIDES}
    assert rules(plan, SIDES) == {"executed_reader"}


def test_a_skipped_test_whose_mark_is_removed_is_selected(repo):
    files = {
        **BASE,
        "tests/test_skip.py": "import pytest\n\n\n@pytest.mark.skip\n"
        "def test_later():\n    assert True\n\n\ndef test_now():\n    assert True\n",
    }
    base, ev = _collected(repo, files)
    head = repo.commit(
        {
            "tests/test_skip.py": "def test_later():\n    assert True\n\n\n"
            "def test_now():\n    assert True\n"
        }
    )
    plan = _plan(repo, base, head, ev)
    assert "tests/test_skip.py::test_later" in selected(plan)
    assert "changed_target" in rules(plan, "tests/test_skip.py::test_later")

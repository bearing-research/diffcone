"""Regression scenarios for misses found by evaluation after audit round 5.

* A module bound to another name (``m = ops``, module-level or local,
  ``m = pkg.ops`` after ``import pkg.ops``) and read through it
  (``m.value()``): a body change to ``pkg.ops.value`` selected nothing
  since audit round 3's W20, which name-matches an attribute read off a
  value of unknown type only to the module-level names of modules such a
  value can be, and did not count a module followed through a name. Two
  independent fixes: the resolver reads an attribute off a name every
  binding of which is a module as off the module (a ``references`` edge;
  a name that may hold something else keeps the name-matched read beside
  it), and a module bound to any name other than its import's is a module
  a value of unknown type can be.

Every scenario's change makes the test fail at the head commit, and an ASV
target beside it is selected or not by the same rules.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from diffcone.cache import ModuleCache, index_to_dict
from diffcone.indexer import build_index
from diffcone.model import UNRESOLVED_ATTRIBUTE, SnapshotInfo
from diffcone.snapshot import Snapshot
from diffcone.testing import asv_target, py_target, reason, rules, selected

OPS = "def value():\n    return 1\n\n\ndef other():\n    return 10\n"
OPS_VALUE = OPS.replace("return 1\n", "return 2\n")
OPS_OTHER = OPS.replace("return 10\n", "return 11\n")
BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/helpers.py": "from pkg import ops\n\nm = ops\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_ops.py": (
        "from pkg import ops\n\n\ndef time_value():\n    m = ops\n    m.value()\n"
    ),
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
BENCH = asv_target("bench_ops.time_value", "benchmarks.bench_ops.time_value")
NOOP = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")
H = "from pkg import ops\n\n\n"

# Read off the module through a name the resolver follows: a real edge.
FOLLOWED = {
    "local": H + "def test_p():\n    m = ops\n    assert m.value() == 1\n",
    "module_level": H + "m = ops\n\n\ndef test_p():\n    assert m.value() == 1\n",
    "dotted_local": (
        "import pkg.ops\n\n\ndef test_p():\n    m = pkg.ops\n    assert m.value() == 1\n"
    ),
    "dotted_module_level": (
        "import pkg.ops\n\nm = pkg.ops\n\n\ndef test_p():\n    assert m.value() == 1\n"
    ),
    "import_as": "import pkg.ops as o\n\n\ndef test_p():\n    m = o\n    assert m.value() == 1\n",
    "package": (
        "import pkg\nimport pkg.ops\n\n\n"
        "def test_p():\n    m = pkg\n    assert m.ops.value() == 1\n"
    ),
    "bound_method": H + "def test_p():\n    m = ops\n    f = m.value\n    assert f() == 1\n",
    "alias_of_alias": H + "def test_p():\n    a = ops\n    b = a\n    assert b.value() == 1\n",
    "annotated": (
        "import types\n\n" + H + "def test_p():\n    m: types.ModuleType = ops\n"
        "    assert m.value() == 1\n"
    ),
    "walrus": H + "def test_p():\n    if (m := ops):\n        assert m.value() == 1\n",
    "unpacking": H + "def test_p():\n    m, n = ops, 1\n    assert m.value() == n\n",
    "for_literal": H + "def test_p():\n    for m in (ops,):\n        assert m.value() == 1\n",
    "global": (
        H + "def _bind():\n    global m\n    m = ops\n\n\n"
        "def test_p():\n    _bind()\n    assert m.value() == 1\n"
    ),
    "enclosing": (
        H + "def test_p():\n    m = ops\n\n    def inner():\n        return m.value()\n\n"
        "    assert inner() == 1\n"
    ),
}
# The name may hold something else too: the edge, and the name-matched read.
MIXED = {
    "rebound": (
        H + "def make():\n    return object()\n\n\n"
        "def test_p():\n    m = make()\n    m = ops\n    assert m.value() == 1\n"
    ),
    "global_and_none": (
        H + "m = None\n\n\ndef _bind():\n    global m\n    m = ops\n\n\n"
        "def test_p():\n    _bind()\n    assert m.value() == 1\n"
    ),
    "nonlocal": (
        H
        + "def test_p():\n    m = None\n\n    def bind():\n        nonlocal m\n        m = ops\n\n"
        "    bind()\n    assert m.value() == 1\n"
    ),
}
# Forms the resolver does not follow: the read is name-matched, and the
# module is one a value of unknown type can be.
MATCHED = {
    "other_module": (
        "from tests import helpers\n\n\ndef test_p():\n    assert helpers.m.value() == 1\n"
    ),
    "imported_alias": "from tests.helpers import m\n\n\ndef test_p():\n    assert m.value() == 1\n",
    "with_as": (
        "import contextlib\n\n" + H + "def test_p():\n"
        "    with contextlib.nullcontext(ops) as m:\n        assert m.value() == 1\n"
    ),
    "parameter": H
    + "def check(p):\n    assert p.value() == 1\n\n\ndef test_p():\n    check(ops)\n",
    "private_parameter": (
        H + "def _check(p):\n    assert p.value() == 1\n\n\ndef test_p():\n    _check(ops)\n"
    ),
    "returned": (
        H
        + "def get():\n    return ops\n\n\n"
        + "def test_p():\n    m = get()\n    assert m.value() == 1\n"
    ),
    "private_returned": (
        H
        + "def _get():\n    return ops\n\n\n"
        + "def test_p():\n    m = _get()\n    assert m.value() == 1\n"
    ),
    "comprehension": H + "def test_p():\n    assert [m.value() for m in (ops,)] == [1]\n",
    "getattr": (
        "import pkg\nimport pkg.ops\n\n\ndef test_p():\n    m = getattr(pkg, 'ops')\n"
        "    assert m.value() == 1\n"
    ),
    "class_body": H
    + "class Holder:\n    mod = ops\n\n\ndef test_p():\n    assert Holder.mod.value() == 1\n",
    "default": H + "def test_p(m=ops):\n    assert m.value() == 1\n",
    "lambda_default": H + "def test_p():\n    f = lambda m=ops: m.value()\n    assert f() == 1\n",
    "list": H + "def test_p():\n    mods = [ops]\n    assert mods[0].value() == 1\n",
}
T_P = py_target("tests/test_p.py::test_p", "tests.test_p.test_p")


def _plan(repo, test: str, change: str):
    base = repo.commit({**BASE, "tests/test_p.py": test})
    head = repo.commit({"pkg/ops.py": change})
    return repo.plan(base, head, [T_P, BENCH, NOOP], source_roots=["."])


@pytest.mark.parametrize(
    "test",
    [*FOLLOWED.values(), *MIXED.values(), *MATCHED.values()],
    ids=[*FOLLOWED, *MIXED, *MATCHED],
)
def test_alias_a_module_read_through_another_name_is_selected(repo, test):
    """Before the fix, the ``MISSED`` forms and ``other_module`` selected
    nothing (the others selected the test through a module handed on)."""
    plan = _plan(repo, test, OPS_VALUE)
    assert selected(plan) == {T_P.runner_id, BENCH.runner_id}


@pytest.mark.parametrize("test", list(FOLLOWED.values()), ids=list(FOLLOWED))
def test_alias_a_followed_name_is_a_dependency_edge(repo, test):
    plan = _plan(repo, test, OPS_VALUE)
    assert rules(plan, T_P.runner_id) == {"dependency"}
    assert rules(plan, BENCH.runner_id) == {"dependency"}
    step = reason(plan, BENCH.runner_id).path[-1]
    assert (step.target, step.kind, step.detail) == ("pkg.ops.value", "references", "")


# The forms that missed: the use scan follows the name instead of counting
# the module as handed on (the other forms hand it on, so every member of
# it is a dependency of the reader).
MISSED = [
    "local",
    "module_level",
    "dotted_local",
    "dotted_module_level",
    "import_as",
    "package",
    "bound_method",
]


@pytest.mark.parametrize("form", MISSED)
def test_alias_another_function_of_the_module_selects_nothing(repo, form):
    plan = _plan(repo, FOLLOWED[form], OPS_OTHER)
    assert selected(plan) == set()


# ------------------------------------------------------------- the index


def _index(test: str, **files: str):
    """The index of BASE with ``test`` as the test module (and ``files``,
    by module name, in place of BASE's: "" drops one), the same whether
    computed, stored in the module cache or loaded back."""
    sources = {k: v for k, v in BASE.items() if k.endswith(".py")}
    sources["tests/test_p.py"] = test
    for module, text in files.items():
        path = module.replace(".", "/") + ".py"
        if text:
            sources[path] = text
        else:
            sources.pop(path)
    snap = Snapshot(
        info=SnapshotInfo(revision="x", commit="x"),
        source_roots=["."],
        files={k: v.encode() for k, v in sources.items()},
    )
    result = build_index(snap)
    with tempfile.TemporaryDirectory() as tmp:
        cache = ModuleCache(Path(tmp))
        assert index_to_dict(build_index(snap, module_cache=cache)) == index_to_dict(result)
        assert index_to_dict(build_index(snap, module_cache=cache)) == index_to_dict(result)
    return result


def _reads(idx) -> tuple[set[str], set[str]]:
    """What ``tests.test_p.test_p`` references, and the attribute names it
    reads off values of unknown type."""
    source = "tests.test_p.test_p"
    targets = {e.target for e in idx.edges if e.source == source and e.kind == "references"}
    names = {
        u.name for u in idx.unresolved if u.symbol == source and u.kind == UNRESOLVED_ATTRIBUTE
    }
    return targets, names


@pytest.mark.parametrize("test", list(FOLLOWED.values()), ids=list(FOLLOWED))
def test_alias_every_binding_a_module_reads_off_the_module(test):
    targets, names = _reads(_index(test))
    assert "pkg.ops.value" in targets
    assert "value" not in names


@pytest.mark.parametrize("test", list(MIXED.values()), ids=list(MIXED))
def test_alias_a_name_that_may_hold_something_else_keeps_the_name_matched_read(test):
    targets, names = _reads(_index(test))
    assert "pkg.ops.value" in targets
    assert "value" in names


@pytest.mark.parametrize("form", ["local", "module_level"])
def test_alias_a_module_bound_to_a_name_is_one_an_unknown_value_can_be(form):
    plain = {
        "tests.helpers": "",
        "benchmarks.bench_ops": H + "def time_value():\n    ops.value()\n",
    }
    assert "pkg.ops" in _index(FOLLOWED[form], **plain).attribute_modules
    # Read through its import alone, it is not.
    direct = H + "def test_p():\n    assert ops.value() == 1\n"
    assert "pkg.ops" not in _index(direct, **plain).attribute_modules

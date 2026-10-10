"""Regression scenarios for the findings known after the third audit round
(internal/audit.md, "Known after round 3"): W2 (an attribute read off a
class that the class does not define) and W6 (a name a function binds
bounded by the module's literal of that name).

Each test names the finding it guards and failed before its fix.
"""

from __future__ import annotations

import ast
import sys

import pytest

from diffcone.indexer import build_index
from diffcone.indexer.literals import make_evaluator
from diffcone.snapshot import read_snapshot
from diffcone.testing import (
    asv_target,
    changes,
    path_ids,
    py_target,
    reason,
    selected,
)

ROOTS = ["src", "tests=tests", "benchmarks=benchmarks"]
TEST_ID = "tests/test_core.py::test_core"
BENCH_ID = "bench_core.time_core"
OTHER_ID = "bench_other.time_helper"
TARGETS = [
    py_target(TEST_ID, "tests.test_core.test_core"),
    asv_target(BENCH_ID, "benchmarks.bench_core.time_core"),
    asv_target(OTHER_ID, "benchmarks.bench_other.time_helper"),
]
TREE = {
    "src/pkg/__init__.py": "",
    "src/pkg/other.py": "def helper():\n    return 1\n",
    "src/pkg/impl.py": "def impl():\n    return 1\n",
    "benchmarks/bench_other.py": (
        "from pkg.other import helper\n\n\ndef time_helper():\n    helper()\n"
    ),
}
IMPL_CHANGED = {"src/pkg/impl.py": "def impl():\n    return 2\n"}


def _readers(expr: str, imports: str = "from pkg.core import C\n") -> dict[str, str]:
    """A pytest test and an ASV benchmark that read ``expr``, which names
    the class only through an attribute it does not define."""
    return {
        "tests/test_core.py": f"{imports}\n\ndef test_core():\n    assert {expr}\n",
        "benchmarks/bench_core.py": f"{imports}\n\ndef time_core():\n    {expr}\n",
    }


needs_pep695 = pytest.mark.skipif(
    not hasattr(ast, "TypeAlias"), reason="PEP 695 syntax needs Python 3.12"
)

# (core before, reader expression, the change). Each change reaches the
# readers only through the class object, never at import.
W2_CASES = [
    pytest.param(
        "from pkg.impl import impl\n\n\nclass C[T: impl]:\n    pass\n",
        "C.__type_params__[0].__bound__() == 1",
        IMPL_CHANGED,
        marks=needs_pep695,
        id="type parameter bound",
    ),
    pytest.param(
        'class C:\n    """a"""\n',
        "C.__doc__ == 'a'",
        {"src/pkg/core.py": 'class C:\n    """b"""\n'},
        id="class docstring",
    ),
    pytest.param(
        'class C:\n    """a"""\n',
        "getattr(C, '__doc__') == 'a'",
        {"src/pkg/core.py": 'class C:\n    """b"""\n'},
        id="class docstring via getattr",
    ),
    pytest.param(
        "class C:\n    def m(self):\n        return 1\n",
        "C.__dict__['m'](None) == 1",
        {"src/pkg/core.py": "class C:\n    def m(self):\n        return 2\n"},
        id="method through __dict__",
    ),
    pytest.param(
        "class C:\n    def m(self):\n        return 1\n",
        "C.__dict__.get('m')(None) == 1",
        {"src/pkg/core.py": "class C:\n    def m(self):\n        return 2\n"},
        id="method through __dict__.get",
    ),
    pytest.param(
        "class C:\n    def m(self):\n        return 1\n",
        "[f for k, f in C.__dict__.items() if k == 'm'][0](None) == 1",
        {"src/pkg/core.py": "class C:\n    def m(self):\n        return 2\n"},
        id="method through __dict__.items",
    ),
    pytest.param(
        "class A:\n    def m(self):\n        return 1\n\n\nclass C(A):\n    pass\n",
        "C.__mro__[1].__dict__['m'](None) == 1",
        {
            "src/pkg/core.py": (
                "class A:\n    def m(self):\n        return 2\n\n\nclass C(A):\n    pass\n"
            )
        },
        id="ancestor through __mro__",
    ),
    pytest.param(
        "class A:\n    def __call__(self):\n        return 1\n\n\nclass C(A):\n    pass\n",
        "C.__bases__[0]()() == 1",
        {
            "src/pkg/core.py": (
                "class A:\n    def __call__(self):\n        return 2\n\n\nclass C(A):\n    pass\n"
            )
        },
        id="ancestor through __bases__",
    ),
]


@pytest.mark.parametrize("core, expr, change", W2_CASES)
def test_w2_attribute_off_a_class_reaches_the_class(repo, core, expr, change):
    base = repo.commit({**TREE, "src/pkg/core.py": core, **_readers(expr)})
    head = repo.commit(change)
    plan = repo.plan(base, head, TARGETS, source_roots=ROOTS)
    assert changes(plan), "no changed symbol"
    assert selected(plan) == {TEST_ID, BENCH_ID}


SUBCLASS = "from pkg.core import C\n\n\nclass D(C):\n    def __call__(self):\n        return {}\n"


def test_w2_subclasses_hand_out_classes_nothing_names(repo):
    """``C.__subclasses__()`` yields D, which the reader never names: it
    constructs it and calls the instance."""
    base = repo.commit(
        {
            **TREE,
            "src/pkg/core.py": "class C:\n    pass\n",
            "src/pkg/sub.py": SUBCLASS.format(1),
            **_readers(
                "C.__subclasses__()[0]()() == 1", "import pkg.sub\nfrom pkg.core import C\n"
            ),
        }
    )
    head = repo.commit({"src/pkg/sub.py": SUBCLASS.format(2)})
    plan = repo.plan(base, head, TARGETS, source_roots=ROOTS)
    assert selected(plan) == {TEST_ID, BENCH_ID}
    assert "pkg.sub.D.__call__" in path_ids(reason(plan, TEST_ID))


@needs_pep695
def test_w2_class_object_read_on_cls(repo):
    """``cls.__type_params__`` in the class's own method: a method of C
    does not depend on C, but on what C's class object holds it does."""
    method = (
        "    @classmethod\n    def bound(cls):\n        return cls.__type_params__[0].__bound__()\n"
    )
    core = f"from pkg.impl import impl\n\n\nclass C[T: impl]:\n{method}"
    base = repo.commit({**TREE, "src/pkg/core.py": core, **_readers("C.bound() == 1")})
    head = repo.commit(IMPL_CHANGED)
    plan = repo.plan(base, head, TARGETS, source_roots=ROOTS)
    assert selected(plan) == {TEST_ID, BENCH_ID}


def test_w2_a_defined_attribute_keeps_its_precise_edge(repo):
    """Precision guard (passes before the fix as well): ``C.m`` resolves to
    the method, with no edge to the class object, and a change to another
    method of C selects nothing that reads ``C.m``."""
    core = (
        "class C:\n    def m(self):\n        return 1\n\n    def other(self):\n        return {}\n"
    )
    base = repo.commit({**TREE, "src/pkg/core.py": core.format(1), **_readers("C.m(None) == 1")})
    head = repo.commit({"src/pkg/core.py": core.format(2)})
    plan = repo.plan(base, head, TARGETS, source_roots=ROOTS)
    assert changes(plan) == {"pkg.core.C.other": ("body_changed",)}
    assert selected(plan) == set()
    index = build_index(read_snapshot(repo.path, base, source_roots=ROOTS))
    edges = {(e.target, e.detail) for e in index.edges if e.source == "tests.test_core.test_core"}
    assert ("pkg.core.C.m", "") in edges
    assert not any(detail == "class object" for _, detail in edges)


# --------------------------------------------------------------------------- W2, evidence

evidence_only = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
EV_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "tests/test_other.py": "def test_other():\n    assert True\n",
}


@evidence_only
@pytest.mark.parametrize(
    "core, test, after",
    [
        (
            'class C:\n    """a"""\n',
            "from pkg.core import C\n\n\ndef test_core():\n    assert C.__doc__ == 'a'\n",
            'class C:\n    """b"""\n',
        ),
        (
            'def f():\n    """a"""\n',
            "import inspect\n\nfrom pkg.core import f\n\n\n"
            "def test_core():\n    assert inspect.getdoc(f) == 'a'\n",
            'def f():\n    """b"""\n',
        ),
    ],
    ids=["class docstring", "function docstring"],
)
def test_w2_evidence_docstring_reader(repo, core, test, after):
    """A docstring-only change runs no other code: evidence selects the
    tests that ran code reading docstrings of what changed."""
    repo.commit({**EV_BASE, "pkg/core.py": core, "tests/test_core.py": test})
    ev = repo.collect()
    head = repo.commit({"pkg/core.py": after})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {TEST_ID}


@evidence_only
def test_w2_evidence_method_through_class_dict(repo):
    """Guard: the recording sees the method run (passes before the fix)."""
    core = "class C:\n    def m(self):\n        return {}\n"
    test = "from pkg.core import C\n\n\ndef test_core():\n    assert C.__dict__['m'](None)\n"
    repo.commit({**EV_BASE, "pkg/core.py": core.format(1), "tests/test_core.py": test})
    ev = repo.collect()
    head = repo.commit({"pkg/core.py": core.format(2)})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {TEST_ID}


# --------------------------------------------------------------------------- W6

W6_ROOTS = ["src", "tests=tests", "benchmarks=benchmarks"]
OPS = "def name():\n    return 0\n\n\ndef other():\n    return {}\n"
W6_TARGETS = [
    py_target("tests/test_x.py::test_x", "tests.test_x.test_x"),
    asv_target("bench_x.time_x", "benchmarks.bench_x.time_x"),
]


def _w6_tree(reader: str, call: str) -> dict[str, str]:
    return {
        "src/pkg/__init__.py": "",
        "src/pkg/ops.py": OPS.format(1),
        "src/pkg/reader.py": "from pkg import ops\n" + reader,
        "tests/test_x.py": f"from pkg.reader import read\n\n\ndef test_x():\n    assert {call}\n",
        "benchmarks/bench_x.py": f"from pkg.reader import read\n\n\ndef time_x():\n    {call}\n",
    }


@pytest.mark.parametrize(
    "reader, call",
    [
        (
            "NAMES = ('name',)\n\n\ndef read(NAMES):\n    return getattr(ops, NAMES[0])()\n",
            "read(['other'])",
        ),
        (
            "NAMES = ('name',)\n\n\ndef read(*, NAMES):\n    return getattr(ops, NAMES[0])()\n",
            "read(NAMES=['other'])",
        ),
        (
            "NAMES = ('name',)\n\n\ndef read(names):\n"
            "    f = lambda NAMES: getattr(ops, NAMES[0])()\n    return f(names)\n",
            "read(['other'])",
        ),
        (
            "NAMES = ('name',)\n\n\ndef read(names):\n"
            "    def inner(NAMES):\n        return getattr(ops, NAMES[0])()\n"
            "    return inner(names)\n",
            "read(['other'])",
        ),
        (
            "NAMES = ('name',)\n\n\ndef read(names):\n"
            "    return [getattr(ops, NAMES[0])() for NAMES, _ in [(names, 0)]][0]\n",
            "read(['other'])",
        ),
        (
            "NAMES = ('name',)\n\n\ndef read(names):\n"
            "    class K:\n        NAMES = names\n        v = getattr(ops, NAMES[0])()\n"
            "    return K.v\n",
            "read(['other'])",
        ),
        (
            "NAME = 'name'\n\n\ndef read(NAME, flag=False):\n"
            "    if flag:\n        NAME = 'name'\n    return getattr(ops, NAME)()\n",
            "read('other')",
        ),
    ],
    ids=[
        "parameter",
        "keyword-only parameter",
        "lambda parameter",
        "nested function parameter",
        "comprehension tuple target",
        "class body",
        "parameter assigned on a branch",
    ],
)
def test_w6_a_local_name_is_not_the_module_literal(repo, reader, call):
    """A name the scope binds shadows the module-level literal of the same
    name: ``def read(NAMES)`` holds whatever the caller passes."""
    base = repo.commit(_w6_tree(reader, call))
    head = repo.commit({"src/pkg/ops.py": OPS.format(2)})
    plan = repo.plan(base, head, W6_TARGETS, source_roots=W6_ROOTS)
    assert selected(plan) == {t.runner_id for t in W6_TARGETS}


def test_w6_a_default_is_evaluated_where_the_def_runs(repo):
    """Precision guard: ``def read(NAME=NAME)``'s default is the module's
    literal, so the parameter stays bounded by it."""
    reader = "NAME = 'other'\n\n\ndef read(NAME=NAME):\n    return getattr(ops, NAME)()\n"
    base = repo.commit(_w6_tree(reader, "read()"))
    changed_other = repo.commit({"src/pkg/ops.py": OPS.format(2)})
    plan = repo.plan(base, changed_other, W6_TARGETS, source_roots=W6_ROOTS)
    assert selected(plan) == {t.runner_id for t in W6_TARGETS}
    changed_name = repo.commit({"src/pkg/ops.py": OPS.format(2).replace("return 0", "return 5")})
    plan = repo.plan(changed_other, changed_name, W6_TARGETS, source_roots=W6_ROOTS)
    assert changes(plan) == {"pkg.ops.name": ("body_changed",)}
    assert selected(plan) == set()


def test_w6_evaluator_keeps_a_parameter_unbounded():
    """The first pass's evaluator (indexer.uses): a parameter assigned a
    literal on one branch may still hold what the caller passed."""
    tree = ast.parse(
        "NAME = 'a'\n\n\ndef f(NAME, flag):\n    if flag:\n        NAME = 'b'\n    return NAME\n"
    )
    evaluate = make_evaluator({"NAME": ("a",)})
    function = tree.body[1]
    assert isinstance(function, ast.FunctionDef)
    returned = function.body[-1]
    assert isinstance(returned, ast.Return) and returned.value is not None
    assert evaluate(returned.value, function, frozenset())[0] is None
    local = ast.parse("def g():\n    x = 'b'\n    return x\n").body[0]
    assert isinstance(local, ast.FunctionDef)
    returned = local.body[-1]
    assert isinstance(returned, ast.Return) and returned.value is not None
    assert evaluate(returned.value, local, frozenset())[0] == ("b",)

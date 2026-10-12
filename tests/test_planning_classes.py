"""Planning scenarios for classes: what a class body binds, what is read
off a class object, what runs when a class is created, and ``match`` class
patterns.

Assertions check exact target sets, with pytest and ASV targets side by
side.
"""

from __future__ import annotations

import ast

import pytest

from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
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

# --------------------------------------------------------------------------- class-body scoping
# Names in a class body resolve as Python scopes them: a method name used in
# the class body shadows a module-level homonym, and a class binding is
# invisible inside a comprehension of the class body.


@pytest.mark.parametrize(
    ("body", "changed"),
    [
        # A method name used in the class body, with a module-level homonym.
        (
            "def _get(self):\n    return 0\n\n\nclass C:\n    def _get(self):\n        return 1\n\n"
            "    value = property(_get)\n\n\ndef make():\n    return C().value\n",
            ("    def _get(self):\n        return 1\n", "    def _get(self):\n        return 11\n"),
        ),
        # A class binding is invisible inside a comprehension of the class body.
        (
            "def scale(x):\n    return x\n\n\nclass C:\n    scale = 10\n"
            "    ITEMS = [scale(x) for x in range(3)]\n\n\ndef make():\n    return C.ITEMS\n",
            ("def scale(x):\n    return x\n", "def scale(x):\n    return -x\n"),
        ),
    ],
)
def test_s3_class_body_names_resolve_as_python_scopes_them(repo, body, changed):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": body,
            "pkg/other.py": "def g():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "from pkg.m import make\n\n\ndef test_make():\n    make()\n",
            "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    g()\n",
        }
    )
    head = repo.commit({"pkg/m.py": body.replace(*changed)})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_m.py::test_make" in selected(plan)
    assert "tests/test_other.py::test_g" in unselected(plan)


# --------------------------------------------------------------- attributes read off a class object
# An attribute read off a class that the class does not define (``__doc__``,
# ``__dict__``, ``__mro__``, ``__bases__``, ``__subclasses__()``,
# ``__type_params__``) reaches the class object; one it defines keeps its
# precise edge.

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


# --------------------------------------------------------------- constructors run at class creation
# ``__init__``/``__new__`` run by class creation (the Enum family makes each
# member; a project metaclass may call ``cls()``) are dependencies of the
# class, consumed through ``Color.RED.code``. A plain class's ``__init__`` is
# not run at creation.

BARE_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _discovered_plan(repo, base, head, targets=(), evidence=None, **kwargs):
    return repo.plan(
        base, head, list(targets), discover_runners=["pytest"], evidence=evidence, **kwargs
    )


ENUM_RUNNERS = {
    "tests/test_x.py": "from pkg import api\n\n\ndef test_x():\n    assert api.run() == 1\n",
    "tests/test_other.py": "def test_other():\n    pass\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg import api\n\n\ndef time_run():\n    api.run()\n",
}
BENCH_RUN = asv_target("bench.time_run", "benchmarks.bench.time_run", "benchmarks.bench")
ENUM_CASES = {
    "enum_init": (
        (
            "import enum\n"
            "\n"
            "\n"
            "class Color(enum.Enum):\n"
            "    RED = 1\n"
            "\n"
            "    def __init__(self, v):\n"
            "        self.code = v{0}\n"
        ),
        "from pkg.mod import Color\n\n\ndef run():\n    return Color.RED.code\n",
    ),
    "from_enum_import_new": (
        (
            "from enum import IntFlag\n"
            "\n"
            "\n"
            "class Perm(IntFlag):\n"
            "    R = 1\n"
            "\n"
            "    def __new__(cls, v):\n"
            "        obj = int.__new__(cls, v)\n"
            "        obj._value_ = v\n"
            "        obj.code = v{0}\n"
            "        return obj\n"
        ),
        "from pkg.mod import Perm\n\n\ndef run():\n    return Perm.R.code\n",
    ),
    "project_enum_base": (
        (
            "from pkg.base import Base\n"
            "\n"
            "\n"
            "class Color(Base):\n"
            "    RED = 1\n"
            "\n"
            "    def __init__(self, v):\n"
            "        self.code = v{0}\n"
        ),
        "from pkg.mod import Color\n\n\ndef run():\n    return Color.RED.code\n",
    ),
    "project_metaclass": (
        (
            "from pkg.meta import M\n"
            "\n"
            "\n"
            "class C(metaclass=M):\n"
            "    def __init__(self):\n"
            "        self.code = 1{0}\n"
        ),
        "from pkg.mod import C\n\n\ndef run():\n    return C.inst.code\n",
    ),
}


@pytest.mark.parametrize("case", sorted(ENUM_CASES))
def test_r20_a_constructor_run_at_class_creation_is_the_class_s_dependency(repo, case):
    mod, api = ENUM_CASES[case]
    base = repo.commit(
        {
            **BARE_BASE,
            **ENUM_RUNNERS,
            "pkg/base.py": "import enum\n\n\nclass Base(enum.Enum):\n    pass\n",
            "pkg/meta.py": (
                "class M(type):\n"
                "    def __init__(cls, n, b, ns):\n"
                "        super().__init__(n, b, ns)\n"
                "        cls.inst = cls()\n"
            ),
            "pkg/mod.py": mod.format(""),
            "pkg/api.py": api,
        }
    )
    head = repo.commit({"pkg/mod.py": mod.format(" + 1")})
    plan = _discovered_plan(repo, base, head, [BENCH_RUN])
    assert len(changes(plan)) == 1 and all(c == ("body_changed",) for c in changes(plan).values())
    assert selected(plan) == {"tests/test_x.py::test_x", "bench.time_run"}
    assert rules(plan, "bench.time_run") == {"dependency"}


def test_r20_a_plain_class_s_init_is_not_run_at_creation(repo):
    """Guard (precision): a plain class's ``__init__`` body change reaches a
    reader of a class attribute through no edge."""
    mod = "class C:\n    TAG = 1\n\n    def __init__(self):\n        self.code = 1{0}\n"
    base = repo.commit(
        {
            **BARE_BASE,
            **ENUM_RUNNERS,
            "pkg/mod.py": mod.format(""),
            "pkg/api.py": "from pkg.mod import C\n\n\ndef run():\n    return C.TAG\n",
        }
    )
    head = repo.commit({"pkg/mod.py": mod.format(" + 1")})
    plan = _discovered_plan(repo, base, head, [BENCH_RUN])
    assert changes(plan) == {"pkg.mod.C.__init__": ("body_changed",)}
    assert selected(plan) == set()


# --------------------------------------------------------------------------- match class patterns
# ``match`` class patterns read attributes by name: ``case C(x=1)`` reads
# ``C.x``, ``case C(1)`` what ``__match_args__`` lists, or any attribute when
# the index does not see ``__match_args__`` written out.

NOOP = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")
BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}


# A test that imports nothing of the project: no rule but select-all reaches it.
CONTROL = "def test_c():\n    assert True\n"


T_C = py_target("tests/test_c.py::test_c", "tests.test_c.test_c")
T_X = py_target("tests/test_x.py::test_x", "tests.test_x.test_x")
TEST_X = "from pkg import api\n\n\ndef test_x():\n    assert api.run() == 1\n"


def _plan(repo, base, head, *targets, **kwargs):
    return repo.plan(base, head, [*targets, NOOP], source_roots=["."], **kwargs)


def _api_scenario(repo, mod, api, mod_changed, mod_other):
    """``tests/test_x.py`` calls ``pkg.api.run``, which uses ``pkg.mod``:
    the test is selected on ``mod_changed`` and not on ``mod_other``."""
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": mod,
            "pkg/tmpl.py": "FMT = '{0.v}'\n",
            "pkg/api.py": api,
            "tests/test_x.py": TEST_X,
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/mod.py": mod_changed}), T_X, T_C)
    assert selected(plan) == {T_X.runner_id}
    if mod_other is not None:
        plan2 = _plan(repo, base, repo.commit({"pkg/mod.py": mod_other}), T_X, T_C)
        assert selected(plan2) == set()
    return plan


PROPERTY_X = (
    "class C:\n    @property\n    def x(self):\n        return {x}\n\n"
    "    def other(self):\n        return 0\n"
)
MATCH_ARGS = (
    "class C:\n    __match_args__ = ('x',)\n\n    @property\n    def x(self):\n"
    "        return {x}\n\n    def other(self):\n        return 0\n"
)
INHERITED_MATCH_ARGS = "class Base:\n    __match_args__ = ('x',)\n\n\n" + MATCH_ARGS.replace(
    "class C:\n    __match_args__ = ('x',)\n\n", "class C(Base):\n"
)
MATCH = (
    "from pkg.mod import C\n\n\ndef run():\n    match C():\n        case {pattern}:\n"
    "            return 1\n        case _:\n            return 2\n"
)
MATCH_THROUGH_MODULE = MATCH.replace("from pkg.mod import C", "from pkg import mod").replace(
    "C()", "mod.C()"
)


@pytest.mark.parametrize(
    ("mod", "api"),
    [
        (PROPERTY_X, MATCH.format(pattern="C(x=1)")),
        (MATCH_ARGS, MATCH.format(pattern="C(1)")),
        (MATCH_ARGS, MATCH_THROUGH_MODULE.format(pattern="mod.C(1)")),
        (INHERITED_MATCH_ARGS, MATCH.format(pattern="C(1)")),
    ],
    ids=["keyword", "positional", "positional-through-module", "match_args-inherited"],
)
def test_r17_a_match_class_pattern_reads_the_attributes_it_names(repo, mod, api):
    """``case C(x=1)`` reads ``C.x``; ``case C(1)`` reads what
    ``__match_args__`` lists. A change to the property reaches the test;
    a change to another method of ``C`` does not."""
    other = mod.format(x=1).replace("return 0", "return 5")
    plan = _api_scenario(repo, mod.format(x=1), api, mod.format(x=2), other)
    assert rules(plan, T_X.runner_id) == {"dependency"}


def test_r17_positional_patterns_without_match_args_read_any_attribute(repo):
    """A dataclass (or any class whose ``__match_args__`` the index does not
    see written out) with positional patterns: every member of the class
    may be read."""
    mod = (
        "from dataclasses import dataclass\n\n\n@dataclass\nclass C:\n    x: int = 1\n\n"
        "    @property\n    def y(self):\n        return {y}\n"
    )
    _api_scenario(repo, mod.format(y=1), MATCH.format(pattern="C(1)"), mod.format(y=2), None)

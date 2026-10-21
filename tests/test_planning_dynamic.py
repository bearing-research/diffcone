"""Planning scenarios for dynamic code: what a lookup, a run-time module or a
program a test starts may reach.

A dynamic reference (``getattr`` by a name, a module named at run time, code
run from text or as a subprocess) is bounded only while the analysis can see
everything that decides it: a literal table nobody changes, a module nothing
writes onto, a program the test names. Each scenario either shows a form that
keeps the reference bounded (a change elsewhere selects nothing more) or a
form that makes it unbounded again, so the change reaches the test. The test
names keep the audit ids (internal/audit.md) of the findings they guard.
"""

from __future__ import annotations

import ast
import sys

import pytest

from diffcone.indexer import build_index
from diffcone.indexer.literals import make_evaluator
from diffcone.indexer.uses import _no_evaluate, scan_module
from diffcone.model import (
    EdgeKind,
    SnapshotInfo,
)
from diffcone.snapshot import Snapshot, read_snapshot
from diffcone.testing import (
    FixtureRepo,
    asv_target,
    changes,
    py_target,
    reason,
    rules,
    selected,
    unselected,
)

# --------------------------------------------------------------------------- literal tables
# A lookup bounded by a literal table.


HANDLERS = "def a():\n    return 1\n\n\ndef b():\n    return {}\n"


def _table_plan(repo, core, test="", extra=None):
    """``pkg.core.run`` looks a handler up by a name a literal table gives
    (``a``); ``pkg.handlers.b`` changes. ``test`` is extra code in the test
    module (which also calls ``run``), ``extra`` extra files."""
    files = {
        "pkg/__init__.py": "",
        "pkg/handlers.py": HANDLERS.format(1),
        "pkg/core.py": core,
        "tests/test_s.py": (
            "from pkg.core import run\n" + test + "\n\n\ndef test_s():\n    run()\n"
        ),
        "benchmarks/bench_s.py": (
            "from pkg.core import run\n\n\ndef time_run():\n    run()\n\n\n"
            "def time_nothing():\n    pass\n"
        ),
        **(extra or {}),
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/handlers.py": HANDLERS.format(2)})
    targets = [
        py_target("t::test_s", "tests.test_s.test_s"),
        asv_target("bench.time_run", "benchmarks.bench_s.time_run"),
        asv_target("bench.time_nothing", "benchmarks.bench_s.time_nothing"),
    ]
    return repo.plan(base, head, targets)


REACHED = {"t::test_s", "bench.time_run"}


CORE = (
    "from pkg import handlers\n\nTABLE = {{'k': 'a'}}\n{extra}\n\n\n"
    "def run():\n    return getattr(handlers, {lookup})()\n"
)


def _core(extra="", lookup="TABLE['k']"):
    return CORE.format(extra=extra, lookup=lookup)


@pytest.mark.parametrize("lookup", ["TABLE['k']", "TABLE.get('k')"])
def test_pln2_a_table_only_read_stays_bounded(repo, lookup):
    core = _core("assert 'k' in TABLE\nNAMES = sorted(TABLE)", lookup)
    assert selected(_table_plan(repo, core)) == set()


# Changes to the table inside its own module.
SAME_MODULE = {
    "alias": "_t = TABLE\n_t['k'] = 'b'",
    "helper": "\n\ndef _fill(d):\n    d['k'] = 'b'\n\n\n_fill(TABLE)",
    "dict_update": "dict.update(TABLE, k='b')",
    "default": "\n\ndef _reg(d=TABLE):\n    d['k'] = 'b'\n\n\n_reg()",
    "tuple_target": "TABLE['k'], _x = 'b', 1",
    "registry_call": "\n\ndef _keep(d):\n    return d\n\n\n_keep(TABLE)['k'] = 'b'",
}


@pytest.mark.parametrize("lookup", ["TABLE['k']", "TABLE.get('k')"])
@pytest.mark.parametrize("route", sorted(SAME_MODULE))
def test_pln2_a_table_changed_in_its_own_module_is_unbounded(repo, route, lookup):
    plan = _table_plan(repo, _core(SAME_MODULE[route], lookup))
    assert selected(plan) == REACHED


# Changes to the table from the test module (prelude, statement in the test).
OTHER_MODULE = {
    "attribute_item": ("from pkg import core", "core.TABLE['k'] = 'b'"),
    "dotted_item": ("import pkg.core", "pkg.core.TABLE['k'] = 'b'"),
    "rebound": ("from pkg import core", "core.TABLE = {'k': 'b'}"),
    "dotted_rebound": ("import pkg.core", "pkg.core.TABLE = {'k': 'b'}"),
    "setattr": ("from pkg import core", "setattr(core, 'TABLE', {'k': 'b'})"),
    "monkeypatch_setitem": (
        "import pytest\nfrom pkg import core",
        "pytest.MonkeyPatch().setitem(core.TABLE, 'k', 'b')",
    ),
    "monkeypatch_setattr": (
        "import pytest\nfrom pkg import core",
        "pytest.MonkeyPatch().setattr(core, 'TABLE', {'k': 'b'})",
    ),
    "patch_dict_object": (
        "from unittest import mock\nfrom pkg import core",
        "mock.patch.dict(core.TABLE, {'k': 'b'}).start()",
    ),
    "patch_dict_string": (
        "from unittest import mock",
        "mock.patch.dict('pkg.core.TABLE', {'k': 'b'}).start()",
    ),
    "patch_string": (
        "from unittest import mock",
        "mock.patch('pkg.core.TABLE', {'k': 'b'}).start()",
    ),
    "imported_alias": ("from pkg.core import TABLE", "t = TABLE\nt['k'] = 'b'"),
    "imported_tuple_target": ("from pkg.core import TABLE", "TABLE['k'], x = 'b', 1"),
    "imported_helper": (
        "from pkg.core import TABLE\n\n\ndef fill(d):\n    d['k'] = 'b'",
        "fill(TABLE)",
    ),
    "sys_modules": ("import sys", "sys.modules['pkg.core'].TABLE['k'] = 'b'"),
    "import_module": ("import importlib", "importlib.import_module('pkg.core').TABLE['k'] = 'b'"),
    "vars": ("from pkg import core", "vars(core)['TABLE']['k'] = 'b'"),
    "dunder_dict": ("from pkg import core", "core.__dict__['TABLE']['k'] = 'b'"),
    "module_passed_on": (
        "from pkg import core\n\n\ndef put(m):\n    m.TABLE['k'] = 'b'",
        "put(core)",
    ),
    "module_bound": ("from pkg import core", "m = core\nm.TABLE['k'] = 'b'"),
    "getattr_by_name": ("from pkg import core", "getattr(core, 'TABLE')['k'] = 'b'"),
    "exec": ("from pkg import core", "exec(\"core.TABLE['k'] = 'b'\")"),
    "star_import": ("from pkg.core import *", "TABLE['k'] = 'b'"),
    "reexport": ("import pkg", "pkg.TABLE['k'] = 'b'"),
}


@pytest.mark.parametrize("route", sorted(OTHER_MODULE))
def test_pln2_a_table_changed_from_another_module_is_unbounded(repo, route):
    prelude, statement = OTHER_MODULE[route]
    test = prelude + "\n" + statement + "\n"
    extra = {"pkg/__init__.py": "from pkg.core import TABLE  # noqa: F401\n"}
    plan = _table_plan(repo, _core(), test, extra if route == "reexport" else None)
    assert selected(plan) == REACHED


def test_pln2_a_list_extended_in_place_from_another_module_is_unbounded(repo):
    core = (
        "from pkg import handlers\n\nNAMES = ['a']\n\n\n"
        "def run():\n    for name in NAMES:\n        getattr(handlers, name)()\n"
    )
    test = "from pkg.core import NAMES\n\nNAMES += ['b']\n"
    assert selected(_table_plan(repo, core, test)) == REACHED


def test_pln2_a_set_changed_by_any_mutator_is_unbounded(repo):
    core = (
        "from pkg import handlers\n\nNAMES = {'a'}\n\n\n"
        "def run():\n    for name in NAMES:\n        getattr(handlers, name)()\n"
    )
    test = "from pkg.core import NAMES\n\nNAMES.symmetric_difference_update({'b'})\n"
    assert selected(_table_plan(repo, core, test)) == REACHED


def test_pln2_a_name_read_out_of_a_table_another_module_changes_is_unbounded(repo):
    # ``hook`` runs while ``core`` is importing (it imports the half-built
    # module): NAME is read after the change.
    core = (
        "from pkg import handlers\n\nTABLE = {'k': 'a'}\n\n"
        "import pkg.hook  # noqa: E402,F401\n\nNAME = TABLE['k']\n\n\n"
        "def run():\n    return getattr(handlers, NAME)()\n"
    )
    hook = "from pkg.core import TABLE\n\nTABLE['k'] = 'b'\n"
    assert selected(_table_plan(repo, core, extra={"pkg/hook.py": hook})) == REACHED


# The lazy-export table of 0.2.0.
LAZY = (
    "import importlib\n\n_LAZY = {{'Thing': ('pkg.impl_a', 'Thing')}}\n{extra}\n\n\n"
    "def __getattr__(name):\n    target = _LAZY.get(name)\n"
    "    if target is None:\n        raise AttributeError(name)\n"
    "    return getattr(importlib.import_module(target[0]), target[1])\n"
)


LAZY_CHANGES = {
    "alias": ("_T = _LAZY\n_T['Thing'] = ('pkg.impl_b', 'Alt')", ""),
    "helper": ("\n\ndef _fill(d):\n    d['Thing'] = ('pkg.impl_b', 'Alt')\n\n\n_fill(_LAZY)", ""),
    "globals": ("globals()['_LAZY'] = {'Thing': ('pkg.impl_b', 'Alt')}", ""),
    "setattr": ("", "import pkg\n\nsetattr(pkg, '_LAZY', {'Thing': ('pkg.impl_b', 'Alt')})\n"),
    "sys_modules": (
        "",
        "import sys\n\nsys.modules['pkg']._LAZY['Thing'] = ('pkg.impl_b', 'Alt')\n",
    ),
    "vars": ("", "import pkg\n\nvars(pkg)['_LAZY']['Thing'] = ('pkg.impl_b', 'Alt')\n"),
}


def _lazy_plan(repo, extra, other):
    base = repo.commit(
        {
            "pkg/__init__.py": LAZY.format(extra=extra),
            "pkg/impl_a.py": "class Thing:\n    v = 1\n",
            "pkg/impl_b.py": "class Alt:\n    v = 1\n",
            "pkg/other.py": other or "X = 1\n",
            "tests/test_t.py": "import pkg\n\n\ndef test_thing():\n    assert pkg.Thing().v\n",
            "tests/test_o.py": "import pkg.other  # noqa: F401\n\n\ndef test_o():\n    pass\n",
            "benchmarks/bench_t.py": "import pkg\n\n\ndef time_thing():\n    pkg.Thing()\n",
        }
    )
    head = repo.commit({"pkg/impl_b.py": "class Alt:\n    v = 2\n"})
    targets = [
        py_target("t::test_thing", "tests.test_t.test_thing"),
        py_target("t::test_o", "tests.test_o.test_o"),
        asv_target("bench.time_thing", "benchmarks.bench_t.time_thing"),
    ]
    return repo.plan(base, head, targets)


def test_pln2_a_lazy_table_only_read_stays_bounded(repo):
    assert selected(_lazy_plan(repo, "", "")) == set()


@pytest.mark.parametrize("route", sorted(LAZY_CHANGES))
def test_pln2_a_lazy_table_changed_is_unbounded(repo, route):
    extra, other = LAZY_CHANGES[route]
    plan = _lazy_plan(repo, extra, other)
    assert {"t::test_thing", "bench.time_thing"} <= selected(plan)


ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'


# I2: a name rebound by any form is no longer the literal it was bound to.


@pytest.mark.parametrize(
    "use",
    [
        "def use():\n    name = 'fast'\n    name += '_path'\n    return getattr(impls, name)()\n",
        "def use():\n    name = 'fast'\n    if (name := name + '_path'):\n"
        "        return getattr(impls, name)()\n",
        "def use():\n    name = 'fast'\n    name, _ = name + '_path', 1\n"
        "    return getattr(impls, name)()\n",
        "def use():\n    name = 'fast'\n\n    def grow():\n        nonlocal name\n"
        "        name += '_path'\n\n    grow()\n    return getattr(impls, name)()\n",
        "NAME = 'fast'\nNAME += '_path'\n\n\ndef use():\n    return getattr(impls, NAME)()\n",
        "NAME = 'fast'\n\n\ndef grow():\n    global NAME\n    NAME = NAME + '_path'\n\n\n"
        "grow()\n\n\ndef use():\n    return getattr(impls, NAME)()\n",
    ],
)
def test_i2_a_rebound_name_is_not_bounded_by_its_first_literal(repo, use):
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "pkg/__init__.py": "",
            "pkg/impls.py": "def fast():\n    return 1\n\n\ndef fast_path():\n    return 2\n",
            "pkg/a.py": "from pkg import impls\n\n\n" + use,
            "pkg/b.py": "def other():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "from pkg.a import use\n\n\ndef test_use():\n    use()\n",
            "tests/test_b.py": "from pkg.b import other\n\n\ndef test_other():\n    other()\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    impls = "def fast():\n    return 1\n\n\ndef fast_path():\n    return 20\n"
    head = repo.commit({"pkg/impls.py": impls})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_a.py::test_use" in selected(plan)
    assert {"tests/test_b.py::test_other", "bench.time_noop"} <= unselected(plan)


W6_ROOTS = ["src", "tests=tests", "benchmarks=benchmarks"]


W6_OPS = "def name():\n    return 0\n\n\ndef other():\n    return {}\n"


W6_TARGETS = [
    py_target("tests/test_x.py::test_x", "tests.test_x.test_x"),
    asv_target("bench_x.time_x", "benchmarks.bench_x.time_x"),
]


def _w6_tree(reader: str, call: str) -> dict[str, str]:
    return {
        "src/pkg/__init__.py": "",
        "src/pkg/ops.py": W6_OPS.format(1),
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
    head = repo.commit({"src/pkg/ops.py": W6_OPS.format(2)})
    plan = repo.plan(base, head, W6_TARGETS, source_roots=W6_ROOTS)
    assert selected(plan) == {t.runner_id for t in W6_TARGETS}


def test_w6_a_default_is_evaluated_where_the_def_runs(repo):
    """Precision guard: ``def read(NAME=NAME)``'s default is the module's
    literal, so the parameter stays bounded by it."""
    reader = "NAME = 'other'\n\n\ndef read(NAME=NAME):\n    return getattr(ops, NAME)()\n"
    base = repo.commit(_w6_tree(reader, "read()"))
    changed_other = repo.commit({"src/pkg/ops.py": W6_OPS.format(2)})
    plan = repo.plan(base, changed_other, W6_TARGETS, source_roots=W6_ROOTS)
    assert selected(plan) == {t.runner_id for t in W6_TARGETS}
    changed_name = repo.commit({"src/pkg/ops.py": W6_OPS.format(2).replace("return 0", "return 5")})
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


LOOKUP_HANDLERS = "def a():\n    return {}\n\n\ndef b():\n    return {}\n"


# ``pkg.core.run`` looks a handler up by a name a literal table gives (``a``);
# ``pkg.handlers.b`` changes, so the plan selects ``run``'s users only when
# something may change the table.
LOOKUP_CORE = (
    "from pkg import handlers\n\nTABLE = {'k': 'a'}\n\n\n"
    "def run():\n    return getattr(handlers, TABLE['k'])()\n"
)


TEST_RUN = "from pkg.core import run\n\n\ndef test_s():\n    run()\n"


def _handle_table_plan(repo, extra, test=TEST_RUN):
    files = {
        "pkg/__init__.py": "",
        "pkg/handlers.py": LOOKUP_HANDLERS,
        "pkg/core.py": LOOKUP_CORE,
        "plugins/__init__.py": "",
        "plugins/one.py": "X = 1\nTABLE = {'k': 'a'}\n",
        "tests/test_s.py": test,
        "benchmarks/bench_s.py": (
            "from pkg.core import run\n\n\ndef time_run():\n    run()\n\n\n"
            "def time_nothing():\n    pass\n"
        ),
        **extra,
    }
    base = repo.commit(files)
    head = repo.commit(
        {"pkg/handlers.py": LOOKUP_HANDLERS.replace("b():\n    return {}", "b():\n    return []")}
    )
    targets = [
        py_target("t::test_s", "tests.test_s.test_s"),
        asv_target("bench.time_run", "benchmarks.bench_s.time_run"),
        asv_target("bench.time_nothing", "benchmarks.bench_s.time_nothing"),
    ]
    return repo.plan(base, head, targets)


def _handle_check(plan, reached: bool) -> None:
    """The table stays bounded (nothing selected), or ``run`` became a
    dynamic reference again (its users selected)."""
    if reached:
        assert selected(plan) == REACHED
        assert rules(plan, "t::test_s") == {"dynamic_reference"}
    else:
        assert selected(plan) == set()


def test_w19_a_function_local_import_does_not_shadow_a_module_level_table(repo):
    # ``other`` imports a TABLE of its own; ``mutate`` writes this module's.
    core = LOOKUP_CORE + (
        "\n\ndef other():\n    from plugins.one import TABLE\n\n    return TABLE\n"
        "\n\ndef mutate():\n    TABLE['k'] = 'b'\n"
    )
    _handle_check(_handle_table_plan(repo, {"pkg/core.py": core}), True)


@pytest.mark.parametrize(
    "body, reached",
    [("return len(TABLE)", False), ("len = print\n    return len(TABLE)", True)],
    ids=["the builtin", "a local of that name"],
)
def test_w19_a_builtin_is_a_name_nothing_in_scope_binds(repo, body, reached):
    # A class elsewhere binding ``len`` does not make the builtin unknown; a
    # local of that name is no builtin, and may change what it is given.
    core = LOOKUP_CORE + f"\n\nclass Shape:\n    len: int = 0\n\n\ndef size():\n    {body}\n"
    _handle_check(_handle_table_plan(repo, {"pkg/core.py": core}), reached)


def test_w19_a_parameter_shadows_a_module_level_import():
    code = (
        "from __future__ import annotations\n\nimport sys\n\n\n"
        "def merge(annotations, sys):\n    annotations.update(sys)\n    return annotations\n"
    )
    found = scan_module(ast.parse(code), "pkg.m", False, _no_evaluate)
    assert {r.target for r in found.records} == set()


BENCH = {
    "asv.conf.json": ASV_CONF,
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}


BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")


# Pass 1 (indexer/uses.py) recognised library names through the imports in
# scope only: a name a star import binds (``from sys import *`` then
# ``modules[...]``) kept its bare name, so a module handle, an install in
# ``sys.modules``, an unpickled object or a ``gc`` list was not seen, and a
# store through one did not unbind the literal table it changed. Pass 1
# cannot see what a star import binds, so such a name may be any library
# name ending the same way where that adds what the name implies, and is
# not known to be one where it leaves something out. A record about a name
# a star import from outside the roots binds is about that module's name;
# and handing on a module below one looked up dynamically counts (as a
# write below it did), with or without a star import.

NAMES = {
    "pkg/__init__.py": "",
    "pkg/impl.py": "def f():\n    return 1\n\n\ndef g():\n    return {0}\n",
    "pkg/core.py": (
        "import pkg.impl\n\nNAMES = ('f',)\n\n\ndef run(i):\n"
        "    return getattr(pkg.impl, NAMES[i])()\n"
    ),
    "tests/__init__.py": "",
    "tests/test_a.py": "import pkg.w\n\n\ndef test_a():\n    pkg.w.go()\n",
    "tests/test_b.py": "import pkg.core\n\n\ndef test_b():\n    assert pkg.core.run(0)\n",
}


LOOKUP = {
    "pkg/__init__.py": "",
    "pkg/core.py": "import os\n\n\ndef run(name):\n    return getattr(os.path, name)()\n",
    "pkg/impl.py": "def g():\n    return {0}\n",
    "tests/__init__.py": "",
    "tests/test_a.py": "import pkg.w\n\n\ndef test_a():\n    pkg.w.go()\n",
    "tests/test_b.py": "import pkg.core\n\n\ndef test_b():\n    assert pkg.core.run('custom')\n",
}


PUT = "import pkg.impl\n\n\ndef put(m):\n    m.custom = pkg.impl.g\n\n\n"


@pytest.mark.parametrize(
    ("files", "writer"),
    [
        (
            NAMES,
            "from importlib import *\n\n\n"
            "def go():\n    import_module('pkg.core').NAMES = ('g',)\n",
        ),
        (NAMES, "from sys import *\n\n\ndef go():\n    modules['pkg.core'].NAMES = ('g',)\n"),
        (NAMES, "from pickle import *\n\n\ndef go(data):\n    loads(data).NAMES = ('g',)\n"),
        (NAMES, "from gc import *\n\n\ndef go():\n    get_objects()[0].NAMES = ('g',)\n"),
        (NAMES, "from gc import *\n\n\ndef go():\n    garbage[0].NAMES = ('g',)\n"),
        (NAMES, "from pkgutil import *\n\n\ndef go(n):\n    resolve_name(n).NAMES = ('g',)\n"),
        (NAMES, "from runpy import *\n\n\ndef go(p):\n    run_path(p)\n"),
        (
            {
                **NAMES,
                "pkg/impl.py": (
                    "class Fake:\n    @staticmethod\n    def run(i):\n        return {0}\n"
                ),
            },
            "from sys import *\n\nfrom pkg.impl import Fake\n\n\n"
            "def go():\n    modules['pkg.core'] = Fake()\n",
        ),
        (LOOKUP, f"import os\n\n{PUT}def go():\n    put(os.path)\n"),
        (LOOKUP, f"from os import *\n\n{PUT}def go():\n    put(path)\n"),
    ],
    ids=[
        "import_module",
        "sys_modules",
        "unpickled",
        "gc_objects",
        "gc_garbage_name",
        "resolve_name",
        "run_path",
        "install",
        "escape_below_lookup",
        "escape_through_star",
    ],
)
def test_l4_what_a_star_imported_name_does_is_seen(repo, files, writer):
    files = {**BENCH, **files, "pkg/w.py": writer}
    base = repo.commit({k: v.replace("{0}", "1") for k, v in files.items()})
    head = repo.commit({"pkg/impl.py": files["pkg/impl.py"].replace("{0}", "2")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert "tests/test_b.py::test_b" in selected(plan)
    assert "bench.time_noop" not in selected(plan)


def test_l4_a_builtin_a_star_import_may_rebind_is_not_a_read(repo):
    """``len``/``sorted`` of a table only read it, unless a star import
    binds the name to something else: then the table may change."""
    core = (
        "from pkg.helpers import *\n\nimport pkg.impl\n\nNAMES = ['f']\n\n\ndef run(i):\n"
        "    sorted(NAMES)\n    return getattr(pkg.impl, NAMES[i])()\n"
    )
    files = {
        **BENCH,
        **NAMES,
        "pkg/helpers.py": "def sorted(x):\n    x.append('g')\n    return x\n",
        "pkg/core.py": core,
        "pkg/w.py": "",
    }
    base = repo.commit({k: v.replace("{0}", "1") for k, v in files.items()})
    head = repo.commit({"pkg/impl.py": files["pkg/impl.py"].replace("{0}", "2")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert "tests/test_b.py::test_b" in selected(plan)
    assert "bench.time_noop" not in selected(plan)


# --------------------------------------------------------------------------- module namespaces
# A module's namespace read as a table.


# W10: a namespace read off any object is a getattr.


@pytest.mark.parametrize(
    "read",
    [
        "obj.__dict__[k]",
        "obj.__dict__.get(k)",
        "obj.__class__.__dict__[k]",
    ],
)
def test_w10_a_namespace_read_off_any_object_is_a_getattr(repo, read):
    """``x.__dict__[k]`` on an object of any type reads what ``getattr(x,
    k)`` reads; it recorded nothing unless ``x`` was a known module or
    class."""
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    def facts(body):
        repo.commit({"pkg/__init__.py": "", "pkg/reader.py": f"def read(obj, k):\n    {body}\n"})
        index = build_index(read_snapshot(repo.path, "HEAD", ["."]))
        return {
            (u.kind, u.name)
            for u in index.unresolved
            if u.symbol == "pkg.reader.read" and u.kind == "dynamic"
        }

    via_getattr = facts("return getattr(obj, k)")
    assert via_getattr
    assert facts(f"return {read}") == via_getattr


@pytest.mark.parametrize(
    "use",
    [
        "return k in obj.__dict__",
        "return [n for n in obj.__dict__]",
        "return len(obj.__dict__)",
        "return list(obj.__dict__.keys())",
    ],
)
def test_w10_a_use_of_the_names_only_reads_no_attribute(repo, use):
    """``m in cls.__dict__`` (flask's ``MethodView``) sees names, no value:
    no dynamic read."""
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    repo.commit({"pkg/__init__.py": "", "pkg/reader.py": f"def read(obj, k):\n    {use}\n"})
    index = build_index(read_snapshot(repo.path, "HEAD", ["."]))
    assert not [
        u for u in index.unresolved if u.symbol == "pkg.reader.read" and u.kind == "dynamic"
    ]


def _namespace_table_plan(repo, extra):
    files = {
        "pkg/__init__.py": "",
        "pkg/handlers.py": LOOKUP_HANDLERS,
        "pkg/core.py": LOOKUP_CORE,
        "plugins/__init__.py": "",
        "plugins/one.py": "X = 1\n",
        "tests/test_s.py": "from pkg.core import run\n\n\ndef test_s():\n    run()\n",
        "benchmarks/bench_s.py": (
            "from pkg.core import run\n\n\ndef time_run():\n    run()\n\n\n"
            "def time_nothing():\n    pass\n"
        ),
        **extra,
    }
    base = repo.commit(files)
    head = repo.commit(
        {"pkg/handlers.py": LOOKUP_HANDLERS.replace("b():\n    return {}", "b():\n    return []")}
    )
    targets = [
        py_target("t::test_s", "tests.test_s.test_s"),
        asv_target("bench.time_run", "benchmarks.bench_s.time_run"),
        asv_target("bench.time_nothing", "benchmarks.bench_s.time_nothing"),
    ]
    return repo.plan(base, head, targets)


def _namespace_check(plan, reached: bool) -> None:
    """The table stays bounded (nothing selected), or ``run`` became a
    dynamic reference again (its users selected, and maybe more)."""
    if reached:
        assert REACHED <= selected(plan)
        assert rules(plan, "t::test_s") == {"dynamic_reference"}
    else:
        assert selected(plan) == set()


CELLS = """\
import sys
import types

FLAG = "__cell_flag__"


def is_cell(value):
    module = sys.modules.get(type(value).__module__)
    return getattr(module, FLAG, None)


def load(name, source):
    module = types.ModuleType(name)
    exec(source, {namespace})
    return module
"""


@pytest.mark.parametrize(
    "namespace, reached",
    [("module.__dict__", False), ("{}", False), ("globals()", True)],
    ids=["a fresh module's namespace", "a fresh dict", "this module's own"],
)
def test_w1_exec_into_a_namespace_of_its_own_leaves_the_module_s_literals(repo, namespace, reached):
    # strata's serializer: code built at run time is exec'd into a fresh
    # module, and ``getattr(module, FLAG)`` hands an attribute of any module
    # on. Only an exec into this module's own namespace makes FLAG any name
    # (so any table of any module may be handed on).
    plan = _namespace_table_plan(repo, {"pkg/cells.py": CELLS.format(namespace=namespace)})
    _namespace_check(plan, reached)


@pytest.mark.parametrize(
    "key, reached",
    [("'__cell_flag__'", False), ("'TABLE'", True), ("name", True)],
    ids=["another name", "the table's name", "a name nothing bounds"],
)
def test_w1_a_literal_key_into_any_module_s_namespace_rebinds_that_name(repo, key, reached):
    # tests/notebook/test_serializer.py's ``_mark_as_cell_module``.
    mark = (
        "import sys\n\n\ndef mark(cls, name):\n"
        f"    module = sys.modules[cls.__module__]\n    module.__dict__[{key}] = True\n"
    )
    plan = _namespace_table_plan(repo, {"pkg/mark.py": mark})
    _namespace_check(plan, reached)


MOD = "def helper():\n    return {}\n\n\ndef other():\n    return 0\n"


def _reflection_plan(repo, core, test=None):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": MOD.format(1),
            "pkg/core.py": core,
            "tests/test_s.py": test
            or "from pkg.core import entry\n\n\ndef test_s():\n    entry()\n",
            "benchmarks/bench_s.py": "def time_nothing():\n    pass\n",
        }
    )
    head = repo.commit({"pkg/mod.py": MOD.format(2)})
    targets = [
        py_target("t::test_s", "tests.test_s.test_s"),
        asv_target("bench.time_nothing", "benchmarks.bench_s.time_nothing"),
    ]
    return repo.plan(base, head, targets)


REFLECTION = {
    "dunder_dict_item": (
        "from pkg import mod\n\n\ndef entry():\n    return mod.__dict__['helper']()\n"
    ),
    "dunder_dict_get": (
        "from pkg import mod\n\n\ndef entry():\n    return mod.__dict__.get('helper')()\n"
    ),
    "dunder_dict_by_name": (
        "from pkg import mod\n\n\ndef entry(n='helper'):\n    return mod.__dict__[n]()\n"
    ),
    "sys_modules_dict": (
        "import sys\n\nimport pkg.mod  # noqa: F401\n\n\n"
        "def entry(n='helper'):\n    return sys.modules['pkg.mod'].__dict__[n]()\n"
    ),
    "getmembers": (
        "import inspect\n\nfrom pkg import mod\n\n\n"
        "def entry():\n    return dict(inspect.getmembers(mod))['helper']()\n"
    ),
    "dunder_dict_items": (
        "from pkg import mod\n\n\ndef entry():\n"
        "    for _, f in list(mod.__dict__.items()):\n        if callable(f):\n            f()\n"
    ),
}


@pytest.mark.parametrize("route", sorted(REFLECTION))
def test_idx5_reflection_over_a_module_is_a_dependency(repo, route):
    assert selected(_reflection_plan(repo, REFLECTION[route])) == {"t::test_s"}


def test_idx5_parametrizing_over_a_modules_members_is_a_dependency(repo):
    test = (
        "import inspect\n\nimport pytest\n\nfrom pkg import mod\n\n\n"
        "@pytest.mark.parametrize(\n"
        "    'f', [f for _, f in inspect.getmembers(mod, inspect.isfunction)]\n"
        ")\n"
        "def test_s(f):\n    f()\n"
    )
    plan = _reflection_plan(repo, "def entry():\n    pass\n", test)
    assert selected(plan) == {"t::test_s"}


# --------------------------------------------------------------------------- external writes
# Writes onto a module outside the roots.


USE = (
    "import logging\n\nimport pkg.writer  # noqa: F401\n\n\n"
    "def run(name):\n    name = ''.join(list(name))\n    return getattr(logging, name)()\n"
)


def _external_plan(repo, prelude, write):
    writer = (
        prelude.replace("{", "{{").replace("}", "}}")
        + "\n\n\ndef handler():\n    return {}\n\n\n"
        + f"def install():\n    {write}\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/writer.py": writer.format(1),
            "pkg/use.py": USE,
            "tests/test_a.py": "from pkg.writer import install\n\n\ndef test_a():\n    install()\n",
            "tests/test_b.py": "from pkg.use import run\n\n\ndef test_b():\n    run('custom')\n",
            "benchmarks/bench_b.py": (
                "from pkg.use import run\n\n\ndef time_run():\n    run('custom')\n\n\n"
                "def time_nothing():\n    pass\n"
            ),
        }
    )
    head = repo.commit({"pkg/writer.py": writer.format(2)})
    targets = [
        py_target("t::test_a", "tests.test_a.test_a"),
        py_target("t::test_b", "tests.test_b.test_b"),
        asv_target("bench.time_run", "benchmarks.bench_b.time_run"),
        asv_target("bench.time_nothing", "benchmarks.bench_b.time_nothing"),
    ]
    return repo.plan(base, head, targets)


def test_pln3_a_lookup_on_an_external_module_nobody_writes_stays_bounded(repo):
    plan = _external_plan(repo, "import logging", "return logging.getLogger(handler.__name__)")
    assert selected(plan) == {"t::test_a"}


EXTERNAL_WRITES = {
    "local_alias": ("import logging", "m = logging\n    m.custom = handler"),
    "module_alias": ("import logging\n\nL = logging", "L.custom = handler"),
    "helper_attribute": (
        "import logging\n\n\ndef put(mod, f):\n    mod.custom = f",
        "put(logging, handler)",
    ),
    "helper_setattr": (
        "import logging\n\n\ndef put(mod, f):\n    setattr(mod, 'custom', f)",
        "put(logging, handler)",
    ),
    "loop": ("import logging", "for m in (logging,):\n        m.custom = handler"),
    "dunder_setattr": ("import logging", "logging.__setattr__('custom', handler)"),
    "object_setattr": ("import logging", "object.__setattr__(logging, 'custom', handler)"),
    "type_setattr": ("import logging", "type(logging).__setattr__(logging, 'custom', handler)"),
    "exec": ("import logging", "exec('logging.custom = handler')"),
    "monkeypatch": (
        "import logging\n\nimport pytest",
        "pytest.MonkeyPatch().setattr(logging, 'custom', handler)",
    ),
    "operator_setitem": (
        "import logging\nimport operator",
        "operator.setitem(vars(logging), 'custom', handler)",
    ),
    "patch_dict": (
        "import logging\nfrom unittest import mock",
        "mock.patch.dict(logging.__dict__, custom=handler).start()",
    ),
    "globals_item": ("import logging", "globals()['logging'].custom = handler"),
    "runtime_name_bound": (
        "import importlib",
        "m = importlib.import_module(''.join(['log', 'ging']))\n    m.custom = handler",
    ),
    "runtime_name_returned": (
        "import importlib\n\n\ndef load(n):\n    return importlib.import_module(n)",
        "load('logging').custom = handler",
    ),
}


@pytest.mark.parametrize("route", sorted(EXTERNAL_WRITES))
def test_pln3_a_write_onto_an_external_module_by_any_route_is_seen(repo, route):
    plan = _external_plan(repo, *EXTERNAL_WRITES[route])
    assert selected(plan) == {"t::test_a", "t::test_b", "bench.time_run"}


BUILTIN_LOOKUP = (
    "import builtins\n\n\ndef lookup(name):\n    return getattr(builtins, name, None)\n"
)


BUILTIN_WRITER = (
    "import builtins\n\nimport pytest\n\n\ndef install_names():\n"
    "    builtins.diffcone_named = 1\n    return 1\n\n\n"
    "@pytest.mark.parametrize('x', [1])\ndef test_writer(x):\n    install_names()\n"
    "    del builtins.diffcone_named\n"
)


@pytest.mark.parametrize("how", ["decorator", "default"])
def test_evp8_newly_running_a_writer_at_import_reaches_the_lookup(repo, how):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/names.py": BUILTIN_LOOKUP,
            "tests/test_names.py": (
                "from pkg.names import lookup\n\n\ndef test_names():\n"
                "    assert lookup('diffcone_named') is None\n"
            ),
            "tests/test_writer.py": BUILTIN_WRITER,
            "benchmarks/bench_n.py": (
                "from pkg.names import lookup\n\n\ndef time_lookup():\n    lookup('x')\n\n\n"
                "def time_nothing():\n    pass\n"
            ),
        }
    )
    if how == "decorator":
        writer = BUILTIN_WRITER.replace("[1])", "[install_names()])")
    else:
        writer = BUILTIN_WRITER.replace(
            "def test_writer(x):", "def test_writer(x, _=install_names()):"
        )
    head = repo.commit({"tests/test_writer.py": writer})
    targets = [
        py_target("t::test_names", "tests.test_names.test_names"),
        py_target("t::test_writer", "tests.test_writer.test_writer"),
        asv_target("bench.time_lookup", "benchmarks.bench_n.time_lookup"),
        asv_target("bench.time_nothing", "benchmarks.bench_n.time_nothing"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_names", "t::test_writer", "bench.time_lookup"}
    assert "dynamic_reference" in rules(plan, "t::test_names")


DISCOVER_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _discover_plan(repo, base, head, targets=(), evidence=None, **kwargs):
    return repo.plan(
        base, head, list(targets), discover_runners=["pytest"], evidence=evidence, **kwargs
    )


# R11: ``name in dir(builtins)`` (hasattr, vars, __dict__) on an external
# module in-scope code writes to was a reflection site static planning did
# not use; ``getattr(builtins, name)`` beside it was a lookup site that sees
# any change, since a writer may newly run anywhere.


@pytest.mark.parametrize(
    "lookup",
    [
        "name in dir(builtins)",
        "hasattr(builtins, name)",
        "name in vars(builtins)",
        "builtins.__dict__.get(name) is not None",
    ],
    ids=["dir", "hasattr", "vars", "dict"],
)
def test_r11_a_listing_of_a_written_external_module_is_a_lookup_site(repo, lookup):
    base = repo.commit(
        {
            **DISCOVER_BASE,
            "pkg/registry.py": (
                "CALLBACKS = []\n\n\ndef run_all():\n    for c in CALLBACKS:\n        c()\n"
            ),
            "pkg/names.py": f"import builtins\n\n\ndef is_installed(name):\n    return {lookup}\n",
            "pkg/plugin.py": "",
            "tests/test_a_writer.py": (
                "import builtins\n"
                "\n"
                "from pkg.registry import CALLBACKS\n"
                "\n"
                "\n"
                "def install_names():\n"
                "    builtins.diffcone_x = 1\n"
                "\n"
                "\n"
                "CALLBACKS.append(install_names)\n"
                "\n"
                "\n"
                "def test_writer():\n"
                "    pass\n"
            ),
            "tests/test_names.py": (
                "from pkg.names import is_installed\n"
                "\n"
                "\n"
                "def test_names():\n"
                "    assert not is_installed('diffcone_x')\n"
            ),
            "tests/test_plugin.py": (
                "import pkg.plugin  # noqa: F401\n\n\ndef test_plugin():\n    pass\n"
            ),
        }
    )
    head = repo.commit({"pkg/plugin.py": "from pkg.registry import run_all\n\nrun_all()\n"})
    plan = _discover_plan(repo, base, head)
    assert selected(plan) == {
        "tests/test_names.py::test_names",
        "tests/test_plugin.py::test_plugin",
    }
    assert rules(plan, "tests/test_names.py::test_names") == {"dynamic_reference"}


W24_PUT = {
    "installed": (
        "import sys\n\nimport tests.fake_registry\n\n"
        "sys.modules['pkg.registry'] = tests.fake_registry\n"
    ),
    "assigned": "import pkg\nimport tests.fake_registry\n\npkg.registry = tests.fake_registry\n",
    "setattr": (
        "import pkg.registry\nimport tests.fake_registry as fake\n\n"
        "for name in dir(fake):\n    setattr(pkg.registry, name, getattr(fake, name))\n"
    ),
}


@pytest.mark.parametrize("mode", ["static", "evidence"])
@pytest.mark.parametrize("put", sorted(W24_PUT))
def test_w24_a_lookup_on_a_module_sees_what_others_put_there(repo, put, mode):
    """``get`` looks a name nothing bounds up on ``pkg.registry``; a helper
    module another test imports puts another module's objects there (or
    that module in its place) for good, so a change to that module's
    ``VALUE`` is seen."""
    files = {
        "pkg/__init__.py": "",
        "pkg/registry.py": "",
        "pkg/lib.py": (
            "def get(name):\n    from pkg import registry\n\n    return getattr(registry, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/fake_registry.py": "VALUE = 1\n",
        "tests/helpers.py": W24_PUT[put],
        "tests/test_b.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def test_b():\n    assert get(os.environ.get('W24', 'VALUE')) == 1\n"
        ),
        "tests/test_a.py": "import tests.helpers  # noqa: F401\n\n\ndef test_a():\n    pass\n",
        "tests/test_c.py": "def test_c():\n    pass\n",
    }
    base = repo.commit(files)
    if mode == "evidence" and sys.version_info < (3, 12):
        pytest.skip("needs sys.monitoring")
    ev = repo.collect() if mode == "evidence" else None
    head = repo.commit({"tests/fake_registry.py": "VALUE = 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert "tests/test_b.py::test_b" in selected(plan)
    if put != "installed":  # installing in sys.modules at import is process state (W12)
        assert "tests/test_c.py::test_c" not in selected(plan)


def test_l4_an_object_of_a_library_handed_on_writes_no_module(repo):
    """marshmallow's shape: ``getattr(dt, name)`` looks on ``datetime``,
    and code elsewhere hands ``dt.timezone.utc`` and ``dt.datetime`` to
    calls. Those are objects of the library, no modules (the project imports
    neither by that name), so they are no writers onto ``datetime``: a
    change to one test's body selects that test, not every reader of the
    lookup. (Handing a module below the looked-up one on still counts.)"""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/fields.py": "import datetime as dt\n\n\ndef parse(name, v):\n"
            "    return getattr(dt, name)(v)\n",
            "pkg/utils.py": "import datetime as dt\n\n\ndef stamp(value):\n"
            "    return value.replace(tzinfo=dt.timezone.utc)\n\n\n"
            "def kind():\n    return type_name(dt.datetime)\n\n\n"
            "def type_name(t):\n    return t.__name__\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "from pkg.fields import parse\n\n\n"
            "def test_a():\n    assert parse('date', 1)\n",
            "tests/test_b.py": "from pkg.utils import stamp\n\n\ndef test_b():\n    assert stamp\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "from pkg.fields import parse\n\n\n"
            "def time_parse():\n    parse('date', 1)\n",
        }
    )
    head = repo.commit(
        {
            "tests/test_b.py": (
                "from pkg.utils import stamp\n\n\ndef test_b():\n    assert stamp\n    pass\n"
            )
        }
    )
    plan = repo.plan(
        base,
        head,
        [asv_target("bench.time_parse", "benchmarks.bench.time_parse")],
        discover_runners=["pytest"],
    )
    assert selected(plan) == {"tests/test_b.py::test_b"}


Q19_BASE = {
    "pkg/__init__.py": "",
    "pkg/config.py": "TIMEOUT = 5\nOTHER = 1\n",
    "pkg/api.py": "def fetch():\n    return 1\n",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}


def test_q19_patch_multiple_of_a_third_party_module_writes_it(repo):
    """``mock.patch.multiple("json", dumps=...)`` started and kept stores on
    ``json``: a lookup by a name nothing bounds on it is a dynamic
    reference, both through the use scan and the walker."""
    files = {
        **Q19_BASE,
        "pkg/api.py": "import json\n\n\ndef fetch(name):\n    return getattr(json, name)\n",
        "tests/helper.py": (
            "from unittest import mock\n\n\ndef install():\n"
            "    mock.patch.multiple('json', dumps=len).start()\n"
        ),
    }
    snapshot = read_snapshot(repo.path, repo.commit(files), source_roots=["."])
    index = build_index(snapshot)
    assert any(r.symbol == "pkg.api.fetch" and "getattr" in r.detail for r in index.unresolved), (
        index.unresolved
    )


@pytest.mark.parametrize(
    "call",
    [
        "mock.patch.multiple(api, fetch=lambda: 1)",
        "mock.patch.multiple('pkg.api', fetch=lambda: 1)",
    ],
    ids=["object", "string"],
)
def test_q19_patch_multiple_rebinds_the_attributes_it_names(repo, call):
    """A started ``patch.multiple`` rebinds ``pkg.api.fetch`` for later
    code: a module-level ``getattr`` on ``pkg.api`` by a name a literal
    table bounds is no longer bounded by the table (a SCOPED or STORE record
    for each keyword, not an escape of the whole module)."""
    files = {
        **Q19_BASE,
        "pkg/api.py": "def fetch():\n    return 1\n",
        "tests/helper.py": (
            f"from unittest import mock\n\nfrom pkg import api\n\n\ndef install():\n"
            f"    {call}.start()\n"
        ),
    }
    snapshot = read_snapshot(repo.path, repo.commit(files), source_roots=["."])
    index = build_index(snapshot)
    writers = index.module_writers.get("pkg.api", ())
    assert "tests.helper.install" in writers, index.module_writers


# --------------------------------------------------------------------------- modules as values
# A module held as a value or handed on.


@pytest.mark.parametrize(
    "api, reached",
    [
        ("from pkg import core\n", True),
        ("import pkg.core as core\n", True),
        ("import pkg.core\n", True),
        ("from pkg.core import TABLE\n", True),
        ("from pkg.core import *\n", True),
        ("from pkg import mid\n", True),
        ("import plugins.one\n", False),
    ],
    ids=["a module", "an alias", "its package", "a table", "a star import", "two hops", "none"],
)
@pytest.mark.parametrize("hand", ["return f(api)", "return getattr(api, name)"])
def test_w16_a_module_handed_on_hands_on_what_its_imports_bind(repo, api, reached, hand):
    # ``api`` is handed on (or any attribute of it is): whoever gets it can
    # write ``api.core.TABLE``, ``api.TABLE`` or ``api.mid.core.TABLE``,
    # though pkg.core is no submodule of pkg.api.
    files = {
        "pkg/mid.py": "from pkg import core\n",
        "pkg/api.py": api,
        "pkg/hand.py": f"from pkg import api\n\n\ndef go(f, name):\n    {hand}\n",
    }
    _handle_check(_handle_table_plan(repo, files), reached)


VALUES = "def real():\n    return 1\n\n\nclass Num:\n    def imag(self):\n        return 2\n"


VALUES_CHANGED = VALUES.replace("return 1", "return 10")


def _w20_plan(repo, use: str, extra: dict[str, str] | None = None, change=None):
    """``pkg.values.real`` changes; ``tests/test_use.py::test_get`` runs
    ``pkg.use.get`` (``use``), ``bench.time_real`` calls ``real`` itself."""
    files = {
        "pkg/__init__.py": "",
        "pkg/values.py": VALUES,
        "pkg/use.py": use,
        "tests/__init__.py": "",
        "tests/test_use.py": (
            "from pkg.use import get\n\n\ndef test_get():\n    assert get(complex(1, 2))\n"
        ),
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": (
            "from pkg.values import real\n\n\ndef time_real():\n    real()\n\n\n"
            "def time_nothing():\n    pass\n"
        ),
        **(extra or {}),
    }
    base = repo.commit(files)
    head = repo.commit(change or {"pkg/values.py": VALUES_CHANGED})
    targets = [
        py_target("t::test_get", "tests.test_use.test_get"),
        asv_target("bench.time_real", "benchmarks.bench.time_real"),
        asv_target("bench.time_nothing", "benchmarks.bench.time_nothing"),
    ]
    return repo.plan(base, head, targets)


DIRECT = {"bench.time_real"}


BOTH = {"t::test_get", "bench.time_real"}


def test_w20_attribute_on_a_value_is_no_module_level_name(repo):
    """``v.real`` on a value nothing types: no module hands ``pkg.values``
    on, so it cannot be ``pkg.values.real``."""
    plan = _w20_plan(repo, "def get(v):\n    return v.real\n")
    assert selected(plan) == DIRECT


def test_w20_a_class_member_still_matches(repo):
    """A method of that name is matched whatever the receiver."""
    plan = _w20_plan(
        repo,
        "def get(v):\n    return v.imag()\n",
        change={"pkg/values.py": VALUES.replace("return 2", "return 20")},
    )
    assert selected(plan) == {"t::test_get"}
    assert rules(plan, "t::test_get") == {"unresolved_name_match"}


def test_w20_a_bare_name_still_matches(repo):
    """A bare name nothing binds may be any symbol of that name (code
    ``exec`` or a star import put there)."""
    plan = _w20_plan(repo, "def get(v):\n    return real()\n")
    assert selected(plan) == BOTH


@pytest.mark.parametrize(
    "use",
    [
        # handed on (W9)
        "from pkg import values\n\n\ndef read(m):\n    return m.real()\n\n\n"
        "def get(v):\n    return read(values)\n",
        # a handle read in place, by a literal and by a name nothing bounds
        "import importlib\n\n\ndef get(v):\n"
        "    m = importlib.import_module('pkg.values')\n    return m.real()\n",
        "import importlib\n\n\ndef get(v, name='pkg.values'):\n"
        "    m = importlib.import_module(name)\n    return m.real()\n",
        "import sys\n\n\ndef get(v):\n    return sys.modules['pkg.values'].real()\n",
        "import sys\n\n\ndef get(v, name='pkg.values'):\n    return sys.modules[name].real()\n",
        "import pytest\n\n\ndef get(v):\n    return pytest.importorskip('pkg.values').real()\n",
        # held at module level, read by another function
        "import importlib\n\n_m = importlib.import_module('pkg.values')\n\n\n"
        "def get(v):\n    return _m.real()\n",
        # a graph walk
        "import gc\n\n\ndef get(v):\n    for o in gc.get_objects():\n"
        "        if getattr(o, '__name__', '') == 'pkg.values':\n            return o.real()\n",
        # an unpickled object
        "import pickle\n\n\ndef get(v):\n    return pickle.loads(v).real()\n",
        # a lazy package export (PEP 562)
        "import pkg\n\n\ndef get(v):\n    return pkg.real()\n",
    ],
    ids=[
        "handed-on",
        "import-module",
        "import-module-any",
        "sys-modules",
        "sys-modules-any",
        "importorskip",
        "held",
        "gc",
        "pickle",
        "lazy-export",
    ],
)
def test_w20_a_module_held_as_a_value_still_matches(repo, use):
    extra = {
        "pkg/__init__.py": (
            "import importlib\n\n\ndef __getattr__(name):\n"
            "    return getattr(importlib.import_module('pkg.values'), name)\n"
        )
    }
    plan = _w20_plan(repo, use, extra)
    assert "t::test_get" in selected(plan)
    assert DIRECT <= selected(plan)
    assert "bench.time_nothing" not in selected(plan)


def test_w20_what_a_handed_on_module_imports_still_matches(repo):
    """``api`` is handed on, and ``api.real`` is ``pkg.values.real`` (W16)."""
    plan = _w20_plan(
        repo,
        "from pkg import api\n\n\ndef read(m):\n    return m.real()\n\n\n"
        "def get(v):\n    return read(api)\n",
        {"pkg/api.py": "from pkg.values import real\n"},
    )
    assert selected(plan) == BOTH


def test_w20_a_module_found_at_run_time_handed_on_matches_everywhere(repo):
    """``gc.get_objects()`` handed to another module: any module may be the
    value ``pkg.use.get`` reads ``real`` off."""
    plan = _w20_plan(
        repo,
        "from pkg.walk import objects\n\n\ndef get(v):\n"
        "    for o in objects():\n        return o.real()\n",
        {"pkg/walk.py": "import gc\n\n\ndef objects():\n    return gc.get_objects()\n"},
    )
    assert selected(plan) == BOTH


def test_w20_a_test_module_the_runner_hands_out_still_matches(repo):
    """``request.module.helper()`` in a conftest fixture: pytest hands the
    test module out, so its module-level ``helper`` stays matched."""
    files = {
        "tests/__init__.py": "",
        "tests/conftest.py": (
            "import pytest\n\n\n@pytest.fixture\ndef value(request):\n"
            "    return request.module.helper()\n"
        ),
        "tests/test_mod.py": (
            "def helper():\n    return 1\n\n\ndef test_value(value):\n    assert value == 1\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit(
        {"tests/test_mod.py": files["tests/test_mod.py"].replace("return 1", "return 2")}
    )
    targets = [
        py_target("t::test_value", "tests.test_mod.test_value", "tests.conftest.value"),
        asv_target("bench.none", "tests.conftest"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_value"}


# --------------------------------------------------------------------------- run-time modules
# A module named, installed or loaded at run time.


@pytest.mark.parametrize(
    "name, reached",
    [
        ('f"plugins.{name}"', False),
        ('"plugins." + name', False),
        ('"cell_%s" % name', False),
        ('f"pkg.{name}"', True),
        ("name", True),
    ],
    ids=["an f-string prefix", "a concatenation", "a percent format", "the package's", "none"],
)
def test_w1_a_module_named_with_a_literal_prefix_is_one_with_that_prefix(repo, name, reached):
    # strata's ``register_default_adapters`` and a test importing
    # ``f"strata.notebook.{path}"``: the handle is handed on (returned), so
    # whatever module it may be may be written.
    loader = f"import importlib\n\n\ndef load(name):\n    return importlib.import_module({name})\n"
    plan = _namespace_table_plan(repo, {"pkg/loader.py": loader})
    _namespace_check(plan, reached)


@pytest.mark.parametrize("package, reached", [("plugins", False), ("pkg", True)])
def test_w1_a_module_named_under_dunder_name_is_a_submodule(repo, package, reached):
    # strata_client.integration's lazy ``__getattr__``.
    getter = (
        "import importlib\n\n\ndef __getattr__(name):\n"
        '    module = importlib.import_module(f"{__name__}.{name}")\n'
        "    globals()[name] = module\n    return module\n"
    )
    extra = {f"{package}/__init__.py": getter}
    plan = _namespace_table_plan(repo, extra)
    _namespace_check(plan, reached)


@pytest.mark.parametrize(
    "install, reached",
    [
        ("", False),
        ('sys.modules["cells." + name] = module', False),
        ('part = "cells"\n    sys.modules[f"{part}.x"] = module', False),
        ('sys.modules["pkg.core"] = module', True),
        ("sys.modules[name] = module", True),
        ('sys.modules.update({"pkg.core": module})', True),
    ],
    ids=[
        "not installed",
        "installed elsewhere",
        "by an f-string",
        "as pkg.core",
        "anywhere",
        "by update",
    ],
)
def test_w1_a_fresh_module_is_the_module_it_is_installed_as(repo, install, reached):
    # A new module object is no module the project imports, until it is
    # installed in sys.modules under a name one may have: whoever imports
    # pkg.core then gets it, with what was stored on it.
    fresh = (
        "import sys\nimport types\n\n\ndef make(name):\n"
        "    module = types.ModuleType(name)\n"
        "    module.TABLE = {'k': 'b'}\n"
        f"    {install}\n    return module\n"
    )
    plan = _namespace_table_plan(repo, {"pkg/fresh.py": fresh})
    _namespace_check(plan, reached)


FAKE_CORE = "types.SimpleNamespace(TABLE={'k': 'b'}, run=lambda: None)"


@pytest.mark.parametrize(
    "install",
    [
        "sys.modules['pkg.core'] = fake",
        "sys.modules.update({'pkg.core': fake})",
        "sys.modules.update({'plugins.other': fake})",
    ],
    ids=["as pkg.core", "by update", "by update, elsewhere"],
)
def test_w17_update_installs_only_the_names_it_gives(repo, install):
    # ``sys.modules.update`` was any module handed on; a dict display names
    # what it installs.
    fake = f"import sys\nimport types\n\n\ndef install():\n    fake = {FAKE_CORE}\n    {install}\n"
    plan = _handle_table_plan(repo, {"pkg/fake.py": fake})
    _handle_check(plan, "pkg.core" in install)


IMPL = "class Impl:\n    def a(self):\n        return 1\n"


EXT_FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "pkg/impl.py": IMPL,
    # Library code looking a name up on a module it imports by name.
    "pkg/lib.py": (
        "def call(n):\n    try:\n        import extlib_w17\n    except ImportError:\n"
        "        return None\n    return getattr(extlib_w17, n, None)\n"
    ),
    "tests/test_a.py": "import pkg.compat  # noqa: F401\n\n\ndef test_a():\n    pass\n",
    "tests/test_call.py": (
        "from pkg.lib import call\n\n\ndef test_call():\n    assert call('extra') is None\n"
    ),
}


CALLER = "tests/test_call.py::test_call"


@pytest.mark.parametrize(
    "compat, reached",
    [
        ("X = 1\n", False),
        ("sys.modules['extlib_w17'] = Impl()\n", True),
        ("sys.modules.setdefault('extlib_w17', Impl())\n", True),
        ("sys.modules.update({'extlib_w17': Impl()})\n", True),
        ("sys.modules |= {'extlib_w17': Impl()}\n", True),
    ],
    ids=["nothing", "assigned", "setdefault", "update", "or-assigned"],
)
@pytest.mark.parametrize("mode", ["static", "evidence"])
def test_w17_an_object_installed_as_an_external_module(repo, compat, reached, mode):
    # pkg.compat (imported by another test) installs a project object as
    # ``extlib_w17``: library lookups on that module find the project's code,
    # so an added method is seen there.
    if mode == "evidence" and sys.version_info < (3, 12):
        pytest.skip("needs sys.monitoring")
    code = f"import sys\n\nfrom pkg.impl import Impl\n\n{compat}"
    base = repo.commit({**EXT_FILES, "pkg/compat.py": code})
    ev = repo.collect() if mode == "evidence" else None
    head = repo.commit({"pkg/impl.py": IMPL + "\n    def extra(self):\n        return 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert (CALLER in selected(plan)) is reached


@pytest.mark.parametrize(
    "use, reached",
    [("return alias_w16.X", False), ("alias_w16.TABLE['k'] = 'b'", True)],
    ids=["read", "written"],
)
def test_w17_a_module_installed_under_a_literal_name_is_that_module(repo, use, reached):
    # Installing pkg.core as ``alias_w16`` hands it to nobody in particular:
    # what imports ``alias_w16`` gets pkg.core, and a write there is one to
    # pkg.core.
    files = {
        "pkg/alias.py": "import sys\n\nfrom pkg import core\n\nsys.modules['alias_w16'] = core\n",
        "pkg/user.py": f"def use():\n    import alias_w16\n\n    {use}\n",
    }
    _handle_check(_handle_table_plan(repo, files), reached)


POKES = {
    "a function's globals": (
        "from pkg.core import run\n\n\ndef poke():\n    run.__globals__['TABLE']['k'] = 'b'\n",
        True,
    ),
    "an unknown function's globals": (
        "def poke(f):\n    f.__globals__['TABLE']['k'] = 'b'\n",
        True,
    ),
    "globals handed on": ("def poke(f):\n    g = f.__globals__\n    return g\n", True),
    "globals read": (
        "from pkg.core import run\n\n\ndef poke():\n    return run.__globals__['TABLE']['k']\n",
        False,
    ),
    "a frame's globals": (
        "import sys\n\n\ndef poke():\n    sys._getframe(1).f_globals['TABLE']['k'] = 'b'\n",
        True,
    ),
    "gc's objects written": (
        "import gc\n\n\ndef poke():\n    for o in gc.get_objects():\n"
        "        if isinstance(o, dict) and o.get('k') == 'a':\n            o['k'] = 'b'\n",
        True,
    ),
    "gc's objects counted": (
        "import gc\n\n\ndef poke():\n    n = 0\n    for o in gc.get_objects():\n"
        "        n += len(type(o).__name__)\n    return len(gc.get_objects()) + n\n",
        False,
    ),
    "an unpickled table": (
        "import pickle\n\n\ndef poke():\n"
        "    t = pickle.loads(b'cpkg.core\\nTABLE\\n.')\n    t['k'] = 'b'\n",
        True,
    ),
    "a resolved name": (
        "import pkgutil\n\n\ndef poke():\n    pkgutil.resolve_name('pkg.core:TABLE')['k'] = 'b'\n",
        True,
    ),
    "a copy written": (
        "import importlib.util\n\n\ndef poke():\n"
        "    spec = importlib.util.find_spec('pkg.core')\n"
        "    m = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(m)\n"
        "    m.TABLE['k'] = 'b'\n"
        "    return m.run()\n",
        True,
    ),
    "a copy read": (
        "import importlib.util\n\n\ndef poke():\n"
        "    spec = importlib.util.find_spec('pkg.core')\n"
        "    m = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(m)\n"
        "    return m.run()\n",
        False,
    ),
}


@pytest.mark.parametrize("poke", sorted(POKES))
def test_w18_run_time_ways_to_a_module(repo, poke):
    code, reached = POKES[poke]
    test = "from pkg.core import run\nfrom pkg.poke import poke\n\n\ndef test_s():\n    run()\n"
    _handle_check(_handle_table_plan(repo, {"pkg/poke.py": code}, test), reached)


# strata's harness: a private loader copies sibling modules by file name and
# keeps them in module-level names its own code only reads.
LOADER = """\
import importlib.util
import sys
from pathlib import Path


def _load(filename, module_name):
    module_path = Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_copy = _load({filename}, "_w18_copy")


def use():
    {use}
"""


@pytest.mark.parametrize(
    "filename, use, other, reached",
    [
        ('"handlers.py"', "_copy.TABLE = {}", "", False),
        ('"core.py"', "return _copy.run()", "", False),
        ('"core.py"', "_copy.TABLE['k'] = 'b'", "", True),
        ('"core.py"', "return _copy", "", True),
        ('sys.argv[1] + ".py"', "return _copy.run()", "", True),
        (
            '"core.py"',
            "return _copy.run()",
            "from pkg import loader\n\n\ndef again():\n    return loader._load('x.py', 'x')\n",
            True,
        ),
        (
            '"core.py"',
            "return _copy.run()",
            "from pkg import loader\n\n\ndef clear():\n    loader._copy.TABLE.clear()\n",
            True,
        ),
    ],
    ids=[
        "another module",
        "read",
        "written",
        "handed on",
        "a name nothing bounds",
        "called elsewhere",
        "its holder reached",
    ],
)
def test_w18_a_module_copy_is_the_module_its_file_is(repo, filename, use, other, reached):
    # ``module_from_spec`` makes a copy of the module the spec's file is; its
    # code is that module's, so what is written to the copy changes what that
    # module's code reads. The file is bounded by literal names relative to
    # the loader's own file, also through a private function's parameter
    # every call passes a literal for, unless other code calls it; the copy
    # is held where only this module reads it, unless other code reaches it.
    files = {"pkg/loader.py": LOADER.format(filename=filename, use=use)}
    if other:
        files["pkg/other.py"] = other
    _handle_check(_handle_table_plan(repo, files), reached)


# A test and a benchmark on the code under test, and a benchmark on nothing
# that changes: a plan selecting all three selected everything.
TARGETS = [
    py_target("tests/test_use.py::test_use", "tests.test_use.test_use"),
    asv_target("bench_use.time_use", "benchmarks.bench_use.time_use"),
    asv_target("bench_other.time_other", "benchmarks.bench_other.time_other"),
]


AFFECTED = {"tests/test_use.py::test_use", "bench_use.time_use"}


RUNNERS = {
    "tests/test_use.py": "from pkg.use import f\n\n\ndef test_use():\n    assert f()\n",
    "benchmarks/bench_use.py": "from pkg.use import f\n\n\ndef time_use():\n    f()\n",
    "benchmarks/bench_other.py": "def time_other():\n    pass\n",
}


# Q2: a dynamic reference that imports or runs code named at run time can
# reach any module; static planning read that off the word "import" in its
# detail, which ``runpy.run_module``, ``runpy.run_path`` and ``python -m
# <parameter>`` do not have, and bounded them by their module's imports.

RUNPY = "import os\nimport runpy\n\n\ndef f():\n"


DYNAMIC_RUNS = {
    "run_module": RUNPY + "    return runpy.run_module(os.environ['M'])\n",
    "run_module discarded": RUNPY + "    runpy.run_module(os.environ['M'])\n    return 1\n",
    "run_path": RUNPY + "    return runpy.run_path(os.environ['P'])\n",
    "python -m parameter": (
        "import os\nimport subprocess\nimport sys\n\n\n"
        "def launch(name):\n    subprocess.run([sys.executable, '-m', name])\n\n\n"
        "def f():\n    launch(os.environ['M'])\n    return 1\n"
    ),
}


@pytest.mark.parametrize("case", list(DYNAMIC_RUNS))
def test_q2_dynamic_run_of_a_module_named_at_run_time_is_unbounded(repo, case):
    """``f`` imports or runs a module named at run time, so a change in a
    module nothing it imports reaches (``pkg.other``) may run under it."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/use.py": DYNAMIC_RUNS[case],
            "pkg/other.py": "def g():\n    return 1\n",
            **RUNNERS,
        }
    )
    head = repo.commit({"pkg/other.py": "def g():\n    return 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["."])
    assert selected(plan) == AFFECTED
    assert rules(plan, "tests/test_use.py::test_use") == {"dynamic_reference"}
    why = reason(plan, "tests/test_use.py::test_use", "dynamic_reference")
    assert "named at runtime" in why.detail


def test_q2_one_classifier_of_dynamic_details():
    """Every detail the indexer gives a dynamic reference that imports or
    runs code named at run time reads as one; a lookup does not."""
    from diffcone.model import (
        DISCARDED_IMPORT,
        RUNS_PROGRAM,
        RUNS_SCRIPT,
        imports_anything,
    )

    for detail in (
        "importlib.import_module(<non-literal>)",
        "__import__(<non-literal>)" + DISCARDED_IMPORT,
        "runpy.run_module(<non-literal>)",
        "runpy.run_module(<non-literal>)" + DISCARDED_IMPORT,
        "runpy.run_path()",
        f"{RUNS_PROGRAM}: an import of anything",
        f"{RUNS_PROGRAM} (python -m <parameter>)",
        f"{RUNS_SCRIPT}scripts/x.py, a Python file no module name maps to",
        "exec() of code read at run time: an import of anything",
        "pickle.loads(): an import of what the data names",
    ):
        assert imports_anything(detail), detail
    for detail in ("getattr(<non-literal>)", "eval()", "exec()", "globals()", "vars()"):
        assert not imports_anything(detail), detail


# A benchmark on code no scenario changes: a plan that selects it selected
# everything.
QUIET_BENCH = {
    "pkg/__init__.py": "",
    "pkg/quiet.py": "def idle():\n    return 0\n",
    "benchmarks/bench_quiet.py": "from pkg.quiet import idle\n\n\ndef time_idle():\n    idle()\n",
}


QUIET_TARGET = asv_target("bench_quiet.time_idle", "benchmarks.bench_quiet.time_idle")


def _quiet_plan(repo: FixtureRepo, base: dict, head: dict, *tests: str):
    """Plan ``head`` against ``base`` with a pytest target per test id
    (``tests/test_m.py::test_x``) and the benchmark."""
    targets = [py_target(t, t.replace("/", ".").replace(".py::", ".")) for t in tests] + [
        QUIET_TARGET
    ]
    first = repo.commit({**QUIET_BENCH, **base})
    second = repo.commit(head)
    return repo.plan(first, second, targets)


# Q11: a caller the index cannot see (a call through a receiver of unknown
# type, or a construction of an escaping class) may pass anything: every rule
# that bounds a parameter by what the call sites pass must give up then.

ENV_B = 'import os\nos.environ["DC_X"] = "{v}"\n'


def test_q11_dynamic_import_called_through_an_unknown_receiver(repo):
    util = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
    user = (
        "from pkg import util\n\n\ndef direct():\n    return util.load('pkg.a')\n\n\n"
        "class Runner:\n    def __init__(self, loader):\n        self.loader = loader\n\n"
        "    def go(self):\n        return self.loader.load('pkg.b')\n"
    )
    test = (
        "import os\nfrom pkg import util\nfrom pkg.user import Runner\n\n\n"
        "def test_go():\n    Runner(util).go()\n    assert os.environ['DC_X'] == '1'\n"
    )
    plan = _quiet_plan(
        repo,
        {
            "pkg/util.py": util,
            "pkg/user.py": user,
            "pkg/a.py": "",
            "pkg/b.py": ENV_B.format(v=1),
            "tests/test_m.py": test,
        },
        {"pkg/b.py": ENV_B.format(v=2)},
        "tests/test_m.py::test_go",
    )
    assert selected(plan) == {"tests/test_m.py::test_go"}


def test_q11_dynamic_import_in_the_init_of_an_escaping_class(repo):
    loader = (
        "import importlib\n\n\nclass Loader:\n    def __init__(self, name):\n"
        "        importlib.import_module(name)\n\n\n"
        "def direct():\n    return Loader('pkg.a')\n\n\n"
        "def factory():\n    return Loader\n"
    )
    test = (
        "import os\nfrom pkg.loader import factory\n\n\n"
        "def test_make():\n    factory()('pkg.b')\n    assert os.environ['DC_X'] == '1'\n"
    )
    plan = _quiet_plan(
        repo,
        {
            "pkg/loader.py": loader,
            "pkg/a.py": "",
            "pkg/b.py": ENV_B.format(v=1),
            "tests/test_m.py": test,
        },
        {"pkg/b.py": ENV_B.format(v=2)},
        "tests/test_m.py::test_make",
    )
    assert selected(plan) == {"tests/test_m.py::test_make"}


def test_q11_receiver_classes_with_a_caller_through_an_unknown_receiver(repo):
    inv = "def invoke(obj, name):\n    return getattr(obj, name)()\n"
    prov = (
        "class Provider:\n    def ping(self):\n        return 1\n\n\n"
        "class Other:\n    def pong(self):\n        return {v}\n"
    )
    user = (
        "from pkg.inv import invoke\nfrom pkg.prov import Provider\n\n\n"
        "def direct(name):\n    return invoke(Provider(), name)\n"
    )
    test = (
        "from pkg import inv\nfrom pkg.prov import Other\n\n\n"
        "def test_pong():\n    holder = [inv][0]\n"
        "    assert holder.invoke(Other(), 'pong') == 1\n"
    )
    plan = _quiet_plan(
        repo,
        {
            "pkg/inv.py": inv,
            "pkg/prov.py": prov.format(v=1),
            "pkg/user.py": user,
            "tests/test_m.py": test,
        },
        {"pkg/prov.py": prov.format(v=2)},
        "tests/test_m.py::test_pong",
    )
    assert selected(plan) == {"tests/test_m.py::test_pong"}


WORK = "def job():\n    return 1\n"


BOX = "\n\nclass Box:\n    pass\n"


OTHER_SETSTATE = "class Other:\n    def __setstate__(self, state):\n        self.x = 1\n"


def _w22_plan(repo, use: str):
    """``pkg.work.job`` changes; ``test_use`` calls ``pkg.use.run``, which
    obtains a function at run time (``use``) and calls it."""
    files = {
        "pkg/__init__.py": "",
        "pkg/work.py": WORK,
        "pkg/use.py": use,
        "tests/__init__.py": "",
        "tests/test_use.py": (
            "from pkg.use import run\n\n\ndef test_use(tmp_path):\n    run(tmp_path)\n"
        ),
        "tests/test_other.py": "def test_other():\n    pass\n",
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/work.py": WORK.replace("1", "2")})
    targets = [
        py_target("t::test_use", "tests.test_use.test_use"),
        py_target("t::test_other", "tests.test_other.test_other"),
        asv_target("bench.run", "pkg.use.run"),
    ]
    return repo.plan(base, head, targets)


@pytest.mark.parametrize(
    "use",
    [
        "import pickle\n\n\ndef run(d):\n    return pickle.loads((d / 'f').read_bytes())()\n",
        "import pickle\n\n\ndef run(d):\n    with open(d / 'f', 'rb') as f:\n"
        "        return pickle.load(f)()\n",
        "import pickle\n\n\ndef run(d):\n    with open(d / 'f', 'rb') as f:\n"
        "        return pickle.Unpickler(f).load()()\n",
        "import importlib.util\n\n\ndef run(d):\n"
        "    spec = importlib.util.spec_from_file_location('m', d / 'm.py')\n"
        "    m = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(m)\n"
        "    return m.entry()\n",
        "import importlib.util\n\n\ndef run(d):\n"
        "    spec = importlib.util.spec_from_file_location('m', d / 'm.py')\n"
        "    m = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(m)\n"
        "    f = m.entry\n    return f()\n",
    ],
    ids=["loads", "load", "unpickler", "spec", "spec-held"],
)
def test_w22_a_function_obtained_at_run_time_is_any(repo, use):
    """What an unpickler returns may be any module's function by reference;
    a module loaded from a path nothing bounds may run (or hand back) any
    code: both are imports by a name nothing bounds."""
    plan = _w22_plan(repo, use)
    assert selected(plan) == {"t::test_use", "bench.run"}
    assert rules(plan, "t::test_use") == {"dynamic_reference"}


# ``pkg.work.job`` by reference (protocol 0: a GLOBAL opcode).
PICKLED_JOB = b"cpkg.work\njob\np0\n."


@pytest.mark.parametrize(
    ("use", "change", "reached"),
    [
        # a round trip loads what was dumped: only pickling hooks matter
        (
            "import pickle\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    return pickle.loads(pickle.dumps(Box()))\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            False,
        ),
        (
            "import pickle\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    data = pickle.dumps(Box())\n    return pickle.loads(data)\n",
            {"pkg/other.py": OTHER_SETSTATE.replace("1", "2")},
            True,
        ),
        # through a new temporary file the function dumps into
        (
            "import pickle\nimport tempfile\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    with tempfile.TemporaryFile() as f:\n"
            "        pickle.dump(Box(), f)\n        f.seek(0)\n        return pickle.load(f)\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            False,
        ),
        # a file that may hold anything
        (
            "import pickle\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    with open(d / 'f', 'rb') as f:\n"
            "        pickle.dump(Box(), f)\n        return pickle.load(f)\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            True,
        ),
        # bytes written out name what they load
        (
            f"import pickle\n\nDATA = {PICKLED_JOB!r}\n\n\n"
            "def run(d):\n    return pickle.loads(DATA)()\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            True,
        ),
        (
            f"import pickle\n\nDATA = {PICKLED_JOB!r}\n\n\n"
            "def run(d):\n    return pickle.loads(DATA)()\n",
            {"pkg/other.py": OTHER_SETSTATE.replace("1", "2")},
            False,
        ),
    ],
    ids=[
        "round-trip-other",
        "round-trip-hook",
        "temporary-file",
        "any-file",
        "bytes-named",
        "bytes-other",
    ],
)
def test_w22_what_an_unpickler_loads_when_the_code_says(repo, use, change, reached):
    files = {
        "pkg/__init__.py": "",
        "pkg/work.py": WORK + BOX,
        "pkg/other.py": OTHER_SETSTATE,
        "pkg/use.py": use,
        "tests/__init__.py": "",
        "tests/test_use.py": (
            "from pkg.use import run\n\n\ndef test_use(tmp_path):\n    run(tmp_path)\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit(change)
    targets = [
        py_target("t::test_use", "tests.test_use.test_use"),
        asv_target("bench.run", "pkg.use.run"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == ({"t::test_use", "bench.run"} if reached else set())


@pytest.mark.parametrize("protocol", [0, 2, 4, 5])
def test_w22_the_globals_a_pickle_names(protocol):
    import collections
    import pickle

    from diffcone.indexer.references import _pickled_globals

    data = pickle.dumps(collections.OrderedDict, protocol=protocol)
    assert _pickled_globals(data) == [("collections", "OrderedDict")]
    assert _pickled_globals(b"not a pickle") is None


def test_w22_a_loader_bounded_by_a_literal_prefix(repo):
    """``spec_from_file_location`` of a path under ``plugins/``: the
    modules there, not ``pkg.work``."""
    use = (
        "import importlib.util\n\n\ndef run(d, name='a'):\n"
        "    spec = importlib.util.spec_from_file_location(name, f'plugins/{name}.py')\n"
        "    m = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(m)\n"
        "    return m.entry()\n"
    )
    files = {"plugins/__init__.py": "", "plugins/a.py": "def entry():\n    return 1\n"}
    plan = _w22_plan(repo, use)
    assert selected(plan) == set()
    base = repo.commit(files)
    head = repo.commit({"plugins/a.py": "def entry():\n    return 2\n"})
    targets = [py_target("t::test_use", "tests.test_use.test_use")]
    assert selected(repo.plan(base, head, targets)) == {"t::test_use"}


# --------------------------------------------------------------------------- unresolved calls
# A call by a name nothing resolves.


# Q1: an edge's detail is a mark (registers, rebinds, writes, mutated_by,
# an install) only on a ``references`` edge. A name match carries the
# unresolved name as its detail, so a bare call of a function named like a
# mark was planned as that mark.


@pytest.mark.parametrize("name", ["rebinds", "registers", "writes", "mutated_by"])
def test_q1_bare_call_named_like_a_mark_is_a_call(repo, name):
    """``f`` calls ``<name>()``, which nothing resolves, so it may be
    ``pkg.lib.<name>``, whose body changes. As ``rebinds`` the name match
    carried only a deletion: test and benchmark were missed. (``registers``,
    ``writes`` and ``mutated_by`` are guards here: from a function the call
    was planned right by luck.)"""
    lib = f"def {name}():\n    return 1\n"
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": lib,
            "pkg/use.py": f"def f():\n    return {name}()\n",
            **RUNNERS,
        }
    )
    head = repo.commit({"pkg/lib.py": lib.replace("1", "2")})
    plan = repo.plan(base, head, TARGETS, source_roots=["."])
    assert selected(plan) == AFFECTED
    assert rules(plan, "tests/test_use.py::test_use") == {"unresolved_name_match"}
    why = reason(plan, "tests/test_use.py::test_use", "unresolved_name_match")
    assert why.changed_symbol == f"pkg.lib.{name}"


@pytest.mark.parametrize("name", ["registers", "rebinds", "writes", "mutated_by"])
def test_q1_module_level_call_named_like_a_mark_runs_at_import(repo, name):
    """``pkg.use`` calls ``<name>()`` at import; ``pkg.lib.<name>`` changes,
    so importing ``pkg.use`` runs different code. As ``registers`` the
    module's call was taken for a registration, which runs nothing new at
    import, and as ``rebinds`` for a rebinding: both missed. (``writes``
    and ``mutated_by`` are guards here.)"""
    lib = f"def {name}():\n    return 1\n"
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": lib,
            "pkg/use.py": (
                f"if {name}() != 1:\n    raise RuntimeError\n\n\ndef f():\n    return 1\n"
            ),
            **RUNNERS,
        }
    )
    head = repo.commit({"pkg/lib.py": lib.replace("1", "2")})
    plan = repo.plan(base, head, TARGETS, source_roots=["."])
    assert selected(plan) == AFFECTED
    assert rules(plan, "bench_use.time_use") == {"unresolved_name_match"}


# What the tests read: MODE, which ``record`` fills in place.
STATE = {
    "pkg/__init__.py": "",
    "pkg/state.py": (
        "MODE = []\n\n\ndef record(value):\n    MODE.append(value)\n\n\n"
        "def current():\n    return list(MODE)\n"
    ),
    "tests/test_mode.py": "from pkg.state import current\n\n\ndef test_mode():\n    current()\n",
    "benchmarks/bench_mode.py": (
        "from pkg.state import current\n\n\ndef time_mode():\n    current()\n"
    ),
    "benchmarks/bench_other.py": "def time_other():\n    pass\n",
}


STATE_TARGETS = [
    py_target("tests/test_mode.py::test_mode", "tests.test_mode.test_mode"),
    asv_target("bench_mode.time_mode", "benchmarks.bench_mode.time_mode"),
    asv_target("bench_other.time_other", "benchmarks.bench_other.time_other"),
]


def test_q1_import_time_call_named_mutated_by_reaches_the_state(repo):
    """``pkg.config`` calls ``mutated_by("slow")`` at import, a name nothing
    resolves that may be ``pkg.state.mutated_by``, which writes ``MODE`` in
    place: ``"slow"`` -> ``"fast"`` changes what ``MODE`` holds. The name
    match (detail ``mutated_by``) was taken for a write in place, no call,
    so the readers of ``MODE`` were missed."""
    state = STATE["pkg/state.py"].replace("def record(", "def mutated_by(")
    config = 'mutated_by("slow")\n'
    base = repo.commit({**STATE, "pkg/state.py": state, "pkg/config.py": config})
    head = repo.commit({"pkg/config.py": config.replace("slow", "fast")})
    plan = repo.plan(base, head, STATE_TARGETS, source_roots=["."])
    assert selected(plan) == {"tests/test_mode.py::test_mode", "bench_mode.time_mode"}
    assert rules(plan, "tests/test_mode.py::test_mode") == {"unresolved_name_match"}


def test_q1_caller_of_a_writer_named_mutated_by_reaches_the_state(repo):
    """``pkg.config.setup`` calls ``mutated_by("slow")`` (unresolved, may be
    the writer of ``MODE``) and changes its argument: whatever runs it
    leaves ``MODE`` holding something else. The name match was no call, so
    ``calls:`` of the writer did not depend on ``setup``: missed."""
    state = STATE["pkg/state.py"].replace("def record(", "def mutated_by(")
    config = 'def setup():\n    mutated_by("slow")\n'
    base = repo.commit({**STATE, "pkg/state.py": state, "pkg/config.py": config})
    head = repo.commit({"pkg/config.py": config.replace("slow", "fast")})
    plan = repo.plan(base, head, STATE_TARGETS, source_roots=["."])
    assert selected(plan) == {"tests/test_mode.py::test_mode", "bench_mode.time_mode"}


# --------------------------------------------------------------------------- programs and text
# Programs and code text a test runs.


# S6: code read from a file at run time can reach anything.


def test_s6_exec_of_a_files_text_reaches_that_file(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/helpers.py": "def h():\n    return 1\n",
            "pkg/m.py": (
                "import os\n\nns = {}\nhere = os.path.dirname(__file__)\n"
                "exec(open(os.path.join(here, 'helpers.py')).read(), ns)\n"
                "h = ns['h']\n"
            ),
            "pkg/other.py": "def g():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "from pkg.m import h\n\n\ndef test_h():\n    assert h() == 1\n",
            "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    g()\n",
        }
    )
    head = repo.commit({"pkg/helpers.py": "def h():\n    return 22\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_m.py::test_h" in selected(plan)
    assert "tests/test_other.py::test_g" in unselected(plan)


# ``m.__file__`` in a command runs the file of whichever module ``m`` is; on
# a name bound several ways only the first binding's module was an import
# of the code running it. (Latent: the bindings' own edges reached the same
# modules in every scenario tried.)


@pytest.mark.parametrize(
    ("files", "expr"),
    [
        (
            {
                "pkg/__init__.py": "",
                "pkg/fast.py": "",
                "pkg/slow.py": "",
                "tests/test_a.py": (
                    "try:\n    import pkg.fast as tool\nexcept ImportError:\n"
                    "    import pkg.slow as tool\n"
                ),
            },
            "tool",
        ),
        (
            {
                "pkg/__init__.py": "from pkg.other import fast\n",
                "pkg/other.py": "def fast():\n    pass\n",
                "pkg/fast.py": "",
                "tests/test_a.py": "import pkg\n",
            },
            "pkg.fast",
        ),
    ],
    ids=["alt_import", "submodule_of_the_same_name"],
)
def test_l3_a_module_file_run_by_name_counts_every_binding(files, expr):
    files = {**files, "tests/__init__.py": ""}
    files["tests/test_a.py"] = (
        f"import subprocess\nimport sys\n\n{files['tests/test_a.py']}\n\ndef test_a():\n"
        f"    subprocess.run([sys.executable, {expr}.__file__])\n"
    )
    snapshot = Snapshot(
        info=SnapshotInfo(revision="x", commit="x"),
        source_roots=["."],
        files={k: v.encode() for k, v in files.items()},
    )
    edges = build_index(snapshot).edges
    imported = {
        e.target for e in edges if e.source == "tests.test_a.test_a" and e.kind == EdgeKind.IMPORTS
    }
    assert "pkg.fast" in imported


SCRIPT_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/other.py": "def g():\n    return 3\n",
    "tests/__init__.py": "",
    "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    assert g() == 3\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.other import g\n\n\ndef time_g():\n    g()\n",
}


SCRIPT_BENCH = asv_target("bench.time_g", "benchmarks.bench.time_g")


OTHER_TEST = "tests/test_other.py::test_g"


def _static(repo, files, change):
    base = repo.commit({**SCRIPT_BASE, **files})
    head = repo.commit(change)
    plan = repo.plan(base, head, [SCRIPT_BENCH], discover_runners=["pytest"])
    chosen = selected(plan)
    assert OTHER_TEST not in chosen and SCRIPT_BENCH.runner_id not in chosen, [
        (r.rule, r.detail, [(s.source, s.target, s.kind) for s in r.path])
        for d in plan.selected
        if d.target.runner_id == OTHER_TEST
        for r in d.reasons
    ]
    return chosen


# A script that imports project code, and the module it imports.
GEN = "from pkg.core import value\n\nprint(value())\n"


SCRIPT_CORE = {"pkg/core.py": "def value():\n    return 1\n"}


SCRIPT_RUNNERS = {
    "relative path": "subprocess.run([sys.executable, 'scripts/gen.py'], check=True)",
    "os.path.join": (
        "subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'gen.py')], check=True)"
    ),
    "Path division": (
        "subprocess.run([sys.executable, str(pathlib.Path(ROOT) / 'scripts' / 'gen.py')])"
    ),
    "Path(__file__)": (
        "subprocess.run([sys.executable, pathlib.Path(__file__).parents[1] / 'scripts/gen.py'])"
    ),
    "f-string": "subprocess.run([sys.executable, f'{ROOT}/scripts/gen.py'])",
    "-m": "subprocess.run([sys.executable, '-m', 'scripts.gen'], check=True)",
    "-m of a package": "subprocess.run([sys.executable, '-u', '-m', 'scripts'], check=True)",
    "-c": "subprocess.run([sys.executable, '-c', 'import scripts.gen'], check=True)",
    "-c through a local": (
        "code = textwrap.dedent('''\n        import scripts.gen\n    ''')\n"
        "    subprocess.run([sys.executable, '-c', code], check=True)"
    ),
    "coverage run": (
        "subprocess.run([sys.executable, '-m', 'coverage', 'run', '-m', 'scripts.gen'])"
    ),
    "shell string": "subprocess.run(f'{sys.executable} -m scripts.gen', shell=True)",
    "-m under a prefix": (
        "subprocess.run(f'{sys.executable} -m scripts.{os.getenv(\"N\")}', shell=True)"
    ),
    "os.system": "os.system('python scripts/gen.py')",
    "a helper's literal": "run_python('scripts/gen.py')",
}


def _runner_test(call: str) -> str:
    return (
        "import os\nimport pathlib\nimport subprocess\nimport sys\nimport textwrap\n\n"
        "from tests.helpers import run_python\n\n"
        "ROOT = os.path.dirname(os.path.dirname(__file__))\n\n\n"
        f"def test_script():\n    {call}\n"
    )


# A helper in test code that runs the script it is handed by path: the
# program is a parameter (any program), and the caller's literal names it.
HELPERS = (
    "import subprocess\nimport sys\n\n\n"
    "def run_python(script):\n    subprocess.run([sys.executable, script], check=True)\n"
)


@pytest.mark.parametrize("change", ["script", "imported module"])
@pytest.mark.parametrize("how", list(SCRIPT_RUNNERS))
def test_w3_a_test_running_a_script_depends_on_it(repo, how, change):
    files = {
        **SCRIPT_CORE,
        "scripts/__init__.py": "",
        "scripts/__main__.py": "import scripts.gen  # noqa: F401\n",
        "scripts/gen.py": GEN,
        "tests/helpers.py": HELPERS,
        "tests/test_script.py": _runner_test(SCRIPT_RUNNERS[how]),
    }
    edit = (
        {"scripts/gen.py": GEN.replace("value()", "value() + 1")}
        if change == "script"
        else {"pkg/core.py": "def value():\n    return 2\n"}
    )
    assert _static(repo, files, edit) == {"tests/test_script.py::test_script"}


DYNAMIC_SCRIPT = "import importlib\n\nimportlib.import_module('pkg.' + 'core').value()\n"


@pytest.mark.parametrize("script", [GEN, DYNAMIC_SCRIPT], ids=["imports", "imports by name"])
def test_w3_a_script_no_module_name_maps_to(repo, script):
    """``scripts/gen-data.py`` is a file the index does not read: a test
    running it sees a change to it, and what the script reaches through its
    imports (any change, when it imports by a name it computes); a script
    nothing names stays unseen (a conftest pytest never loads,
    ``test_a_conftest_pytest_does_not_load_is_not_read``)."""
    files = {
        **SCRIPT_CORE,
        "scripts/gen-data.py": script,
        "scripts/unused-tool.py": script,
        "tests/test_script.py": _runner_test(SCRIPT_RUNNERS["relative path"]).replace(
            "gen.py", "gen-data.py"
        ),
        "tests/helpers.py": HELPERS,
    }
    repo.commit({**SCRIPT_BASE, **files})

    def plan_change(change):
        return repo.plan(
            repo.git("rev-parse", "HEAD").strip(),
            repo.commit(change),
            [SCRIPT_BENCH],
            discover_runners=["pytest"],
        )

    plan = plan_change({"scripts/gen-data.py": script + "\n# changed\nX = 1\n"})
    assert selected(plan) == {"tests/test_script.py::test_script"}
    (why,) = [r for d in plan.selected for r in d.reasons]
    assert why.rule == "unanalysed_file_changed" and "scripts/gen-data.py" in why.detail
    assert selected(plan_change({"scripts/unused-tool.py": script + "X = 2\n"})) == set()
    core = plan_change({"pkg/core.py": "def value():\n    return 2\n"})
    assert selected(core) == {"tests/test_script.py::test_script"}
    elsewhere = selected(plan_change({"pkg/other.py": "def g():\n    return 4\n"}))
    assert elsewhere - {OTHER_TEST, SCRIPT_BENCH.runner_id} == (
        set() if script == GEN else {"tests/test_script.py::test_script"}
    )


DYNAMIC = {
    "a script path from elsewhere": "subprocess.run([sys.executable, os.environ['SCRIPT']])",
    "a module from elsewhere": "subprocess.run([sys.executable, '-m', os.environ['MOD']])",
    # ``'import ' + NAME`` is read as the program it is since audit round 5
    # (R12), so the name must come from elsewhere to be built at run time.
    "code built at run time": (
        "subprocess.run([sys.executable, '-c', 'import ' + os.environ['MOD']])"
    ),
    "standard input": "subprocess.run([sys.executable, '-'], input=CODE)",
    "starred arguments": "subprocess.run([sys.executable, *ARGS])",
    "pytest on a directory": "subprocess.run([sys.executable, '-m', 'pytest', 'tests/sub'])",
    "a command line": "subprocess.run(f'{sys.executable} -c \"import {NAME}\"', shell=True)",
    "a concatenated list": "subprocess.run([sys.executable] + ARGS)",
}


@pytest.mark.parametrize("how", list(DYNAMIC))
def test_w3_a_python_program_built_at_run_time_can_run_anything(repo, how):
    test = _runner_test(DYNAMIC[how]).replace(
        "ROOT =", "NAME = 'pkg.core'\nCODE = b'import pkg.core'\nARGS = ['scripts/gen.py']\nROOT ="
    )
    files = {**SCRIPT_CORE, "tests/helpers.py": HELPERS, "tests/test_script.py": test}
    assert _static(repo, files, {"pkg/core.py": "def value():\n    return 2\n"}) == {
        "tests/test_script.py::test_script"
    }


PRECISE = {
    "-c code importing nothing of ours": "subprocess.run([sys.executable, '-c', 'print(1)'])",
    "--version": "subprocess.run([sys.executable, '--version'])",
    "another module": "subprocess.run([sys.executable, '-m', 'venv', str(ROOT)])",
    "a command line": "subprocess.run(f'{sys.executable} -c \"import sys; print(1)\"')",
    "a git command": "subprocess.run(['git', 'commit', '-m', MESSAGE])",
    "interpreter paths": "assert [p for p in (sys.executable, str(sys.executable))]",
    "a list of words": "assert {'python'} <= set(['python', 'recursion', MESSAGE])",
    "a docstring naming a file": "'''Like scripts/gen.py and python -m scripts.gen.'''",
}


@pytest.mark.parametrize("how", list(PRECISE))
def test_w3_a_python_program_that_runs_nothing_of_ours(repo, how):
    test = _runner_test(PRECISE[how]).replace("ROOT =", "MESSAGE = 'x'\nROOT =")
    files = {
        **SCRIPT_CORE,
        "scripts/__init__.py": "",
        "scripts/gen.py": GEN,
        "tests/helpers.py": HELPERS,
        "tests/test_script.py": test,
    }
    assert _static(repo, files, {"pkg/core.py": "def value():\n    return 2\n"}) == set()


BOUNDED = {
    "a helper's literal": "run_python('scripts/gen.py')",
    "a local path": (
        "path = pathlib.Path(__file__).parents[1] / 'scripts' / 'gen.py'\n"
        "    subprocess.run([sys.executable, str(path)])"
    ),
    "a module's __file__": (
        "subprocess.run([sys.executable, '-m', 'coverage', 'run', MOD.__file__])"
    ),
}


@pytest.mark.parametrize("how", list(BOUNDED))
def test_w3_a_named_program_is_not_any_program(repo, how):
    """The program is named (through a helper's parameter, a local, a
    module's ``__file__``): a change elsewhere does not select the test."""
    test = _runner_test(BOUNDED[how]).replace("ROOT =", "import scripts.gen as MOD\n\nROOT =")
    files = {
        **SCRIPT_CORE,
        "scripts/__init__.py": "",
        "scripts/gen.py": GEN,
        "tests/helpers.py": HELPERS,
        "tests/test_script.py": test,
    }
    base = repo.commit({**SCRIPT_BASE, **files})
    head = repo.commit({"pkg/other.py": "def g():\n    return 4\n"})
    plan = repo.plan(base, head, [SCRIPT_BENCH], discover_runners=["pytest"])
    assert selected(plan) == {OTHER_TEST, SCRIPT_BENCH.runner_id}


NOOP = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")


Q21_BASE = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def value():\n    return 1\n",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}


Q21_TEST = (
    "import subprocess\nimport sys\n\n\ndef test_cli():\n"
    "    subprocess.run([sys.executable, '-c', {code!r}], check=True)\n"
)


CLI = py_target("tests/test_cli.py::test_cli", "tests.test_cli.test_cli")


@pytest.mark.parametrize(
    "change",
    [
        "sys.path[:0] = ['pkg']",
        "sys.path += ['pkg']",
        "sys.path = ['pkg'] + sys.path",
        "del sys.path[0]",
        "sys.path.remove('')",
        "sys.meta_path.append(None)",
        "sys.modules['core'] = None",
        "p = sys.path; p[:0] = ['pkg']",
        "p = sys.path; p += ['pkg']",
        "import os; os.chdir('pkg')",
        "setattr(sys, 'path', ['pkg'] + sys.path)",
    ],
)
def test_q21_dash_c_code_changing_the_import_system_runs_anything(repo, change):
    """``-c`` code that redirects its imports (here ``import core`` finds
    ``pkg/core.py`` once ``pkg`` is on the path) can import anything, as
    with ``sys.path.insert``: a change anywhere selects the test."""
    code = f"import sys; {change}; import core; core.value()"
    base = repo.commit({**Q21_BASE, "tests/test_cli.py": Q21_TEST.format(code=code)})
    head = repo.commit({"pkg/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [CLI, NOOP], source_roots=["."])
    assert selected(plan) == {CLI.runner_id}


def test_q21_dash_c_code_through_an_imported_path(repo):
    """``from sys import path`` then ``path[:0] = ...``: the same list."""
    code = "from sys import path; path[:0] = ['pkg']; import core; core.value()"
    base = repo.commit({**Q21_BASE, "tests/test_cli.py": Q21_TEST.format(code=code)})
    head = repo.commit({"pkg/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [CLI, NOOP], source_roots=["."])
    assert selected(plan) == {CLI.runner_id}


def test_q21_dash_c_code_leaving_the_path_alone_stays_bounded(repo):
    """``-c`` code that binds a local named ``path`` and imports nothing of
    the project stays bounded: the change does not select it."""
    code = "import os; path = os.getcwd(); path += '/x'; print(path)"
    base = repo.commit({**Q21_BASE, "tests/test_cli.py": Q21_TEST.format(code=code)})
    head = repo.commit({"pkg/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [CLI, NOOP], source_roots=["."])
    assert selected(plan) == set()


BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}


OPS = "def value():\n    return 1\n\n\ndef other():\n    return 10\n"


OPS_VALUE = OPS.replace("return 1\n", "return 2\n")


OPS_OTHER = OPS.replace("return 10\n", "return 11\n")


CONTROL = "def test_c():\n    assert True\n"


T_A = py_target("tests/test_a.py::test_a", "tests.test_a.test_a")


T_C = py_target("tests/test_c.py::test_c", "tests.test_c.test_c")


def _plan(repo, base, head, *targets, **kwargs):
    return repo.plan(base, head, [*targets, NOOP], source_roots=["."], **kwargs)


CHILD = {
    "pkg/work.py": "def handle(x):\n    return x + 1\n",
    "pkg/tool.py": (
        'from pkg.work import handle\n\nif __name__ == "__main__":\n    print(handle(1))\n'
    ),
}


HANDLED = "def handle(x):\n    return x + 2\n"


CHILD_TEST = (
    "import asyncio\nimport os\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n"
    "LAUNCHER = Path('/opt/uv')\n\n\ndef test_a():\n    {call}\n"
)


PYTHON_TOOL = '"-m", "pkg.tool"'


@pytest.mark.parametrize(
    "call",
    [
        f'subprocess.run(["env", "X=1", sys.executable, {PYTHON_TOOL}])',
        f'subprocess.run([str(LAUNCHER), "run", sys.executable, {PYTHON_TOOL}])',
        'subprocess.run(["timeout", "30", sys.executable, "pkg/tool.py"])',
        'subprocess.run(["env", "OMPI=1", "--", sys.executable, "pkg/tool.py"])',
        f'subprocess.run([os.environ.get("PYTHON", sys.executable), {PYTHON_TOOL}])',
        f"asyncio.run(asyncio.create_subprocess_exec(sys.executable, {PYTHON_TOOL}))",
        f"os.execl(sys.executable, sys.executable, {PYTHON_TOOL})",
        'subprocess.run("X=1 " + sys.executable + " -m pkg.tool", shell=True)',
        'subprocess.run(f"X=1 {sys.executable} -m pkg.tool", shell=True)',
        'subprocess.run("env X=1 python -m pkg.tool", shell=True)',
    ],
    ids=[
        "env",
        "launcher-in-a-name",
        "timeout",
        "double-dash",
        "environ-get",
        "create_subprocess_exec",
        "execl",
        "concatenated-shell-line",
        "f-string-shell-line",
        "shell-line-with-env",
    ],
)
def test_r8_the_interpreter_need_not_be_the_first_item(repo, call):
    """A command the test starts runs ``pkg.tool`` whatever stands before
    the interpreter; the program it runs is what changed."""
    base = repo.commit(
        {
            **BASE,
            **CHILD,
            "tests/test_a.py": CHILD_TEST.format(call=call),
            "tests/test_c.py": CONTROL,
        }
    )
    head = repo.commit({"pkg/work.py": HANDLED})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dependency"}


def test_r8_a_program_from_elsewhere_after_a_launcher_is_unbounded(repo):
    """``["env", "X=1", sys.executable, script]`` with a script nothing
    names: the test may run anything, as with the interpreter first."""
    call = 'subprocess.run(["env", "X=1", sys.executable, os.environ["SCRIPT"]])'
    base = repo.commit(
        {
            **BASE,
            **CHILD,
            "pkg/other.py": "def g():\n    return 1\n",
            "tests/test_a.py": CHILD_TEST.format(call=call),
            "tests/test_c.py": CONTROL,
        }
    )
    head = repo.commit({"pkg/other.py": "def g():\n    return 2\n"})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dynamic_reference"}


IMPORT_OPS = "from pkg import ops"


EXEC_TESTS = {
    "literal": f"def test_a():\n    ns = {{}}\n    exec('{IMPORT_OPS}\\nres = ops.value()', ns)\n",
    "template-format": (
        f"TEMPLATE = '{IMPORT_OPS}\\nres = ops.{{fn}}()'\n\n\n"
        "def test_a():\n    ns = {}\n    exec(TEMPLATE.format(fn='value'), ns)\n"
    ),
    "template-percent": (
        f"TEMPLATE = '{IMPORT_OPS}\\nres = ops.%s()'\n\n\n"
        "def test_a():\n    ns = {}\n    exec(TEMPLATE % 'value', ns)\n"
    ),
    "f-string-literal": (
        f"def test_a():\n    fn = 'value'\n    exec(f'{IMPORT_OPS}\\nres = ops.{{fn}}()')\n"
    ),
    "eval-dunder-import": (
        "def test_a():\n    assert eval('__import__(\"pkg.ops\").ops.value()') == 1\n"
    ),
    "compile-in-helper": (
        f"SRC = '{IMPORT_OPS}\\nres = ops.value()'\n\n\n"
        "def run_src(src):\n    ns = {}\n    exec(compile(src, '<t>', 'exec'), ns)\n"
        "    return ns\n\n\ndef test_a():\n    assert run_src(SRC)['res'] == 1\n"
    ),
    "parameter-fills-a-field": (
        "def make(body):\n    d = {}\n    exec(f'def fn():\\n    {body}', {}, d)\n"
        "    return d['fn']\n\n\n"
        f"def test_a():\n    assert make('{IMPORT_OPS}; return ops.value()')() == 1\n"
    ),
    "dedent": (
        "import textwrap\n\n\ndef test_a():\n    exec(textwrap.dedent('''\n"
        f"        {IMPORT_OPS}\n        res = ops.value()\n    '''))\n"
    ),
    "timeit": (
        "import timeit\n\n\ndef test_a():\n"
        f"    timeit.timeit('ops.value()', setup='{IMPORT_OPS}', number=1)\n"
    ),
    "interactive": (
        "import code\n\n\ndef test_a():\n    i = code.InteractiveInterpreter()\n"
        f"    i.runsource('{IMPORT_OPS}; ops.value()')\n"
    ),
}


@pytest.mark.parametrize("text", list(EXEC_TESTS.values()), ids=list(EXEC_TESTS))
def test_r12_text_written_out_is_read_as_the_code_it_is(repo, text):
    """Code a test runs as text imports ``pkg.ops`` and calls ``value``: a
    body change of ``value`` reaches the test, and nothing else does."""
    base = repo.commit(
        {**BASE, "pkg/ops.py": OPS, "tests/test_a.py": text, "tests/test_c.py": CONTROL}
    )
    head = repo.commit({"pkg/ops.py": OPS_VALUE})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    # The text names ``value`` only: ``other`` does not reach it.
    plan2 = _plan(repo, base, repo.commit({"pkg/ops.py": OPS_OTHER}), T_A, T_C)
    assert selected(plan2) == set()


def test_r12_text_from_elsewhere_is_unbounded(repo):
    """``exec(gen.source())``: the project builds the text, which may import
    anything; nothing bounds the test, so any change reaches it."""
    base = repo.commit(
        {
            **BASE,
            "pkg/ops.py": OPS,
            "pkg/gen.py": (
                f"SRC = '{IMPORT_OPS}\\nres = ops.value()'\n\n\ndef source():\n    return SRC\n"
            ),
            "tests/test_a.py": (
                "from pkg import gen\n\n\ndef test_a():\n    ns = {}\n    exec(gen.source(), ns)\n"
            ),
            "tests/test_c.py": CONTROL,
        }
    )
    head = repo.commit({"pkg/ops.py": OPS_VALUE})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dynamic_reference"}


PLUGIN_PROJECT = {
    **BASE,
    "pyproject.toml": (
        "[project]\nname = 'pytest-myplug'\nversion = '0'\n\n"
        "[project.entry-points.pytest11]\nmyplug = 'pytest_myplug'\n"
    ),
    "pytest_myplug.py": "import pytest\n\n\n@pytest.fixture\ndef plugval():\n    return 1\n",
    "tests/conftest.py": "pytest_plugins = ['pytester']\n",
    "tests/test_c.py": CONTROL,
}


PLUGIN_CHANGED = "import pytest\n\n\n@pytest.fixture\ndef plugval():\n    return 2\n"


ROOT = "import pathlib\n\nROOT = str(pathlib.Path(__file__).resolve().parent.parent)\n"


INLINE = "def test_x(plugval):\\n    assert plugval == 1\\n"


PYTESTER_TESTS = {
    "subprocess-dash-p": (
        f"{ROOT}\n\ndef test_a(pytester):\n    pytester.makepyfile('{INLINE}')\n"
        "    pytester.syspathinsert(ROOT)\n"
        "    r = pytester.runpytest_subprocess('-p', 'pytest_myplug')\n"
        "    r.assert_outcomes(passed=1)\n"
    ),
    "entry-point-only": (
        f"{ROOT}\n\ndef test_a(pytester):\n    pytester.makepyfile('{INLINE}')\n"
        "    r = pytester.runpytest()\n    r.assert_outcomes(passed=1)\n"
    ),
    "makeconftest-pytest_plugins": (
        f"{ROOT}\n\ndef test_a(pytester):\n"
        "    pytester.makeconftest('pytest_plugins = [\"pytest_myplug\"]')\n"
        f"    pytester.makepyfile('{INLINE}')\n"
        "    r = pytester.runpytest_inprocess()\n    r.assert_outcomes(passed=1)\n"
    ),
    "testdir": (
        f"{ROOT}\n\ndef test_a(testdir):\n    testdir.makepyfile('{INLINE}')\n"
        "    r = testdir.runpytest()\n    r.assert_outcomes(passed=1)\n"
    ),
    "through-a-fixture": (
        f"{ROOT}\nimport pytest\n\n\n@pytest.fixture\ndef session(pytester):\n"
        f"    pytester.makepyfile('{INLINE}')\n    return pytester\n\n\n"
        "def test_a(session):\n    session.runpytest().assert_outcomes(passed=1)\n"
    ),
}


@pytest.mark.parametrize("text", list(PYTESTER_TESTS.values()), ids=list(PYTESTER_TESTS))
def test_r13_a_pytester_test_depends_on_the_projects_plugins(repo, text):
    """The inline session loads the project's ``pytest11`` plugin (or is
    told to), and the inline test requests its fixture by a name only the
    text holds: a body change of the fixture reaches the test."""
    base = repo.commit({**PLUGIN_PROJECT, "tests/test_a.py": text})
    head = repo.commit({"pytest_myplug.py": PLUGIN_CHANGED})
    plan = repo.plan(base, head, [NOOP], source_roots=["."], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a"}


def test_r13_what_the_inline_text_imports_counts(repo):
    """``makepyfile("from pkg import ops ...")``: the text is read as code,
    so a body change of what it calls reaches the test, and ``other`` does
    not."""
    inline = f"{IMPORT_OPS}\\ndef test_x():\\n    assert ops.value() == 1\\n"
    base = repo.commit(
        {
            **BASE,
            "pkg/ops.py": OPS,
            "tests/conftest.py": "pytest_plugins = ['pytester']\n",
            "tests/test_c.py": CONTROL,
            "tests/test_a.py": (
                f"{ROOT}\n\ndef test_a(pytester):\n    pytester.makepyfile('{inline}')\n"
                "    pytester.syspathinsert(ROOT)\n"
                "    pytester.runpytest_inprocess().assert_outcomes(passed=1)\n"
            ),
        }
    )
    kwargs = {"source_roots": ["."], "discover_runners": ["pytest"]}
    plan = repo.plan(base, repo.commit({"pkg/ops.py": OPS_VALUE}), [NOOP], **kwargs)
    assert selected(plan) == {"tests/test_a.py::test_a"}
    plan2 = repo.plan(base, repo.commit({"pkg/ops.py": OPS_OTHER}), [NOOP], **kwargs)
    assert selected(plan2) == set()


INNER_TEST = "from pkg import ops\n\n\ndef test_v(expected):\n    assert ops.value() == expected\n"


INNER_CASES = (
    "import unittest\nfrom pkg import ops\n\n\nclass T(unittest.TestCase):\n"
    "    def test_v(self):\n        self.assertEqual(ops.value(), 1)\n"
)


INNER_CONFTEST = "import pytest\n\n\n@pytest.fixture\ndef expected():\n    return 1\n"


INNER = {
    "pkg/ops.py": OPS,
    "tests/inner/__init__.py": "",
    "tests/inner/conftest.py": INNER_CONFTEST,
    "tests/inner/check_inner.py": INNER_TEST,
    "tests/inner/cases.py": INNER_CASES,
    "tests/test_c.py": CONTROL,
}


RUN = "import subprocess\nimport sys\n\n\ndef test_a():\n    subprocess.run("


INNER_PATH = "tests/inner/check_inner.py"


RUNNER_TESTS = {
    "subprocess-pytest-path": (
        f"{RUN}[sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '{INNER_PATH}'])\n"
    ),
    "subprocess-pytest-nodeid": (
        f"{RUN}[sys.executable, '-m', 'pytest', '-x', '{INNER_PATH}::test_v'])\n"
    ),
    "pytest-main-literal": (
        "import pytest\n\n\ndef test_a():\n"
        f"    assert pytest.main(['-p', 'no:cacheprovider', '{INNER_PATH}']) == 0\n"
    ),
    "pytest-main-joined-path": (
        "import pathlib\nimport pytest\n\n\ndef test_a():\n"
        "    here = pathlib.Path(__file__).parent\n"
        "    assert pytest.main(['--tb', 'short', str(here / 'inner' / 'check_inner.py')]) == 0\n"
    ),
    "pytest-main-local-list": (
        f"import pytest\n\n\ndef test_a():\n    args = ['-q', '{INNER_PATH}']\n"
        "    assert pytest.main(args) == 0\n"
    ),
    "coverage-run-pytest": (
        f"{RUN}[sys.executable, '-m', 'coverage', 'run', '--source', 'pkg', "
        f"'-m', 'pytest', '{INNER_PATH}'])\n"
    ),
    "shell-line": f"{RUN}f'{{sys.executable}} -m pytest -q {INNER_PATH}', shell=True)\n",
}


UNITTEST_TESTS = {
    "subprocess-unittest-module": (
        f"{RUN}[sys.executable, '-m', 'unittest', '-v', 'tests.inner.cases'])\n"
    ),
    "unittest-main": (
        "import unittest\n\n\ndef test_a():\n"
        "    unittest.main(module='tests.inner.cases', exit=False)\n"
    ),
    "loader-by-name": (
        "import unittest\n\n\ndef test_a():\n"
        "    suite = unittest.defaultTestLoader.loadTestsFromName('tests.inner.cases')\n"
        "    assert unittest.TextTestRunner().run(suite).wasSuccessful()\n"
    ),
    "loader-instance-by-names": (
        "import unittest\n\n\ndef test_a():\n    loader = unittest.TestLoader()\n"
        "    suite = loader.loadTestsFromNames(['tests.inner.cases.T'])\n"
        "    assert unittest.TextTestRunner().run(suite).wasSuccessful()\n"
    ),
}


@pytest.mark.parametrize("text", list(RUNNER_TESTS.values()), ids=list(RUNNER_TESTS))
def test_r14_a_test_file_given_to_pytest_runs_whole(repo, text):
    """The file's test calls ``ops.value`` and uses a conftest fixture: a
    change to the called function, to the test's own body or to the fixture
    reaches the test that runs the file; ``other`` reaches nothing."""
    base = repo.commit({**BASE, **INNER, "tests/test_a.py": text})
    for change in (
        {"pkg/ops.py": OPS_VALUE},
        {"tests/inner/check_inner.py": INNER_TEST.replace("== expected", "== expected + 0")},
        {"tests/inner/conftest.py": INNER_CONFTEST.replace("return 1", "return 2")},
    ):
        plan = _plan(repo, base, repo.commit({**INNER, **change}), T_A, T_C)
        assert selected(plan) == {T_A.runner_id}, change
        assert rules(plan, T_A.runner_id) == {"dependency"}, change
    plan = _plan(repo, base, repo.commit({**INNER, "pkg/ops.py": OPS_OTHER}), T_A, T_C)
    assert selected(plan) == set()


@pytest.mark.parametrize("text", list(UNITTEST_TESTS.values()), ids=list(UNITTEST_TESTS))
def test_r14_a_module_given_to_unittest_runs_whole(repo, text):
    base = repo.commit({**BASE, **INNER, "tests/test_a.py": text})
    for change in (
        {"pkg/ops.py": OPS_VALUE},
        {"tests/inner/cases.py": INNER_CASES.replace("value(), 1", "value(), 1 + 0")},
    ):
        plan = _plan(repo, base, repo.commit({**INNER, **change}), T_A, T_C)
        assert selected(plan) == {T_A.runner_id}, change
        assert rules(plan, T_A.runner_id) == {"dependency"}, change
    plan = _plan(repo, base, repo.commit({**INNER, "pkg/ops.py": OPS_OTHER}), T_A, T_C)
    assert selected(plan) == set()


@pytest.mark.parametrize(
    "call",
    [
        "subprocess.run([sys.executable, '-m', 'pytest', 'tests/inner'])",
        "subprocess.run([sys.executable, '-m', 'pytest', 'tests'])",
        "subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])",
        "pytest.main()",
        "pytest.main(sys.argv[1:])",
        "unittest.main(module=None)",
        "unittest.defaultTestLoader.discover('tests')",
    ],
    ids=[
        "dir",
        "top-dir",
        "discover",
        "main-argv",
        "main-list-from-elsewhere",
        "unittest-argv",
        "loader-discover",
    ],
)
def test_r14_a_directory_or_a_run_from_elsewhere_is_unbounded(repo, call):
    base = repo.commit(
        {
            **BASE,
            **INNER,
            "pkg/other.py": "def g():\n    return 1\n",
            "tests/test_a.py": (
                "import pytest\nimport subprocess\nimport sys\nimport unittest\n\n\n"
                f"def test_a():\n    {call}\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "def g():\n    return 2\n"})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dynamic_reference"}


@pytest.mark.parametrize(
    "call",
    [
        "pytest.main(['--verbose', 'docs'])",
        "pytest.main(['--driver', 'docs'])",
        "subprocess.run([sys.executable, '-m', 'pytest', '--verbose', 'docs'])",
        "subprocess.run([sys.executable, '-m', 'unittest', 'docs'])",
        "pytest.main(['-q', '-p', 'no:cacheprovider'])",
        "pytest.main(['--cov', 'tests/inner/check_inner.py'])",
        "subprocess.run([sys.executable, '-m', 'unittest', '-v'])",
    ],
    ids=[
        "after-a-flag",
        "after-a-plugin-option",
        "subprocess",
        "unittest-name",
        "no-path",
        "only-an-options-argument",
        "unittest-discovers",
    ],
)
def test_u22_a_directory_of_doctest_files_given_to_a_runner_is_unbounded(repo, call):
    """``docs/`` holds a doctest text file only: the word after ``--verbose``
    (a flag) or an option nothing knows was taken for the option's argument
    unless it named a directory holding a ``.py`` file, so the run was
    "known" and ran nothing; the doctest calling ``pkg.other.g`` broke
    unseen. The word is now one of the runner's own arguments, and a
    directory is one that holds any file. A runner given no file at all
    collects its default (``testpaths``; unittest discovers), which was
    taken for a run of nothing."""
    base = repo.commit(
        {
            **BASE,
            **INNER,
            "pkg/other.py": "def g():\n    return 1\n",
            "docs/test_doc.txt": ">>> from pkg.other import g\n>>> g()\n1\n",
            "tests/test_a.py": (
                f"import pytest\nimport subprocess\nimport sys\n\n\ndef test_a():\n    {call}\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "def g():\n    return 2\n"})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dynamic_reference"}


def test_u22_the_argument_of_a_known_long_option_is_no_path(repo):
    """``--tb short`` and ``--rootdir docs``: their words are arguments, so
    the file the run names still runs whole and bounds it."""
    text = (
        "import pytest\n\n\ndef test_a():\n"
        f"    assert pytest.main(['--tb', 'short', '--rootdir', 'docs', '{INNER_PATH}']) == 0\n"
    )
    base = repo.commit({**BASE, **INNER, "docs/notes.txt": "x\n", "tests/test_a.py": text})
    plan = _plan(repo, base, repo.commit({**INNER, "pkg/ops.py": OPS_VALUE}), T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dependency"}
    plan = _plan(repo, base, repo.commit({**INNER, "pkg/ops.py": OPS_OTHER}), T_A, T_C)
    assert selected(plan) == set()


# --------------------------------------------------------------------------- strings and escapes
# Names in strings and objects handed to code outside the roots.


# S7: names a test reaches through a string.


@pytest.mark.parametrize(
    "test",
    [
        "def test_t(monkeypatch):\n    monkeypatch.setattr('lib.config.LEGACY', 0)\n",
        "import lib.config\n\n\ndef test_t(monkeypatch):\n"
        "    monkeypatch.setattr(lib.config, 'LEGACY', 0)\n",
        "import pytest\n\nimport lib.config\n\n\n@pytest.mark.skipif('lib.config.LEGACY')\n"
        "def test_t():\n    pass\n",
    ],
)
def test_s7_a_name_reached_through_a_string_is_a_dependency(repo, test):
    base = repo.commit(
        {
            "lib/__init__.py": "",
            "lib/config.py": "LEGACY = 1\nOTHER = 2\n",
            "lib/other.py": "def g():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/test_t.py": test,
            "tests/test_other.py": "from lib.other import g\n\n\ndef test_g():\n    g()\n",
        }
    )
    head = repo.commit({"lib/config.py": "OTHER = 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_t.py::test_t" in selected(plan)
    assert "tests/test_other.py::test_g" in unselected(plan)


T_X = py_target("tests/test_x.py::test_x", "tests.test_x.test_x")


TEST_X = "from pkg import api\n\n\ndef test_x():\n    assert api.run() == 1\n"


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


PROP = (
    "class C:\n    @property\n    def v(self):\n        return {v}\n\n"
    "    def m(self):\n        return {v}\n\n    def other(self):\n        return 0\n"
)


FROM_C = "from pkg.mod import C\n\n\n"


STRING_APIS = {
    "attrgetter": (
        f"import operator\n{FROM_C}def run():\n    return operator.attrgetter('v')(C())\n"
    ),
    "attrgetter-imported-name": (
        f"from operator import attrgetter\n{FROM_C}def run():\n    return attrgetter('v')(C())\n"
    ),
    "methodcaller": (
        f"import operator\n{FROM_C}def run():\n    return operator.methodcaller('m')(C())\n"
    ),
    "format-positional-field": f"{FROM_C}def run():\n    return int('{{0.v}}'.format(C()))\n",
    "format-keyword-field": f"{FROM_C}def run():\n    return int('{{c.v}}'.format(c=C()))\n",
    "format-template-constant": (
        "from pkg.mod import C\nfrom pkg.tmpl import FMT\n\n\ndef run():\n"
        "    return int(FMT.format(C()))\n"
    ),
    "formatter-class": (
        f"import string\n{FROM_C}def run():\n"
        "    return int(string.Formatter().format('{0.v}', C()))\n"
    ),
    "attrgetter-dotted": (
        f"import operator\n{FROM_C}def run():\n    return operator.attrgetter('inner.v')(C())\n"
    ),
    "attrgetter-parameter": (
        f"import operator\n{FROM_C}def pick(name):\n    return operator.attrgetter(name)(C())\n"
        "\n\ndef run():\n    return pick('v')\n"
    ),
    "methodcaller-parameter": (
        f"import operator\n{FROM_C}def call(name):\n"
        "    return operator.methodcaller(name)(C())\n\n\ndef run():\n    return call('m')\n"
    ),
    "format-template-parameter": (
        f"{FROM_C}def render(template):\n    return int(template.format(C()))\n\n\n"
        "def run():\n    return render('{0.v}')\n"
    ),
}


@pytest.mark.parametrize("api", list(STRING_APIS.values()), ids=list(STRING_APIS))
def test_r18_names_in_strings_the_stdlib_consumes_are_read_by_name(repo, api):
    """``attrgetter('v')``, ``methodcaller('m')``, ``'{0.v}'.format(...)``:
    the name in the string is read off the object; a body change of the
    property or method reaches the test. (``C()`` handed to the library's
    callable escapes too, R19, so every member of ``C`` reaches the test;
    the precision of that rule is test_r19_builtins_reading_dunders_only_hand_nothing_on's.)"""
    _api_scenario(repo, PROP.format(v=1), api, PROP.format(v=2), None)


def test_r18_a_name_from_elsewhere_is_a_dynamic_reference(repo):
    """``methodcaller(name)`` with ``name`` a test's parameter (pytest
    supplies it): any method may be called, so the test is bounded only by
    its import closure, which the change lies in."""
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": PROP.format(v=1),
            "tests/test_x.py": (
                "import operator\n\nimport pytest\n\nfrom pkg.mod import C\n\n\n"
                "@pytest.mark.parametrize('name', ['m'])\ndef test_x(name):\n"
                "    assert operator.methodcaller(name)(C()) == 1\n"
            ),
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/mod.py": PROP.format(v=2)}), T_X, T_C)
    assert selected(plan) == {T_X.runner_id}


WRITER = (
    "class W:\n    def __init__(self):\n        self.n = 0\n\n"
    "    def write(self, s):\n        self.n += {n}\n\n    def flush(self):\n        pass\n\n"
    "    def other(self):\n        return 0\n"
)


FROM_W = "from pkg.mod import W\n\n\n"


DUCK_APIS = {
    "json.dump": (
        f"import json\n{FROM_W}def run():\n    w = W()\n    json.dump(1, w)\n    return w.n\n"
    ),
    "print-file": f"{FROM_W}def run():\n    w = W()\n    print('x', file=w)\n    return w.n\n",
    "csv.writer": (
        f"import csv\n{FROM_W}def run():\n    w = W()\n    csv.writer(w).writerow([1])\n"
        "    return w.n\n"
    ),
    "logging.StreamHandler": (
        f"import logging\n{FROM_W}def run():\n    w = W()\n    h = logging.StreamHandler(w)\n"
        "    return w.n\n"
    ),
    "contextlib.closing": (
        f"import contextlib\n{FROM_W}def run():\n    w = W()\n    with contextlib.closing(w):\n"
        "        pass\n    return w.n\n"
    ),
    "stored-on-a-library-object": (
        f"import logging\n{FROM_W}def run():\n    h = logging.StreamHandler()\n"
        "    h.stream = W()\n    return 1\n"
    ),
    "appended-to-an-unknown-list": (
        f"{FROM_W}def run():\n    sinks = make()\n    sinks.append(W())\n    return 1\n\n\n"
        "def make():\n    return []\n"
    ),
    "factory-result": (
        f"import json\n{FROM_W}def make():\n    return W()\n\n\ndef run():\n"
        "    json.dump(1, make())\n    return 1\n"
    ),
    "class-without-constructor": (
        f"import json\n{FROM_W}class Box(dict):\n    pass\n\n\ndef run():\n    Box(W())\n"
        "    return 1\n"
    ),
}


@pytest.mark.parametrize("api", list(DUCK_APIS.values()), ids=list(DUCK_APIS))
def test_r19_an_instance_handed_to_code_outside_the_roots_escapes(repo, api):
    """A library (or a receiver of unknown type) calls plain-named methods
    on what it is handed: a body change of ``W.write`` reaches the test
    through the class."""
    plan = _api_scenario(repo, WRITER.format(n=1), api, WRITER.format(n=2), None)
    assert rules(plan, T_X.runner_id) == {"dependency"}


KEYS = (
    "class C:\n    def keys(self):\n        return ['a'{more}]\n\n"
    "    def __getitem__(self, k):\n        return 1\n"
)


@pytest.mark.parametrize(
    "body",
    ["len(dict(C()))", "len({**C()})", "len(dict(**C()))", "update(C())"],
    ids=["dict", "double-star-display", "double-star-call", "dict.update"],
)
def test_r19_the_keys_protocol_is_a_plain_name(repo, body):
    api = (
        f"{FROM_C}def update(x):\n    d = {{}}\n    d.update(x)\n    return len(d)\n\n\n"
        f"def run():\n    return {body}\n"
    )
    _api_scenario(repo, KEYS.format(more=""), api, KEYS.format(more=", 'b'"), None)


@pytest.mark.parametrize(
    "body",
    ["len(W())", "str(W())", "isinstance(W(), W)", "cast(W, None)", "repr(W())", "sorted(W())"],
    ids=["len", "str", "isinstance", "cast", "repr", "sorted"],
)
def test_r19_builtins_reading_dunders_only_hand_nothing_on(repo, body):
    """``len(w)`` calls ``__len__``, nothing by a plain name: ``W.write``
    changing reaches no test that only does that (the rule's precision)."""
    api = f"from typing import cast\n\n{FROM_W}def run():\n    {body}\n    return 1\n"
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": WRITER.format(n=1),
            "pkg/api.py": api,
            "tests/test_x.py": TEST_X,
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/mod.py": WRITER.format(n=2)}), T_X, T_C)
    assert selected(plan) == set()


# ---------------------------------------------------------------------- doctest namespace
# pytest's ``doctest_namespace`` is a dict its doctest runner reads: storing a
# module into it hands nothing on (roadmap item 19).

GRAPH_PKG = {
    "pkg/__init__.py": "import gc  # noqa: F401\n\nfrom pkg.ops import add  # noqa: F401\n",
    "pkg/ops.py": "def add(a, b):\n    return a + b\n",
    "tests/__init__.py": "",
    "tests/test_x.py": "def test_x():\n    pass\n",
}


def _graph_handles(repo, conftest: str) -> set[str]:
    from diffcone.model import GRAPH_HANDLE

    repo.commit({**GRAPH_PKG, "tests/conftest.py": conftest})
    index = build_index(read_snapshot(repo.path, "HEAD", source_roots=["."]))
    return {symbol for symbol, detail in index.reflection if detail == GRAPH_HANDLE}


DOCTEST_FIXTURE = (
    "import pytest\n\nimport pkg as pd\n\n\n@pytest.fixture(autouse=True)\n"
    "def add_doctest_imports(doctest_namespace):\n    doctest_namespace['pd'] = pd\n"
)


def test_a_store_into_doctest_namespace_hands_nothing_on(repo):
    """pandas' ``add_doctest_imports``: ``pkg`` imports ``gc``, so handing it
    on hands on the object-graph walker; storing it into pytest's
    ``doctest_namespace`` does not. Before: the fixture was a graph handle."""
    assert _graph_handles(repo, DOCTEST_FIXTURE) == set()


@pytest.mark.parametrize(
    "body",
    [
        # Handed to code nothing bounds: still a graph handle.
        "    register(pd)\n",
        # Rebound to another object first: a store into that object.
        "    doctest_namespace = REGISTRY\n    doctest_namespace['pd'] = pd\n",
    ],
)
def test_a_module_handed_elsewhere_from_a_doctest_fixture_still_is(repo, body):
    conftest = (
        "import pytest\n\nimport pkg as pd\nfrom other import register, REGISTRY\n\n\n"
        "@pytest.fixture(autouse=True)\ndef add_doctest_imports(doctest_namespace):\n" + body
    )
    assert _graph_handles(repo, conftest) == {"tests.conftest.add_doctest_imports"}

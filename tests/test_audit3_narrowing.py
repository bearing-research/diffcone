"""Regression scenarios for the third audit round's batch on the static
narrowings (internal/audit.md, round 3: PLN-1, PLN-2/IDX-2/IDX-3,
PLN-3/IDX-1, EVP-8, IDX-5).

A narrowing holds only while the table or module it reads is used in forms
the analysis recognises; every other use makes the lookup unbounded again.
Each regression test names the finding it guards and failed before its fix;
the ``stays_bounded`` tests guard the narrowing itself.
"""

from __future__ import annotations

import pytest

from diffcone.testing import asv_target, py_target, rules, selected

# ---------------------------------------------------------------- helpers

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


# ---------------------------------------------------------------- PLN-2 / IDX-2 / IDX-3


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


# ---------------------------------------------------------------- PLN-1


def _constructor_plan(repo, init, test, extra=None):
    base = repo.commit(
        {
            "pkg/__init__.py": init,
            "pkg/impl_b.py": "class Alt:\n    def __init__(self):\n        self.v = 1\n",
            "tests/test_t.py": test,
            "benchmarks/bench_t.py": "def time_nothing():\n    pass\n",
            **(extra or {}),
        }
    )
    head = repo.commit(
        {"pkg/impl_b.py": "class Alt:\n    def __init__(self):\n        self.v = 2\n"}
    )
    targets = [
        py_target("t::test_thing", "tests.test_t.test_thing"),
        asv_target("bench.time_nothing", "benchmarks.bench_t.time_nothing"),
    ]
    return repo.plan(base, head, targets)


LAZY_ALT = LAZY.replace("('pkg.impl_a', 'Thing')", "('pkg.impl_b', 'Alt')").format(extra="")


@pytest.mark.parametrize(
    "init",
    [
        LAZY_ALT,
        "import importlib\n\n\ndef __getattr__(name):\n"
        "    return getattr(importlib.import_module('pkg.impl_b'), 'Alt')\n",
    ],
    ids=["lazy_table", "literal_import_module"],
)
def test_pln1_a_class_served_by_getattr_reaches_its_constructor(repo, init):
    test = "import pkg\n\n\ndef test_thing():\n    assert pkg.Thing().v == 1\n"
    assert selected(_constructor_plan(repo, init, test)) == {"t::test_thing"}


def test_pln1_a_class_named_on_an_unknown_receiver_reaches_its_constructor(repo):
    test = (
        "import importlib\n\nfrom pkg.make import make\n\n\ndef test_thing():\n"
        "    assert make(importlib.import_module('pkg.impl_b')).v == 1\n"
    )
    extra = {"pkg/make.py": "def make(m):\n    return m.Alt()\n"}
    plan = _constructor_plan(repo, "", test, extra)
    assert selected(plan) == {"t::test_thing"}
    assert rules(plan, "t::test_thing") == {"unresolved_name_match"}


# ---------------------------------------------------------------- PLN-3 / IDX-1

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


# ---------------------------------------------------------------- EVP-8

NAMES = "import builtins\n\n\ndef lookup(name):\n    return getattr(builtins, name, None)\n"
WRITER = (
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
            "pkg/names.py": NAMES,
            "tests/test_names.py": (
                "from pkg.names import lookup\n\n\ndef test_names():\n"
                "    assert lookup('diffcone_named') is None\n"
            ),
            "tests/test_writer.py": WRITER,
            "benchmarks/bench_n.py": (
                "from pkg.names import lookup\n\n\ndef time_lookup():\n    lookup('x')\n\n\n"
                "def time_nothing():\n    pass\n"
            ),
        }
    )
    if how == "decorator":
        writer = WRITER.replace("[1])", "[install_names()])")
    else:
        writer = WRITER.replace("def test_writer(x):", "def test_writer(x, _=install_names()):")
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


# ---------------------------------------------------------------- IDX-5

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

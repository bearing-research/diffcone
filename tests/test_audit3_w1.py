"""Regression scenarios for what the third audit round left known: W1 (V4, a
module named at run time taken to be any module) and W9 (a module handed on
as a value and read by an unbounded ``getattr``) (internal/audit.md).

W1 bounds what a run-time module handle can be and what it can write, and
what code that obtains a module, walks the object graph or runs at import
can do to a test module's names. Each narrowing comes with the scenario
that keeps it honest: the same shape where the bound does not hold still
selects. W9 is a miss: a module passed to other code depends on its members,
as a class handed on does.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.testing import asv_target, py_target, rules, selected

# ---------------------------------------------------------------- static: helpers

HANDLERS = "def a():\n    return {}\n\n\ndef b():\n    return {}\n"

# ``pkg.core.run`` looks a handler up by a name a literal table gives (``a``);
# ``pkg.handlers.b`` changes, so the plan selects ``run``'s users only when
# something may change the table.
CORE = (
    "from pkg import handlers\n\nTABLE = {'k': 'a'}\n\n\n"
    "def run():\n    return getattr(handlers, TABLE['k'])()\n"
)


def _table_plan(repo, extra):
    files = {
        "pkg/__init__.py": "",
        "pkg/handlers.py": HANDLERS,
        "pkg/core.py": CORE,
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
        {"pkg/handlers.py": HANDLERS.replace("b():\n    return {}", "b():\n    return []")}
    )
    targets = [
        py_target("t::test_s", "tests.test_s.test_s"),
        asv_target("bench.time_run", "benchmarks.bench_s.time_run"),
        asv_target("bench.time_nothing", "benchmarks.bench_s.time_nothing"),
    ]
    return repo.plan(base, head, targets)


REACHED = {"t::test_s", "bench.time_run"}


def _check(plan, reached: bool) -> None:
    """The table stays bounded (nothing selected), or ``run`` became a
    dynamic reference again (its users selected, and maybe more)."""
    if reached:
        assert REACHED <= selected(plan)
        assert rules(plan, "t::test_s") == {"dynamic_reference"}
    else:
        assert selected(plan) == set()


# ---------------------------------------------------------------- W1: static

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
    plan = _table_plan(repo, {"pkg/cells.py": CELLS.format(namespace=namespace)})
    _check(plan, reached)


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
    plan = _table_plan(repo, {"pkg/mark.py": mark})
    _check(plan, reached)


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
    plan = _table_plan(repo, {"pkg/loader.py": loader})
    _check(plan, reached)


@pytest.mark.parametrize("package, reached", [("plugins", False), ("pkg", True)])
def test_w1_a_module_named_under_dunder_name_is_a_submodule(repo, package, reached):
    # strata_client.integration's lazy ``__getattr__``.
    getter = (
        "import importlib\n\n\ndef __getattr__(name):\n"
        '    module = importlib.import_module(f"{__name__}.{name}")\n'
        "    globals()[name] = module\n    return module\n"
    )
    extra = {f"{package}/__init__.py": getter}
    plan = _table_plan(repo, extra)
    _check(plan, reached)


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
    plan = _table_plan(repo, {"pkg/fresh.py": fresh})
    _check(plan, reached)


# ---------------------------------------------------------------- W9: static

READER = "def read(obj, names):\n    return getattr(obj, names[0])()\n"
OPS = "def add(a, b):\n    return a + b\n\n\ndef other():\n    return 1\n"


@pytest.mark.parametrize("passed", [True, False])
def test_w9_a_module_passed_on_depends_on_its_members(repo, passed):
    call = "read(ops, ['other'])" if passed else "ops.add(1, 2)"
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "pkg/reader.py": READER,
            "tests/test_r.py": (
                f"from pkg import ops\nfrom pkg.reader import read\n\n\ndef test_r():\n    {call}\n"
            ),
            "benchmarks/bench_r.py": (
                f"from pkg import ops\nfrom pkg.reader import read\n\n\ndef time_r():\n    {call}\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": OPS.replace("return 1", "return 2")})
    targets = [
        py_target("t::test_r", "tests.test_r.test_r"),
        asv_target("bench.time_r", "benchmarks.bench_r.time_r"),
    ]
    plan = repo.plan(base, head, targets)
    if passed:
        assert selected(plan) == {"t::test_r", "bench.time_r"}
        assert rules(plan, "t::test_r") == {"dependency"}
    else:
        # A module only read from is not handed on: ``ops.add`` alone is
        # what the test depends on.
        assert selected(plan) == set()


# ---------------------------------------------------------------- evidence

evidence = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}
# A test module whose data list changes (strata's MALFORMED), read by its
# top-level code.
DATA = (
    "import pytest\n\nCASES = [1, 2]\n\n\n"
    "@pytest.mark.parametrize('case', CASES)\ndef test_data(case):\n    assert case\n"
)
DATA_CHANGED = DATA.replace("[1, 2]", "[1, 2, 3]")
# Library code that looks names up on an object from elsewhere, and a test
# that runs it.
LOOK = "def look(obj, names):\n    return [getattr(obj, name, None) for name in names]\n"
TEST_LOOK = (
    "from pkg.look import look\n\n\nclass Thing:\n    x = 1\n\n\n"
    "def test_look():\n    assert look(Thing, ['x']) == [1]\n"
)
LOOKER = "tests/test_look.py::test_look"


def _plan(repo, base, head, ev):
    return repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)


def _data_plan(repo, files):
    base = repo.commit({**BASE, "pkg/look.py": LOOK, "tests/test_look.py": TEST_LOOK, **files})
    ev = repo.collect()
    head = repo.commit({"tests/test_data.py": DATA_CHANGED})
    return _plan(repo, base, head, ev)


LAZY = """\
import importlib

_LAZY = {"Thing": ("pkg.things", "Thing")}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(name)
    return getattr(importlib.import_module(target[0]), target[1])
"""
# A module named by data, handed on: any module, so any table may change.
LOADER = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"


@evidence
@pytest.mark.parametrize("loaded", ["in a test", "at import"])
def test_w1_a_lazy_table_names_a_test_module_only_after_its_writers_ran(repo, loaded):
    # strata.__getattr__ runs at import (a test module imports pkg.Thing),
    # and its table may change only through pkg.loader.load's handle. When
    # load runs only inside tests, the getter cannot have obtained a test
    # module outside every test.
    files = {
        "pkg/__init__.py": LAZY,
        "pkg/things.py": "class Thing:\n    pass\n",
        "pkg/loader.py": LOADER,
        "tests/test_data.py": DATA,
        "tests/test_lazy.py": (
            "from pkg import Thing\nfrom pkg.loader import load\n"
            + ("\nload('json')\n" if loaded == "at import" else "")
            + "\n\ndef test_lazy():\n    assert Thing and load('json')\n"
        ),
    }
    plan = _data_plan(repo, files)
    if loaded == "at import":
        assert LOOKER in selected(plan)
        assert "lookup_site" in rules(plan, LOOKER)
    else:
        assert LOOKER not in selected(plan)


@evidence
@pytest.mark.parametrize("call, walks", [("get_stats()", False), ("get_objects()", True)])
def test_w1_a_gc_call_walks_the_graph_only_when_it_hands_objects_out(repo, call, walks):
    # strata's GCTracker.install (gc.callbacks) and metrics (gc.get_stats).
    files = {
        "pkg/stats.py": f"import gc\n\n\ndef stats():\n    return gc.{call}\n",
        "tests/test_data.py": DATA,
        "tests/test_stats.py": (
            "from pkg.stats import stats\n\n\ndef test_stats():\n    assert stats() is not None\n"
        ),
    }
    plan = _data_plan(repo, files)
    assert ("tests/test_stats.py::test_stats" in selected(plan)) is walks


@evidence
@pytest.mark.parametrize("kept", [False, True])
def test_w1_an_import_whose_module_is_not_kept_hands_it_to_nothing(repo, kept):
    # strata's pool_worker.warm_imports.
    body = "return __import__(name)" if kept else "__import__(name)"
    files = {
        "pkg/warm.py": f"def warm(names):\n    for name in names:\n        {body}\n",
        "tests/test_data.py": DATA,
        "tests/test_warm.py": (
            "from pkg.warm import warm\n\n\ndef test_warm():\n    warm(['json'])\n"
        ),
    }
    plan = _data_plan(repo, files)
    assert ("tests/test_warm.py::test_warm" in selected(plan)) is kept


# A lookup on builtins, which library code writes to (only inside tests).
BUILTINS = {
    "pkg/names.py": (
        "import builtins\n\n\ndef lookup(name):\n    return getattr(builtins, name, None)\n"
    ),
    "pkg/extra.py": "import builtins\n\n\ndef install():\n    builtins.diffcone_x = 1\n",
    "tests/test_names.py": (
        "from pkg.names import lookup\n\n\ndef test_names():\n    assert lookup('len') is len\n"
    ),
    "tests/test_install.py": (
        "import builtins\n\nfrom pkg.extra import install\n\n\n"
        "def test_install():\n    install()\n    del builtins.diffcone_x\n"
    ),
}
NAMES = "tests/test_names.py::test_names"


@evidence
@pytest.mark.parametrize(
    "data, reached",
    [
        (DATA, False),
        (DATA.replace("CASES = [1, 2]", "CASES = [len(str(1)), 2]"), False),
        (
            "from pkg.extra import install\n\n"
            + DATA.replace("CASES = [1, 2]", "CASES = [1, 2]")
            + "\n\nSET_UP = install()\n",
            True,
        ),
    ],
    ids=["a literal", "a computed value", "import code that runs the writer"],
)
def test_w1_import_time_code_reaches_an_external_lookup_only_through_a_writer(repo, data, reached):
    # strata: ``MALFORMED = [...]`` changed in a test module, whose top-level
    # code (a parametrize) reads it; configure_logging's ``getattr(logging,
    # level)`` is a lookup on a module library code writes to. The changed
    # import-time code can run no writer, so the lookup cannot see it.
    files = {**BUILTINS, "tests/test_data.py": data}
    base = repo.commit({**BASE, **files})
    ev = repo.collect()
    head = repo.commit({"tests/test_data.py": data.replace("2]", "2, 3]")})
    plan = _plan(repo, base, head, ev)
    assert (NAMES in selected(plan)) is reached


@evidence
@pytest.mark.parametrize("imported", ["pkg.boot", "pkg.quiet"])
def test_w1_a_new_import_runs_what_the_imported_module_runs_at_import(repo, imported):
    # The import-time code that changed is a new import: what the imported
    # module's own import runs (pkg.boot runs the writer) now runs there.
    files = {
        **BUILTINS,
        "pkg/boot.py": "from pkg.extra import install\n\ninstall()\n",
        "pkg/quiet.py": "QUIET = 1\n",
        "tests/test_data.py": DATA,
    }
    base = repo.commit({**BASE, **files})
    ev = repo.collect()
    head = repo.commit({"tests/test_data.py": f"import {imported}  # noqa: F401\n" + DATA})
    plan = _plan(repo, base, head, ev)
    assert (NAMES in selected(plan)) is (imported == "pkg.boot")


@evidence
def test_w9_a_module_passed_on_in_evidence_mode(repo):
    base = repo.commit(
        {
            **BASE,
            "pkg/ops.py": OPS,
            "pkg/reader.py": READER,
            "tests/test_r.py": (
                "from pkg import ops\nfrom pkg.reader import read\n\n\n"
                "def test_r():\n    assert read(ops, ['other']) == 1\n"
            ),
        }
    )
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS + "\n\ndef added():\n    return 3\n"})
    plan = _plan(repo, base, head, ev)
    # An added member is seen by the lookup on an object from elsewhere.
    assert "tests/test_r.py::test_r" in selected(plan)


@evidence
@pytest.mark.parametrize("added", ["helper", "workerinput"])
def test_w13_hasattr_with_a_literal_notices_only_that_name(repo, added):
    # strata's conftest: ``hasattr(session.config, "workerinput")`` was a
    # reflection site that saw every name added anywhere.
    thing = "class Thing:\n    def run(self):\n        return 1\n"
    base = repo.commit(
        {
            **BASE,
            "pkg/things.py": thing,
            "pkg/check.py": 'def check(obj):\n    return hasattr(obj, "workerinput")\n',
            "tests/test_check.py": (
                "from pkg.check import check\n\n\n"
                "def test_check():\n    assert not check(object())\n"
            ),
        }
    )
    ev = repo.collect()
    head = repo.commit({"pkg/things.py": thing + f"\n    def {added}(self):\n        return 2\n"})
    plan = _plan(repo, base, head, ev)
    assert ("tests/test_check.py::test_check" in selected(plan)) is (added == "workerinput")

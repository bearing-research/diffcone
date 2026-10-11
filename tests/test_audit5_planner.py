"""Regression scenarios for the fifth audit round (behaviour-first reviews),
batch A: hashing, classification and the planner (R6, R7, R11, R20, R21,
R23, R24, R26, R27).

Each test names the finding it guards and failed before its fix, unless its
docstring says it is a guard. Scenarios mix pytest targets (discovered) and
ASV targets (a manifest) where the shape allows, and assert exact target
sets.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.discovery import DiscoveryOptions
from diffcone.testing import asv_target, changes, path_ids, reason, rules, selected

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _plan(repo, base, head, targets=(), evidence=None, **kwargs):
    return repo.plan(
        base, head, list(targets), discover_runners=["pytest"], evidence=evidence, **kwargs
    )


# --------------------------------------------------------------------------- R6

# R6: no hash held the order of a module's or class's statements relative
# to its definitions (variables are symbols of their own, the module body
# hash strips defs and imports, class attributes hash per name), so a
# reorder changed no symbol and selected nothing.

BENCH_B = asv_target("bench.time_b", "benchmarks.bench.time_b", "benchmarks.bench")
RUNNERS_B = {
    "tests/test_m.py": "from pkg.m import b\n\n\ndef test_b():\n    assert b() == 2\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.m import b\n\n\ndef time_b():\n    b()\n",
}


def test_r6_reordering_module_statements_is_a_definition_change(repo):
    """``A = 1; A = 2; B = A`` -> ``A = 1; B = A; A = 2``: ``B`` now holds
    1. No hash changed; the module's statement order did."""
    base = repo.commit(
        {**BASE, **RUNNERS_B, "pkg/m.py": "A = 1\nA = 2\nB = A\n\n\ndef b():\n    return B\n"}
    )
    head = repo.commit({"pkg/m.py": "A = 1\nB = A\nA = 2\n\n\ndef b():\n    return B\n"})
    plan = _plan(repo, base, head, [BENCH_B])
    assert changes(plan) == {"pkg.m": ("definition_changed",)}
    assert selected(plan) == {"tests/test_m.py::test_b", "bench.time_b"}
    assert rules(plan, "bench.time_b") == {"dependency"}


def test_r6_variable_moved_below_the_decorator_reading_it(repo):
    """``X = 1`` moved below ``@deco(X) def f``: the decorator sees no
    ``X`` any more (NameError at import)."""
    deco = "def deco(x):\n    def wrap(f):\n        f.x = x\n        return f\n    return wrap\n"
    base = repo.commit(
        {
            **BASE,
            **RUNNERS_B,
            "pkg/d.py": deco,
            "pkg/m.py": (
                "from pkg.d import deco\n\nX = 1\n\n\n@deco(X)\ndef b():\n    return b.x + 1\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/m.py": (
                "from pkg.d import deco\n\n\n@deco(X)\ndef b():\n    return b.x + 1\n\n\nX = 1\n"
            )
        }
    )
    plan = _plan(repo, base, head, [BENCH_B])
    assert changes(plan) == {"pkg.m": ("definition_changed",)}
    assert selected(plan) == {"tests/test_m.py::test_b", "bench.time_b"}


def test_r6_def_moved_below_a_fallback_import(repo):
    """``def f`` moved below ``try: from pkg.fast import f except
    ImportError: pass``: the import now wins over the local definition."""
    fast = "def b():\n    return 1\n"
    local = "def b():\n    return 2\n"
    fallback = "try:\n    from pkg.fast import b\nexcept ImportError:\n    pass\n"
    base = repo.commit(
        {**BASE, **RUNNERS_B, "pkg/fast.py": fast, "pkg/m.py": fallback + "\n\n" + local}
    )
    head = repo.commit({"pkg/m.py": local + "\n\n" + fallback})
    plan = _plan(repo, base, head, [BENCH_B])
    assert "definition_changed" in changes(plan)["pkg.m"]
    assert selected(plan) == {"tests/test_m.py::test_b", "bench.time_b"}


def test_r6_two_registering_defs_swapped(repo):
    """Two ``@register`` defs trade places: the registry's order, read by
    ``first()``, changes. Both defs run code at import, so their order is
    not free."""
    reg = (
        "ORDER = []\n"
        "\n"
        "\n"
        "def register(f):\n"
        "    ORDER.append(f.__name__)\n"
        "    return f\n"
        "\n"
        "\n"
        "def first():\n"
        "    return ORDER[0]\n"
    )
    one = "@register\ndef one():\n    return 1\n"
    two = "@register\ndef two():\n    return 2\n"
    runners = {
        "tests/test_r.py": (
            "from pkg.reg import first\n\n\ndef test_first():\n    assert first() == 'one'\n"
        ),
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": "from pkg.reg import first\n\n\ndef time_first():\n    first()\n",
    }
    base = repo.commit(
        {
            **BASE,
            **runners,
            "pkg/reg.py": reg,
            "pkg/m.py": f"from pkg.reg import register\n\n\n{one}\n\n{two}",
            "tests/test_m.py": "import pkg.m\n\n\ndef test_m():\n    pass\n",
        }
    )
    head = repo.commit({"pkg/m.py": f"from pkg.reg import register\n\n\n{two}\n\n{one}"})
    bench = asv_target("bench.time_first", "benchmarks.bench.time_first", "benchmarks.bench")
    plan = _plan(repo, base, head, [bench])
    assert changes(plan) == {"pkg.m": ("definition_changed",)}
    # test_m imports pkg.m: its import-time code changed. test_first and the
    # benchmark read ORDER, mutated by register, which pkg.m's import runs.
    assert selected(plan) == {
        "tests/test_m.py::test_m",
        "tests/test_r.py::test_first",
        "bench.time_first",
    }


def test_r6_class_statement_moved_across_a_method(repo):
    """``m = staticmethod(m)`` moved above ``def m`` in the class body:
    NameError at import. The class body hash strips defs, its definition
    hash holds the sorted member names: neither saw it."""
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": "class A:\n    def m(x):\n        return x\n\n    m = staticmethod(m)\n",
            "tests/test_a.py": (
                "from pkg.mod import A\n\n\ndef test_a():\n    assert A().m(1) == 1\n"
            ),
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    head = repo.commit(
        {"pkg/mod.py": "class A:\n    m = staticmethod(m)\n\n    def m(x):\n        return x\n"}
    )
    plan = _plan(repo, base, head)
    assert changes(plan) == {"pkg.mod.A": ("definition_changed",)}
    assert selected(plan) == {"tests/test_a.py::test_a"}


def test_r6_appending_and_swapping_inert_defs_is_no_reorder(repo):
    """Guard (precision): a function appended at the end of a module is its
    own ``added`` change; two plain defs trading places, and a new def
    inserted between two statements, change nothing that runs."""
    body = "A = 1\n\n\ndef f():\n    return A\n\n\ndef g():\n    return 2\n\n\nB = f()\n"
    base = repo.commit(
        {
            **BASE,
            "pkg/m.py": body,
            "tests/test_m.py": "from pkg.m import B\n\n\ndef test_b():\n    assert B == 1\n",
        }
    )
    appended = repo.commit({"pkg/m.py": body + "\n\ndef h():\n    return 3\n"})
    plan = _plan(repo, base, appended)
    assert changes(plan) == {"pkg.m.h": ("added",)}
    assert selected(plan) == set()
    swapped = repo.commit(
        {"pkg/m.py": "A = 1\n\n\ndef g():\n    return 2\n\n\ndef f():\n    return A\n\n\nB = f()\n"}
    )
    plan = _plan(repo, base, swapped)
    assert changes(plan) == {}
    inserted = repo.commit(
        {
            "pkg/m.py": (
                "A = 1\n"
                "\n"
                "\n"
                "def f():\n"
                "    return A\n"
                "\n"
                "\n"
                "def new():\n"
                "    return 0\n"
                "\n"
                "\n"
                "def g():\n"
                "    return 2\n"
                "\n"
                "\n"
                "B = f()\n"
            )
        }
    )
    plan = _plan(repo, base, inserted)
    assert changes(plan) == {"pkg.m.new": ("added",)}


@needs_monitoring
def test_r6_evidence_escalates_a_class_reorder(repo):
    """Evidence mode: a class body reorder is no attribute's own change; it
    escalates the module, as the class statement would."""
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": (
                "SEEN = []\n"
                "\n"
                "\n"
                "class A:\n"
                "    def m(self):\n"
                "        return 1\n"
                "\n"
                "    SEEN.append(m)\n"
                "\n"
                "\n"
                "def count():\n"
                "    return len(SEEN)\n"
            ),
            "tests/test_a.py": (
                "from pkg.mod import count\n\n\ndef test_a():\n    assert count() == 1\n"
            ),
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    evidence = repo.collect()
    head = repo.commit(
        {
            "pkg/mod.py": (
                "SEEN = []\n"
                "\n"
                "\n"
                "class A:\n"
                "    SEEN.append(m)\n"
                "\n"
                "    def m(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "def count():\n"
                "    return len(SEEN)\n"
            )
        }
    )
    plan = _plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert "escalated" in rules(plan, "tests/test_a.py::test_a")


# --------------------------------------------------------------------------- R24

# R24: an import added (or removed) that starts (or stops) running a
# registering module's body: only the importer changed, and the planner
# never entered an unchanged module, so the registry's readers were missed.

REG = {
    "pkg/reg.py": "REG = {}\n\n\ndef names():\n    return sorted(REG)\n",
    "pkg/plug.py": "import pkg.reg\n\npkg.reg.REG['a'] = 1\n",
    "tests/test_y.py": "from pkg.reg import names\n\n\ndef test_y():\n    assert names() == []\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.reg import names\n\n\ndef time_names():\n    names()\n",
}
BENCH_NAMES = asv_target("bench.time_names", "benchmarks.bench.time_names", "benchmarks.bench")
IMPORT_CASES = {
    "test_module": (
        "tests/test_x.py",
        "def test_x():\n    pass\n",
        "import pkg.plug\n\n\ndef test_x():\n    pass\n",
    ),
    "from_import": (
        "tests/test_x.py",
        "def test_x():\n    pass\n",
        "from pkg import plug\n\n\ndef test_x():\n    pass\n",
    ),
    "function_local": (
        "tests/test_x.py",
        "def test_x():\n    pass\n",
        "def test_x():\n    import pkg.plug\n",
    ),
    "project_module": (
        "pkg/cli.py",
        "def main():\n    return 0\n",
        "import pkg.plug\n\n\ndef main():\n    return 0\n",
    ),
}


@pytest.mark.parametrize("case", sorted(IMPORT_CASES))
def test_r24_import_added_runs_a_registering_module(repo, case):
    path, before, after = IMPORT_CASES[case]
    files = {**BASE, **REG, path: before}
    if path.startswith("pkg/"):
        files["tests/test_x.py"] = "import pkg.cli\n\n\ndef test_x():\n    assert pkg.cli.main\n"
    base = repo.commit(files)
    head = repo.commit({path: after})
    plan = _plan(repo, base, head, [BENCH_NAMES])
    assert selected(plan) == {
        "tests/test_x.py::test_x",
        "tests/test_y.py::test_y",
        "bench.time_names",
    }
    why = reason(plan, "tests/test_y.py::test_y")
    assert why.rule == "dependency"
    assert "pkg.reg.REG" in path_ids(why) and "pkg.plug" in path_ids(why)
    assert any(step.kind == "newly_imported_by" for step in why.path)


def test_r24_import_removed_stops_a_registering_module(repo):
    base = repo.commit(
        {**BASE, **REG, "tests/test_x.py": "import pkg.plug\n\n\ndef test_x():\n    pass\n"}
    )
    head = repo.commit({"tests/test_x.py": "def test_x():\n    pass\n"})
    plan = _plan(repo, base, head, [BENCH_NAMES])
    assert selected(plan) == {
        "tests/test_x.py::test_x",
        "tests/test_y.py::test_y",
        "bench.time_names",
    }
    why = reason(plan, "bench.time_names")
    assert any(step.kind == "no_longer_imported_by" for step in why.path)


def test_r24_import_added_binds_the_submodule_on_its_package(repo):
    """``import pkg.sub`` added in one test module makes ``pkg.sub`` reachable
    under ``import pkg`` in another, which read it through the package."""
    base = repo.commit(
        {
            **BASE,
            "pkg/sub.py": "def f():\n    return 1\n",
            "tests/test_x.py": "def test_x():\n    pass\n",
            "tests/test_y.py": "import pkg\n\n\ndef test_y():\n    assert pkg.sub.f() == 1\n",
        }
    )
    head = repo.commit({"tests/test_x.py": "import pkg.sub\n\n\ndef test_x():\n    pass\n"})
    plan = _plan(repo, base, head)
    assert selected(plan) == {"tests/test_x.py::test_x", "tests/test_y.py::test_y"}
    assert any(
        step.kind == "bound_on_package" for step in reason(plan, "tests/test_y.py::test_y").path
    )


def test_r24_import_already_in_the_closure_runs_nothing_new(repo):
    """Guard (precision): an import of a module the importer's own imports
    already ran leaves nothing different behind."""
    base = repo.commit(
        {
            **BASE,
            **REG,
            "pkg/core.py": "import pkg.plug\n",
            "tests/test_x.py": "import pkg.core\n\n\ndef test_x():\n    pass\n",
        }
    )
    head = repo.commit(
        {"tests/test_x.py": "import pkg.core\nimport pkg.plug\n\n\ndef test_x():\n    pass\n"}
    )
    plan = _plan(repo, base, head, [BENCH_NAMES])
    assert changes(plan) == {"tests.test_x": ("imports_added",)}
    assert selected(plan) == set()


@needs_monitoring
def test_r24_evidence_follows_a_newly_run_registering_module(repo):
    """Evidence mode: no record shows ``pkg.plug`` running at C (nothing
    imported it), so what its import-time code writes is a changed value
    for the registry's readers."""
    base = repo.commit({**BASE, **REG, "tests/test_x.py": "def test_x():\n    pass\n"})
    evidence = repo.collect()
    head = repo.commit({"tests/test_x.py": "import pkg.plug\n\n\ndef test_x():\n    pass\n"})
    plan = _plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {"tests/test_x.py::test_x", "tests/test_y.py::test_y"}
    assert "executed_reader" in rules(plan, "tests/test_y.py::test_y")


# --------------------------------------------------------------------------- R23, R26, R27

# The isolation assumption: a store made by code that runs only inside one
# test is seen by that test alone. R23: a doctest's entry is the project
# function itself, and a manifest may point at project code; both were
# isolated by the file their id names. R26: an entry (or ``setUp``) that
# module-level code calls is not isolated. R27: a session-scoped
# ``monkeypatch`` override stores for good.

ISOLATION = {
    "pkg/reg.py": "REG = {}\n\n\ndef current():\n    return REG.get('k')\n",
    "pkg/impl.py": "def make():\n    return 1\n",
    "tests/test_r.py": (
        "from pkg.reg import current\n\n\ndef test_r():\n    assert current() == 1\n"
    ),
}
IMPL2 = {"pkg/impl.py": "def make():\n    return 2\n"}
FILLER = (
    "import pkg.impl\nimport pkg.reg\n\n\n"
    "def fill():\n    '''Fill the registry.\n\n    >>> fill()\n    '''\n"
    "    pkg.reg.REG['k'] = pkg.impl.make()\n"
)


def test_r23_a_doctest_entry_is_no_isolated_test_code(repo):
    """With ``--doctest-modules`` the project function ``fill`` is a target's
    entry. It fills the registry when a test module imports and calls it at
    collection; test_r reads the registry."""
    base = repo.commit(
        {
            **BASE,
            **ISOLATION,
            "pkg/mod.py": FILLER,
            "tests/test_x.py": "import pkg.mod\n\npkg.mod.fill()\n\n\ndef test_x():\n    pass\n",
        }
    )
    head = repo.commit(IMPL2)
    options = DiscoveryOptions(runner_args=("--doctest-modules",))
    plan = _plan(repo, base, head, discovery_options=options)
    assert selected(plan) == {
        "pkg/mod.py::pkg.mod.fill",
        "tests/test_x.py::test_x",
        "tests/test_r.py::test_r",
    }
    assert rules(plan, "tests/test_r.py::test_r") == {"dependency"}


def test_r23_a_manifest_entry_on_project_code_is_no_isolated_test_code(repo):
    """An ASV manifest target whose entry is the project's ``fill``: the
    function stays a writer for the pytest reader."""
    base = repo.commit(
        {
            **BASE,
            **ISOLATION,
            "pkg/mod.py": FILLER,
            "tests/test_x.py": "import pkg.mod\n\npkg.mod.fill()\n\n\ndef test_x():\n    pass\n",
        }
    )
    head = repo.commit(IMPL2)
    bench = asv_target("pkg/mod.py::fill", "pkg.mod.fill")
    plan = _plan(repo, base, head, [bench])
    assert selected(plan) == {
        "pkg/mod.py::fill",
        "tests/test_x.py::test_x",
        "tests/test_r.py::test_r",
    }


def test_r26_a_test_called_at_module_level_is_no_isolated_writer(repo):
    """``test_a`` stores into the registry; another test module imports and
    calls it at collection, so its store outlives it."""
    base = repo.commit(
        {
            **BASE,
            **ISOLATION,
            "tests/test_a.py": (
                "import pkg.impl\n"
                "import pkg.reg\n"
                "\n"
                "\n"
                "def test_a():\n"
                "    pkg.reg.REG['k'] = pkg.impl.make()\n"
            ),
            "tests/test_b.py": (
                "from tests.test_a import test_a\n\ntest_a()\n\n\ndef test_b():\n    pass\n"
            ),
        }
    )
    head = repo.commit(IMPL2)
    plan = _plan(repo, base, head)
    assert "tests/test_r.py::test_r" in selected(plan)
    assert rules(plan, "tests/test_r.py::test_r") == {"dependency"}


def test_r26_setup_called_by_module_level_code_is_no_isolated_writer(repo):
    base = repo.commit(
        {
            **BASE,
            **ISOLATION,
            "tests/test_a.py": (
                "import unittest\n"
                "\n"
                "import pkg.impl\n"
                "import pkg.reg\n"
                "\n"
                "\n"
                "class TestA(unittest.TestCase):\n"
                "    def setUp(self):\n"
                "        pkg.reg.REG['k'] = pkg.impl.make()\n"
                "\n"
                "    def test_a(self):\n"
                "        pass\n"
            ),
            "tests/test_b.py": (
                "from tests.test_a import TestA\n"
                "\n"
                "TestA.setUp(TestA())\n"
                "\n"
                "\n"
                "def test_b():\n"
                "    pass\n"
            ),
        }
    )
    head = repo.commit(IMPL2)
    plan = _plan(repo, base, head)
    assert "tests/test_r.py::test_r" in selected(plan)


def test_r26_an_uncalled_test_stays_isolated(repo):
    """Guard (precision): a test nothing else refers to is isolated; what
    it stores is its own."""
    base = repo.commit(
        {
            **BASE,
            **ISOLATION,
            "tests/test_a.py": (
                "import pkg.impl\n"
                "import pkg.reg\n"
                "\n"
                "\n"
                "def test_a():\n"
                "    pkg.reg.REG['k'] = pkg.impl.make()\n"
            ),
        }
    )
    head = repo.commit(IMPL2)
    plan = _plan(repo, base, head)
    assert selected(plan) == {"tests/test_a.py::test_a"}


SERVER = {
    "pkg/server.py": "_state = None\n\n\ndef state():\n    return _state\n",
    "pkg/impl.py": "def make():\n    return 1\n",
    "tests/test_other.py": (
        "from pkg.server import state\n\n\ndef test_other():\n    assert state() == 1\n"
    ),
}


def test_r27_a_session_scoped_monkeypatch_override_stores_for_good(repo):
    """A conftest overrides ``monkeypatch`` with a session-scoped
    ``MonkeyPatch``; a session fixture stores through it; test_other reads
    the stored value later."""
    base = repo.commit(
        {
            **BASE,
            **SERVER,
            "tests/conftest.py": (
                "import pytest\n\nimport pkg.impl\nimport pkg.server\n\n\n"
                "@pytest.fixture(scope='session')\ndef monkeypatch():\n"
                "    mp = pytest.MonkeyPatch()\n    yield mp\n    mp.undo()\n\n\n"
                "@pytest.fixture(scope='session')\ndef init(monkeypatch):\n"
                "    monkeypatch.setattr(pkg.server, '_state', pkg.impl.make())\n"
            ),
            "tests/test_a.py": "def test_a(init):\n    pass\n",
        }
    )
    head = repo.commit({"pkg/impl.py": "def make():\n    return 2\n"})
    plan = _plan(repo, base, head)
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_other.py::test_other"}


def test_r27_pytests_monkeypatch_in_a_test_is_undone(repo):
    """Guard (precision): a test storing through pytest's own
    function-scoped ``monkeypatch`` leaves nothing for a later test."""
    base = repo.commit(
        {
            **BASE,
            **SERVER,
            "tests/test_a.py": (
                "import pkg.impl\n"
                "import pkg.server\n"
                "\n"
                "\n"
                "def test_a(monkeypatch):\n"
                "    monkeypatch.setattr(pkg.server, '_state', pkg.impl.make())\n"
            ),
        }
    )
    head = repo.commit({"pkg/impl.py": "def make():\n    return 2\n"})
    plan = _plan(repo, base, head)
    assert selected(plan) == {"tests/test_a.py::test_a"}


# --------------------------------------------------------------------------- R7

# R7: a docstring read through a value the reader does not name (a
# parameter, ``getdoc(fn)`` in a helper) or through ``mod.__doc__`` (an
# unresolved module attribute) selected nothing when the docstring changed.

DOC_MOD = "def f():\n    '''{0}'''\n"


@pytest.mark.parametrize(
    "helper",
    [
        "import inspect\n\n\ndef doc_of(fn):\n    return inspect.getdoc(fn)\n",
        "def doc_of(obj):\n    return obj.__doc__\n",
        "def doc_of(obj):\n    return getattr(obj, '__doc__')\n",
    ],
    ids=["getdoc", "attribute", "getattr"],
)
def test_r7_a_docstring_read_through_a_parameter(repo, helper):
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": DOC_MOD.format("hello"),
            "pkg/help.py": helper,
            "tests/test_a.py": (
                "from pkg.help import doc_of\n"
                "from pkg.mod import f\n"
                "\n"
                "\n"
                "def test_a():\n"
                "    assert doc_of(f) == 'hello'\n"
            ),
            "tests/test_other.py": (
                "from pkg.mod import f\n\n\ndef test_other():\n    assert f() is None\n"
            ),
        }
    )
    head = repo.commit({"pkg/mod.py": DOC_MOD.format("goodbye")})
    plan = _plan(repo, base, head)
    assert changes(plan) == {"pkg.mod.f": ("docstring_changed",)}
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert any(
        step.kind == "reads_docstring" for step in reason(plan, "tests/test_a.py::test_a").path
    )


@pytest.mark.parametrize(
    "test",
    [
        "import pkg.mod\n\n\ndef test_a():\n    assert pkg.mod.__doc__ == 'hello'\n",
        "from pkg import mod\n\n\ndef test_a():\n    assert mod.__doc__ == 'hello'\n",
    ],
    ids=["dotted", "from_import"],
)
def test_r7_a_module_docstring_read_as_a_module_attribute(repo, test):
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": "'''hello'''\n\n\ndef f():\n    return 1\n",
            "tests/test_a.py": test,
            "tests/test_other.py": (
                "from pkg.mod import f\n\n\ndef test_other():\n    assert f() == 1\n"
            ),
        }
    )
    head = repo.commit({"pkg/mod.py": "'''goodbye'''\n\n\ndef f():\n    return 1\n"})
    plan = _plan(repo, base, head)
    assert changes(plan) == {"pkg.mod": ("docstring_changed",)}
    assert selected(plan) == {"tests/test_a.py::test_a"}


def test_r7_a_decorator_reading_its_parameter_is_no_reader_of_every_docstring(repo):
    """Guard (precision): ``@doc`` reads ``f.__doc__`` of what it decorates,
    which ``doc_decorated`` covers. The docstring of an undecorated
    function elsewhere changing reaches no test through the decorator."""
    base = repo.commit(
        {
            **BASE,
            "pkg/deco.py": (
                "DOCS = {}\n\n\ndef doc(f):\n    DOCS[f.__name__] = f.__doc__\n    return f\n"
            ),
            "pkg/mod.py": "from pkg.deco import doc\n\n\n@doc\ndef g():\n    '''g'''\n",
            "pkg/other.py": "def h():\n    '''one'''\n",
            "tests/test_doc.py": (
                "from pkg.deco import DOCS\n"
                "import pkg.mod\n"
                "\n"
                "\n"
                "def test_doc():\n"
                "    assert DOCS['g'] == 'g'\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "def h():\n    '''two'''\n"})
    plan = _plan(repo, base, head)
    assert changes(plan) == {"pkg.other.h": ("docstring_changed",)}
    assert selected(plan) == set()
    # And the decorated function's own docstring still reaches the reader.
    head2 = repo.commit(
        {"pkg/mod.py": "from pkg.deco import doc\n\n\n@doc\ndef g():\n    '''changed'''\n"}
    )
    assert selected(_plan(repo, base, head2)) == {"tests/test_doc.py::test_doc"}


@needs_monitoring
def test_r7_evidence_reaches_a_reader_through_a_parameter(repo):
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": DOC_MOD.format("hello"),
            "tests/test_a.py": (
                "import inspect\n"
                "from pkg.mod import f\n"
                "\n"
                "\n"
                "def doc_of(fn):\n"
                "    return inspect.getdoc(fn)\n"
                "\n"
                "\n"
                "def test_a():\n"
                "    assert doc_of(f) == 'hello'\n"
            ),
            "tests/test_other.py": (
                "from pkg.mod import f\n\n\ndef test_other():\n    assert f() is None\n"
            ),
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/mod.py": DOC_MOD.format("goodbye")})
    plan = _plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {"tests/test_a.py::test_a"}


# --------------------------------------------------------------------------- R11

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
            **BASE,
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
    plan = _plan(repo, base, head)
    assert selected(plan) == {
        "tests/test_names.py::test_names",
        "tests/test_plugin.py::test_plugin",
    }
    assert rules(plan, "tests/test_names.py::test_names") == {"dynamic_reference"}


# --------------------------------------------------------------------------- R20

# R20: ``__init__``/``__new__`` run by class creation (the Enum family makes
# each member; a project metaclass may call ``cls()``) and consumed through
# ``Color.RED.code``: the explicit special methods got no edge from the
# class, and the constructor edge needs a bare class call.

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
            **BASE,
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
    plan = _plan(repo, base, head, [BENCH_RUN])
    assert len(changes(plan)) == 1 and all(c == ("body_changed",) for c in changes(plan).values())
    assert selected(plan) == {"tests/test_x.py::test_x", "bench.time_run"}
    assert rules(plan, "bench.time_run") == {"dependency"}


def test_r20_a_plain_class_s_init_is_not_run_at_creation(repo):
    """Guard (precision): a plain class's ``__init__`` body change reaches a
    reader of a class attribute through no edge."""
    mod = "class C:\n    TAG = 1\n\n    def __init__(self):\n        self.code = 1{0}\n"
    base = repo.commit(
        {
            **BASE,
            **ENUM_RUNNERS,
            "pkg/mod.py": mod.format(""),
            "pkg/api.py": "from pkg.mod import C\n\n\ndef run():\n    return C.TAG\n",
        }
    )
    head = repo.commit({"pkg/mod.py": mod.format(" + 1")})
    plan = _plan(repo, base, head, [BENCH_RUN])
    assert changes(plan) == {"pkg.mod.C.__init__": ("body_changed",)}
    assert selected(plan) == set()


# --------------------------------------------------------------------------- R21


def test_r21_dir_of_a_module_depends_on_its_dir_hook(repo):
    base = repo.commit(
        {
            **BASE,
            **ENUM_RUNNERS,
            "pkg/mod.py": "def __dir__():\n    return ['a']\n",
            "pkg/api.py": "from pkg import mod\n\n\ndef run():\n    return len(dir(mod))\n",
        }
    )
    head = repo.commit({"pkg/mod.py": "def __dir__():\n    return ['a', 'b']\n"})
    plan = _plan(repo, base, head, [BENCH_RUN])
    assert changes(plan) == {"pkg.mod.__dir__": ("body_changed",)}
    assert selected(plan) == {"tests/test_x.py::test_x", "bench.time_run"}
    assert "pkg.mod.__dir__" in path_ids(reason(plan, "tests/test_x.py::test_x"))

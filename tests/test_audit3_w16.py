"""Regression scenarios for W16-W19 (internal/audit.md, "Known after round
3"): what the use model of literal tables and module handles missed.

- W16: a module handed on hands on what its imports bind, not only its
  submodules (``m.core.TABLE`` through a handed-on ``m``), and the code
  handing it on depends on those symbols.
- W17: any object installed in ``sys.modules`` is what a later import of
  that name gets.
- W18: other ways to reach a module at run time: a function's or a frame's
  globals, ``gc``'s object lists, unpicklers and name resolvers, a
  ``module_from_spec`` copy.
- W19: a name resolves in its own scope (a local shadows a module-level
  import or a builtin).

Each rule that bounds something comes with the shape where the bound does
not hold, which still selects.
"""

from __future__ import annotations

import ast
import sys

import pytest

from diffcone.classify import SymbolChange
from diffcone.indexer.uses import _no_evaluate, scan_module
from diffcone.model import INSTALLED, MODULE, REFERENCES, Edge
from diffcone.planner import _Graph, _import_call_effects
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
TEST_RUN = "from pkg.core import run\n\n\ndef test_s():\n    run()\n"


def _table_plan(repo, extra, test=TEST_RUN):
    files = {
        "pkg/__init__.py": "",
        "pkg/handlers.py": HANDLERS,
        "pkg/core.py": CORE,
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
    dynamic reference again (its users selected)."""
    if reached:
        assert selected(plan) == REACHED
        assert rules(plan, "t::test_s") == {"dynamic_reference"}
    else:
        assert selected(plan) == set()


# ---------------------------------------------------------------- W16: static


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
    _check(_table_plan(repo, files), reached)


READER = (
    "def read(obj, names):\n    for name in names:\n        obj = getattr(obj, name)\n"
    "    return obj()\n"
)
OPS = "def add(a, b):\n    return a + b\n\n\ndef other():\n    return 1\n"


@pytest.mark.parametrize(
    "api, call",
    [
        ("from pkg.ops import other\n", "read(api, ['other'])"),
        ("from pkg import ops\n", "read(api, ['ops', 'other'])"),
        ("from pkg.ops import *\n", "read(api, ['other'])"),
    ],
    ids=["an imported function", "an imported module", "a star import"],
)
def test_w16_code_handing_a_module_on_depends_on_what_it_imports(repo, api, call):
    # W9 made the code handing a module on depend on its members; what the
    # module imports is as reachable (``read(api, ["ops", "other"])``).
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "pkg/api.py": api,
            "pkg/reader.py": READER,
            "tests/test_r.py": (
                f"from pkg import api\nfrom pkg.reader import read\n\n\ndef test_r():\n    {call}\n"
            ),
            "benchmarks/bench_r.py": (
                f"from pkg import api\nfrom pkg.reader import read\n\n\ndef time_r():\n    {call}\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": OPS.replace("return 1", "return 2")})
    targets = [
        py_target("t::test_r", "tests.test_r.test_r"),
        asv_target("bench.time_r", "benchmarks.bench_r.time_r"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_r", "bench.time_r"}
    assert rules(plan, "t::test_r") == {"dependency"}


# ---------------------------------------------------------------- W17: static

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
    plan = _table_plan(repo, {"pkg/fake.py": fake})
    _check(plan, "pkg.core" in install)


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


ALT = "def fn():\n    return 1\n"


@pytest.mark.parametrize(
    "compat, reached",
    [
        ("sys.modules['pkg.core'] = types.SimpleNamespace(run=alt.fn)\n", True),
        (
            "\n\ndef test_swap(monkeypatch):\n"
            "    fake = types.SimpleNamespace(run=alt.fn)\n"
            "    monkeypatch.setitem(sys.modules, 'pkg.core', fake)\n",
            False,
        ),
    ],
    ids=["for good", "for one test"],
)
def test_w17_an_import_gets_what_was_installed_for_good(repo, compat, reached):
    # Installed for good at import (of a module only another test imports),
    # pkg.core is the fake for every later import: its importers depend on
    # what the installing code put there. Installed for one test, it is gone
    # before the next.
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/handlers.py": HANDLERS,
            "pkg/core.py": CORE,
            "pkg/alt.py": ALT,
            "pkg/compat.py": f"import sys\nimport types\n\nfrom pkg import alt\n\n{compat}",
            "tests/test_a.py": "import pkg.compat  # noqa: F401\n\n\ndef test_a():\n    pass\n",
            "tests/test_b.py": TEST_RUN.replace("test_s", "test_b"),
            "benchmarks/bench_b.py": "from pkg.core import run\n\n\ndef time_b():\n    run()\n",
        }
    )
    head = repo.commit({"pkg/alt.py": ALT.replace("1", "2")})
    targets = [
        py_target("t::test_b", "tests.test_b.test_b"),
        asv_target("bench.time_b", "benchmarks.bench_b.time_b"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == ({"t::test_b", "bench.time_b"} if reached else set())


@pytest.mark.parametrize("detail", ["", INSTALLED])
def test_w17_a_module_does_not_run_what_is_installed_as_it(detail):
    # The edge from a module to the code installing something as it is a
    # dependency, not a call: a change to the module's import-time code does
    # not run the installer, so what the installer fills stays as it was.
    graph = _Graph()
    graph.add(Edge("pkg.core", "pkg.compat.install", REFERENCES, detail), ("base", "head"))
    graph.add(Edge("pkg.compat.REG", "pkg.compat.install", REFERENCES, "mutated_by"), ("head",))
    change = SymbolChange("pkg.core", MODULE, ("body_changed",), None, None)
    effects = _import_call_effects(graph, [change], {"pkg.core"})
    assert ("pkg.compat.REG" in effects) is (detail == "")


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
    _check(_table_plan(repo, files), reached)


# ---------------------------------------------------------------- W18: static

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
    _check(_table_plan(repo, {"pkg/poke.py": code}, test), reached)


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
    _check(_table_plan(repo, files), reached)


# ---------------------------------------------------------------- W19: static


def test_w19_a_function_local_import_does_not_shadow_a_module_level_table(repo):
    # ``other`` imports a TABLE of its own; ``mutate`` writes this module's.
    core = CORE + (
        "\n\ndef other():\n    from plugins.one import TABLE\n\n    return TABLE\n"
        "\n\ndef mutate():\n    TABLE['k'] = 'b'\n"
    )
    _check(_table_plan(repo, {"pkg/core.py": core}), True)


@pytest.mark.parametrize(
    "body, reached",
    [("return len(TABLE)", False), ("len = print\n    return len(TABLE)", True)],
    ids=["the builtin", "a local of that name"],
)
def test_w19_a_builtin_is_a_name_nothing_in_scope_binds(repo, body, reached):
    # A class elsewhere binding ``len`` does not make the builtin unknown; a
    # local of that name is no builtin, and may change what it is given.
    core = CORE + f"\n\nclass Shape:\n    len: int = 0\n\n\ndef size():\n    {body}\n"
    _check(_table_plan(repo, {"pkg/core.py": core}), reached)


def test_w19_a_parameter_shadows_a_module_level_import():
    code = (
        "from __future__ import annotations\n\nimport sys\n\n\n"
        "def merge(annotations, sys):\n    annotations.update(sys)\n    return annotations\n"
    )
    found = scan_module(ast.parse(code), "pkg.m", False, _no_evaluate)
    assert {r.target for r in found.records} == set()


# ---------------------------------------------------------------- evidence

evidence = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/handlers.py": "def a():\n    return {}\n",
    "pkg/core.py": (
        "from pkg import handlers\n\nTABLE = {'k': 'a'}\n\n\n"
        "def run():\n    return getattr(handlers, TABLE['k'], None)\n"
    ),
    "tests/__init__.py": "",
    # Runs after test_a, in the same session.
    "tests/test_s.py": "from pkg.core import run\n\n\ndef test_s():\n    assert run() is None\n",
}
WRITERS = {
    "a module handed on": (
        {
            "pkg/api.py": "from pkg import core  # noqa: F401\n",
            "pkg/mutate.py": "def mutate(m):\n    m.core.TABLE['k'] = 'c'\n",
        },
        "from pkg import api\nfrom pkg.mutate import mutate\n\n\ndef test_a():\n    mutate(api)\n",
    ),
    "a function's globals": (
        {"pkg/mutate.py": "def mutate(f):\n    f.__globals__['TABLE']['k'] = 'c'\n"},
        "from pkg.core import run\nfrom pkg.mutate import mutate\n\n\n"
        "def test_a():\n    mutate(run)\n",
    ),
}


@evidence
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_w16_w18_a_table_written_elsewhere_in_evidence_mode(repo, writer):
    # test_a points pkg.core.TABLE at ``c``, which handlers lacks at C; the
    # change adds it. test_s runs after test_a and now gets 1: its lookup
    # must see the added name.
    files, test_a = WRITERS[writer]
    base = repo.commit({**BASE, **files, "tests/test_a.py": test_a})
    ev = repo.collect()
    head = repo.commit(
        {"pkg/handlers.py": BASE["pkg/handlers.py"] + "\n\ndef c():\n    return 1\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert "tests/test_s.py::test_s" in selected(plan)
    assert "lookup_site" in rules(plan, "tests/test_s.py::test_s")

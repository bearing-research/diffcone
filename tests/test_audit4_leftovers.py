"""Regression scenarios for round 4 of the pre-release audit: the leftovers
the batch agents found outside their files (L1 to L3).

Each test names the finding it guards, and each failed before its fix.
"""

from __future__ import annotations

import json
import sys

import pytest

from diffcone.cache import IndexCache
from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import build_index
from diffcone.model import IMPORTS, SnapshotInfo
from diffcone.snapshot import Snapshot, read_snapshot
from diffcone.testing import asv_target, selected

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'
BENCH = {
    "asv.conf.json": ASV_CONF,
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}
BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
TESTS = {"tests/__init__.py": "", "tests/test_ok.py": "def test_ok():\n    pass\n"}

# ---------------------------------------------------------------------- L1
# A module too deeply nested for discovery's recursive walks (ast.unparse
# of a test name bound to a long attribute chain, ast.parse itself on a
# longer one) raised RecursionError out of discovery: no plan at all. It is
# now left out as a file that does not parse (``unparsed_file``, so
# discovery is incomplete), and such a result is not cached.

CHAIN = "import a\n\n{0} = a" + ".b" * 1000 + "\n"
LONG_CHAIN = "import a\n\nX = a" + ".b" * 100_000 + "\n"


def _discover(repo, rev, runner, **options):
    snap = read_snapshot(repo.path, rev, ["."], with_config=True)
    return discover(runner, snap, build_index(snap), DiscoveryOptions(**options))


def _unparsed(result) -> set[str]:
    return {n.detail for n in result.notes if n.kind == "unparsed_file"}


@pytest.mark.parametrize(
    "module",
    [
        CHAIN.format("test_x"),
        "import a\n\n\nclass TestX:\n" + CHAIN.format("    test_y").split("\n\n", 1)[1],
        LONG_CHAIN,
    ],
    ids=["module_binding", "class_binding", "parse"],
)
def test_l1_a_too_deep_test_module_leaves_discovery_incomplete(repo, tmp_path, module):
    base = repo.commit({**BENCH, **TESTS, "tests/test_x.py": module})
    head = repo.commit({"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert {(n.kind, n.path) for n in plan.incomplete_discovery} == {
        ("unparsed_file", "tests/test_x.py")
    }
    assert any("too deeply nested" in n.detail for n in plan.incomplete_discovery)
    assert {"tests/test_ok.py::test_ok", "bench.time_noop"} <= selected(plan)
    # Running out of stack depends on the caller: not cached.
    cache = IndexCache(tmp_path / "cache")
    repo.plan(base, head, [], discover_runners=["pytest", "asv"], cache=cache)
    stored = [json.loads(p.read_text()) for p in (tmp_path / "cache" / "discovery").glob("*")]
    assert sorted(d["runner"] for d in stored) == ["asv", "asv"]


def test_l1_a_too_deep_conftest_and_benchmark_module(repo):
    rev = repo.commit(
        {
            **BENCH,
            **TESTS,
            "tests/conftest.py": LONG_CHAIN,
            "benchmarks/deep.py": LONG_CHAIN + "\n\ndef time_x():\n    pass\n",
        }
    )
    found = _discover(repo, rev, "pytest")
    assert {n.path for n in found.notes if n.kind == "unparsed_file"} == {"tests/conftest.py"}
    assert {t.runner_id for t in found.targets} == {"tests/test_ok.py::test_ok"}
    found = _discover(repo, rev, "asv")
    assert _unparsed(found) == {"benchmarks/deep.py: too deeply nested to analyse"}
    assert {t.runner_id for t in found.targets} == {"bench.time_noop"}


def test_l1_a_too_deep_doctest_or_timeraw_snippet_is_unread(repo):
    """Code in a string (a doctest example, the code a ``timeraw_``
    benchmark returns) is parsed on its own: too deep, it is code that
    cannot be read, as a syntax error is."""
    snippet = "x = a" + ".b" * 100_000
    rev = repo.commit(
        {
            **BENCH,
            "pkg/__init__.py": "",
            "pkg/mod.py": f"def f():\n    '''\n    >>> {snippet}\n    '''\n",
            "benchmarks/raw.py": f"def timeraw_x():\n    return {snippet!r}\n",
        }
    )
    found = _discover(repo, rev, "pytest", runner_args=("--doctest-modules",))
    target = next(t for t in found.targets if t.runner_id.endswith("pkg.mod.f"))
    assert "doctest:unparsed" in target.lifecycle_dependencies
    found = _discover(repo, rev, "asv")
    raw = next(t for t in found.targets if t.runner_id == "raw.timeraw_x")
    assert any("timeraw" in d for d in raw.lifecycle_dependencies)


# ---------------------------------------------------------------------- L2
# The reference collector read a callee or a stored-to chain through one
# import binding only (the last of an import in ``try`` and another in
# ``except``, the primary of a function-local one) and never through a star
# import, so a write to ``os.environ`` through any other binding was no
# process write: a test that ran after it was not selected. Every name the
# chain may stand for is considered now; where recognising a name leaves
# something out (``with catch_warnings():`` puts the filters back), every
# name must be it.

TEST_B = "def test_b():\n    pass\n"
WRITE = "environ['MODE'] = '{0}'\n\n\ndef test_a():\n    pass\n"


@pytest.mark.parametrize(
    "module",
    [
        "from os import *\n\n" + WRITE,
        "try:\n    from os import environ\nexcept ImportError:\n    from compat import environ\n\n"
        + WRITE,
        "def _set(v):\n    try:\n        from os import environ\n    except ImportError:\n"
        "        from compat import environ\n    environ['MODE'] = v\n\n\n_set('{0}')\n\n\n"
        "def test_a():\n    pass\n",
    ],
    ids=["star", "alt_import", "local_alt_import"],
)
def test_l2_a_process_write_through_any_binding_reaches_every_test(repo, module):
    files = {
        "tests/__init__.py": "",
        "tests/test_a.py": module.format("x"),
        "tests/test_b.py": TEST_B,
    }
    base = repo.commit({**BENCH, **files})
    head = repo.commit({"tests/test_a.py": module.format("y")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_l2_a_with_that_may_not_be_catch_warnings_restores_nothing(repo):
    module = (
        "import warnings\n\ntry:\n    from compat import nothing as catch_warnings\n"
        "except ImportError:\n    from warnings import catch_warnings\n\n"
        "with catch_warnings():\n    warnings.filterwarnings('{0}')\n\n\n"
        "def test_a():\n    pass\n"
    )
    files = {
        "tests/__init__.py": "",
        "tests/test_a.py": module.format("ignore"),
        "tests/test_b.py": TEST_B,
    }
    base = repo.commit({**BENCH, **files})
    head = repo.commit({"tests/test_a.py": module.format("error")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


# ---------------------------------------------------------------------- L3
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
    imported = {e.target for e in edges if e.source == "tests.test_a.test_a" and e.kind == IMPORTS}
    assert "pkg.fast" in imported


# ---------------------------------------------------------------------- L4
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


# ---------------------------------------------------------------------- L6
# A change to a module-level variable's object made through a module object
# other than a name pass 2 resolves (``import_module("pkg.reg").TABLE["x"]
# = f``, ``sys.modules[...]``, ``__import__``, a function-local import, a
# module passed in or returned, ``getattr`` on a package, a star import) was
# no ``mutated_by`` dependency: the code reading the variable did not see the
# writer, so a change to what it stored there selected nothing. The use
# records (indexer/uses.py) now give one, for every write form (an item
# assigned or deleted, an augmented assignment, a mutator, an attribute of
# what it holds, an item changed on the way, the variable handed to code
# that may change it), and for a module found at run time or an object from
# anywhere, every variable it may be (by name, or any).

REG = (
    "def a():\n    return 1\n\n\ndef b():\n    return {0}\n\n\nTABLE = {{'a': a}}\n"
    "LIST = [a]\nNEST = {{'k': [a]}}\n\n\nclass O:\n    x = a\n\n\nOBJ = O()\n"
)
READERS = {
    "TABLE": "def read():\n    return TABLE['x']()\n",
    "LIST": "def read():\n    return LIST[-1]()\n",
    "NEST": "def read():\n    return NEST['k'][-1]()\n",
    "OBJ": "def read():\n    return OBJ.x()\n",
}
GETS = {
    "import_module": ("import importlib", "importlib.import_module('pkg.reg')"),
    "sys_modules": ("import sys", "sys.modules['pkg.reg']"),
    "sys_modules_get": ("import sys", "sys.modules.get('pkg.reg')"),
    "dunder_import": ("", "__import__('pkg.reg', fromlist=['x'])"),
    "getattr_package": ("import pkg", "getattr(pkg, 'reg')"),
    "star_import_module": ("from importlib import *", "import_module('pkg.reg')"),
    "unpickled": ("import pickle", "pickle.loads(DATA)"),
    "import_any_name": ("import importlib", "importlib.import_module(NAME)"),
    "gc_object": ("import gc", "gc.get_objects()[0]"),
}
WRITES = {
    "item": ("TABLE", "{m}.TABLE['x'] = pkg.reg.b"),
    "update": ("TABLE", "{m}.TABLE.update(x=pkg.reg.b)"),
    "dunder_setitem": ("TABLE", "{m}.TABLE.__setitem__('x', pkg.reg.b)"),
    "augassign": ("TABLE", "{m}.TABLE |= {{'x': pkg.reg.b}}"),
    "append": ("LIST", "{m}.LIST.append(pkg.reg.b)"),
    "iadd": ("LIST", "{m}.LIST += [pkg.reg.b]"),
    "slice": ("LIST", "{m}.LIST[0:0] = [pkg.reg.b]"),
    "item_on_the_way": ("NEST", "{m}.NEST['k'].append(pkg.reg.b)"),
    "item_iadd": ("NEST", "{m}.NEST['k'] += [pkg.reg.b]"),
    "attribute": ("OBJ", "{m}.OBJ.x = pkg.reg.b"),
    "setattr": ("OBJ", "setattr({m}.OBJ, 'x', pkg.reg.b)"),
    "handed_on": ("TABLE", "_put({m}.TABLE)"),
}


def _l6_writer(imports: str, body: str) -> str:
    return (
        f"{imports}\n\nimport pkg.reg\n\n\ndef _put(t):\n    t['x'] = pkg.reg.b\n\n\n"
        f"def go(DATA=b'', NAME='x'):\n{body}"
    )


def _l6_plan(repo, variable: str, writer: str):
    files = {
        **BENCH,
        "pkg/__init__.py": "",
        "pkg/reg.py": REG.format("1"),
        "pkg/use.py": f"from pkg.reg import {variable}\n\n\n{READERS[variable]}",
        "pkg/w.py": writer,
        "tests/__init__.py": "",
        "tests/test_a.py": "import pkg.w\n\n\ndef test_a():\n    pkg.w.go()\n",
        "tests/test_b.py": "from pkg.use import read\n\n\ndef test_b():\n    assert read() == 2\n",
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/reg.py": REG.format("2")})
    return repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])


BOTH = {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


@pytest.mark.parametrize("get", sorted(GETS))
def test_l6_an_item_stored_through_any_module_object_reaches_the_readers(repo, get):
    imports, expr = GETS[get]
    variable, write = WRITES["item"]
    plan = _l6_plan(repo, variable, _l6_writer(imports, f"    {write.format(m=expr)}\n"))
    assert selected(plan) == BOTH


@pytest.mark.parametrize("write", sorted(WRITES))
def test_l6_every_write_form_through_a_handle_reaches_the_readers(repo, write):
    variable, text = WRITES[write]
    imports, expr = GETS["import_module"]
    plan = _l6_plan(repo, variable, _l6_writer(imports, f"    {text.format(m=expr)}\n"))
    assert selected(plan) == BOTH


@pytest.mark.parametrize(
    "body",
    [
        "    import pkg.reg as m\n    m.TABLE['x'] = pkg.reg.b\n",
        "    m = importlib.import_module('pkg.reg')\n    m.TABLE['x'] = pkg.reg.b\n",
        "    _store(pkg.reg)\n\n\ndef _store(m):\n    m.TABLE['x'] = pkg.reg.b\n",
        "    _store(sys.modules['pkg.reg'])\n\n\ndef _store(m):\n    m.TABLE['x'] = pkg.reg.b\n",
        "    _get().TABLE['x'] = pkg.reg.b\n\n\ndef _get():\n"
        "    return importlib.import_module('pkg.reg')\n",
    ],
    ids=["local_import", "local_handle", "module_passed", "handle_passed", "handle_returned"],
)
def test_l6_a_module_object_however_held_reaches_the_readers(repo, body):
    plan = _l6_plan(repo, "TABLE", _l6_writer("import importlib\nimport sys", body))
    assert selected(plan) == BOTH


@pytest.mark.parametrize(
    "body",
    [
        "    gc.get_objects()[0]['x'] = pkg.reg.b\n",
        "    pickle.loads(DATA)['x'] = pkg.reg.b\n",
        "    for o in gc.get_objects():\n        o['x'] = pkg.reg.b\n",
    ],
    ids=["gc_item", "unpickled_item", "gc_loop"],
)
def test_l6_an_object_from_anywhere_changed_may_be_any_variable(repo, body):
    """An object nothing names (a ``gc`` list's element, an unpickled one)
    may be any module's variable: changing it reaches every variable's
    readers (the unbounded case)."""
    plan = _l6_plan(repo, "TABLE", _l6_writer("import gc\nimport pickle", body))
    assert selected(plan) == BOTH


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_l6_evidence_a_changed_writer_through_a_handle_selects_the_readers(repo):
    """The recorded run of ``test_b`` executed ``read`` only; ``go`` (run by
    ``test_a`` first) put ``b`` in the table through a handle. Changing
    ``go`` to put ``c`` there changes ``test_b``'s outcome."""
    reg = REG.format("2") + "\n\ndef c():\n    return 3\n"
    writer = (
        "import importlib\n\nimport pkg.reg\n\n\ndef go():\n"
        "    importlib.import_module('pkg.reg').TABLE['x'] = pkg.reg.{0}\n"
    )
    files = {
        ".gitignore": "__pycache__/\n.diffcone/\n",
        "pkg/__init__.py": "",
        "pkg/reg.py": reg,
        "pkg/use.py": "from pkg.reg import TABLE\n\n\n" + READERS["TABLE"],
        "pkg/w.py": writer.format("b"),
        "tests/__init__.py": "",
        "tests/test_a.py": "import pkg.w\n\n\ndef test_a():\n    pkg.w.go()\n",
        "tests/test_b.py": (
            "from pkg.use import read\n\n\ndef test_b():\n    assert read() in (2, 3)\n"
        ),
    }
    base = repo.commit(files)
    evidence = repo.collect()
    head = repo.commit({"pkg/w.py": writer.format("c")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == BOTH


# A test or function-scoped fixture that saves a module attribute, stores
# into it and puts it back (``orig = m.X; m.X = v; try: yield finally: m.X =
# orig``) leaves nothing for later tests, as ``monkeypatch.setattr`` does:
# no ``mutated_by`` dependency of the attribute's readers. When the shape
# does not guarantee the restore (none, code that may raise before it, a
# wider scope), the store lasts.

STATE_FILES = {
    "pkg/__init__.py": "",
    "pkg/state.py": "STATE = None\n\n\ndef get():\n    return STATE\n",
    "pkg/impl.py": "def make():\n    return {0}\n",
    "tests/__init__.py": "",
    "tests/test_b.py": "from pkg.state import get\n\n\ndef test_b():\n    assert get() is None\n",
}
SAVE = "    orig = m.STATE\n    m.STATE = pkg.impl.make()\n"


def _restore_plan(repo, test_a: str):
    files = {**BENCH, **STATE_FILES, "tests/test_a.py": test_a}
    base = repo.commit({k: v.replace("{0}", "1") for k, v in files.items()})
    head = repo.commit({"pkg/impl.py": STATE_FILES["pkg/impl.py"].replace("{0}", "2")})
    return repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])


FIXTURE = "import pytest\n\nimport pkg.impl\n\n\n@pytest.fixture{scope}\ndef client():\n{body}"
TEST_A = "\n\ndef test_a(client):\n    pass\n"
LOCAL = "    import pkg.state as m\n\n"


@pytest.mark.parametrize(
    "test_a",
    [
        FIXTURE.format(
            scope="",
            body=LOCAL + SAVE + "    try:\n        yield 1\n    finally:\n        m.STATE = orig\n",
        )
        + TEST_A,
        FIXTURE.format(scope="", body=LOCAL + SAVE + "    yield\n    m.STATE = orig\n") + TEST_A,
        FIXTURE.format(
            scope="(autouse=True)",
            body=LOCAL + SAVE + "    try:\n        yield client_of()\n    finally:\n"
            "        m.STATE = orig\n        cleanup()\n",
        )
        + TEST_A,
        "import pkg.impl\nimport pkg.state as m\n\n\ndef test_a():\n"
        + SAVE
        + "    try:\n        assert m.STATE\n    finally:\n        m.STATE = orig\n",
    ],
    ids=["fixture_try_finally", "fixture_plain_yield", "fixture_finally_first", "test_try_finally"],
)
def test_l6_a_store_put_back_before_the_test_ends_lasts_for_no_later_test(repo, test_a):
    plan = _restore_plan(repo, test_a)
    assert selected(plan) == {"tests/test_a.py::test_a"}


@pytest.mark.parametrize(
    "test_a",
    [
        FIXTURE.format(scope="", body=LOCAL + SAVE + "    yield 1\n") + TEST_A,
        FIXTURE.format(
            scope="", body=LOCAL + SAVE + "    yield 1\n    cleanup()\n    m.STATE = orig\n"
        )
        + TEST_A,
        FIXTURE.format(scope="", body=LOCAL + SAVE + "    yield client_of()\n    m.STATE = orig\n")
        + TEST_A,
        FIXTURE.format(
            scope="(scope='module')",
            body=LOCAL + SAVE + "    try:\n        yield 1\n    finally:\n        m.STATE = orig\n",
        )
        + TEST_A,
        FIXTURE.format(
            scope="",
            body=LOCAL + SAVE + "    m.OTHER = pkg.impl.make()\n    try:\n        yield 1\n"
            "    finally:\n        m.STATE = orig\n",
        )
        + TEST_A,
        "import pkg.impl\nimport pkg.state as m\n\n\ndef helper():\n"
        + SAVE
        + "    try:\n        pass\n    finally:\n        m.STATE = orig\n\n\n"
        "def test_a():\n    helper()\n",
    ],
    ids=[
        "not_put_back",
        "code_before_the_restore",
        "yield_value_may_raise",
        "module_scope",
        "second_store_may_raise",
        "not_a_test",
    ],
)
def test_l6_a_store_not_surely_put_back_lasts(repo, test_a, request):
    """A store not surely put back lasts. Made by code that runs only inside
    the test (a function-scoped fixture, a helper only the test calls), it
    lasts for that test alone under the isolation assumption (L7); made by
    a module-scoped fixture, it lasts for every later test."""
    plan = _restore_plan(repo, test_a)
    if request.node.callspec.id == "module_scope":
        assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    else:
        assert selected(plan) == {"tests/test_a.py::test_a"}


# ---------------------------------------------------------------------- L7
# The isolation assumption (the user's decision): a store into module state
# made by code that runs only inside one test (the test, a function-scoped
# fixture, a helper of the test module only tests call) is seen by that test
# alone, so it is no dependency of the readers in other tests or benchmarks.
# Code that runs at collection (a test module's top level), a wider-scoped
# fixture, and project code (a cache filled from a test) stay writers.

ISO = {
    "pkg/__init__.py": "",
    "pkg/server.py": "_state = None\n\n\ndef state():\n    return _state\n",
    "pkg/impl.py": "def make():\n    return {0}\n",
    "pkg/cache.py": (
        "import pkg.impl\n\n_CACHE = {}\n\n\ndef fill():\n    _CACHE['k'] = pkg.impl.make()\n\n\n"
        "def get():\n    return _CACHE.get('k')\n"
    ),
    "tests/__init__.py": "",
    "tests/test_other.py": (
        "from pkg.server import state\n\n\ndef test_other():\n    assert state() is None\n"
    ),
    "tests/test_reads_cache.py": (
        "from pkg.cache import get\n\n\ndef test_reads_cache():\n    assert get() is None\n"
    ),
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.server import state\n\n\ndef time_state():\n    state()\n",
}
ISO_BENCH = asv_target("bench.time_state", "benchmarks.bench.time_state")
ISO_HEAD = "import pytest\n\nimport pkg.cache\nimport pkg.impl\nimport pkg.server\n\n\n"
OTHERS = {"tests/test_other.py::test_other", "bench.time_state"}


def _iso_plan(repo, test_a: str, evidence: bool = False):
    files = {**ISO, "tests/test_a.py": test_a}
    if evidence:
        files[".gitignore"] = "__pycache__/\n.diffcone/\n"
    base = repo.commit({**files, "pkg/impl.py": ISO["pkg/impl.py"].format("1")})
    recorded = repo.collect() if evidence else None
    head = repo.commit({"pkg/impl.py": ISO["pkg/impl.py"].format("2")})
    return repo.plan(base, head, [ISO_BENCH], discover_runners=["pytest"], evidence=recorded)


@pytest.mark.parametrize(
    "test_a",
    [
        ISO_HEAD + "def _put():\n    pkg.server._state = pkg.impl.make()\n\n\n"
        "def test_a():\n    _put()\n",
        ISO_HEAD + "def test_a():\n    pkg.server._state = pkg.impl.make()\n",
        ISO_HEAD + "@pytest.fixture\ndef put():\n    pkg.server._state = pkg.impl.make()\n\n\n"
        "def test_a(put):\n    pass\n",
        ISO_HEAD
        + "class TestA:\n    def _put(self):\n        pkg.server._state = pkg.impl.make()\n\n"
        "    def test_a(self):\n        self._put()\n",
    ],
    ids=["helper", "test", "function_fixture", "method_helper"],
)
def test_l7_a_store_by_isolated_test_code_reaches_only_its_test(repo, test_a):
    plan = _iso_plan(repo, test_a)
    selected_ids = selected(plan)
    assert {t for t in selected_ids if "test_a" in t} and not selected_ids & OTHERS


@pytest.mark.parametrize(
    "test_a",
    [
        ISO_HEAD + "pkg.server._state = pkg.impl.make()\n\n\ndef test_a():\n    pass\n",
        ISO_HEAD + "@pytest.fixture(scope='session')\ndef put():\n"
        "    pkg.server._state = pkg.impl.make()\n\n\ndef test_a(put):\n    pass\n",
        ISO_HEAD + "SCOPE = 'function'\n\n\n@pytest.fixture(scope=SCOPE)\ndef put():\n"
        "    pkg.server._state = pkg.impl.make()\n\n\ndef test_a(put):\n    pass\n",
        ISO_HEAD + "def _put():\n    pkg.server._state = pkg.impl.make()\n\n\n_put()\n\n\n"
        "def test_a():\n    _put()\n",
    ],
    ids=["module_level", "session_fixture", "computed_scope", "helper_also_at_module_level"],
)
def test_l7_a_store_by_code_that_outlives_the_test_reaches_every_reader(repo, test_a):
    plan = _iso_plan(repo, test_a)
    assert OTHERS <= selected(plan)


def test_l7_a_project_cache_filled_from_a_test_still_reaches_its_readers(repo):
    plan = _iso_plan(repo, ISO_HEAD + "def test_a():\n    pkg.cache.fill()\n")
    assert "tests/test_reads_cache.py::test_reads_cache" in selected(plan)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_l7_evidence_a_store_by_a_test_helper_reaches_only_its_test(repo):
    test_a = (
        ISO_HEAD
        + "def _put():\n    pkg.server._state = pkg.impl.make()\n\n\n"
        + ("def test_a():\n    _put()\n    pkg.server._state = None\n")
    )
    plan = _iso_plan(repo, test_a, evidence=True)
    assert "tests/test_a.py::test_a" in selected(plan)
    assert "tests/test_other.py::test_other" not in selected(plan)

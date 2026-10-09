"""Regression scenarios for the third audit round, planning from execution
evidence (internal/audit.md, round 3, EVP).

Each test names the finding it guards and failed before its fix. Evidence is
recorded for real in a fixture repository (``diffcone collect`` runs its
suite under the recorder); assertions check exact target sets.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from diffcone.cli import main
from diffcone.cython import cython_changes, read
from diffcone.evidence import FLAG_UNSTABLE, advance, list_stores, load_store
from diffcone.report import to_text
from diffcone.testing import asv_target, selected

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _plan(repo, base, head, evidence, targets=()):
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], evidence=evidence)


# --------------------------------------------------------------------------- EVP-7


CYTHON_QUERY = '''\
def query():
    return """
SELECT a
# not a comment: part of the string
FROM t
"""
'''


@pytest.mark.parametrize(
    "edit",
    [
        ("# not a comment: part of the string", "# changed text"),
        ("SELECT a\n", "SELECT a\n\n"),
        ("# not a comment: part of the string\n", ""),
        ("FROM t\n", "FROM t   \n"),
    ],
    ids=["hash line edited", "blank line added", "hash line deleted", "trailing space"],
)
def test_evp7_cython_lines_inside_a_string_are_part_of_the_body(edit):
    after = CYTHON_QUERY.replace(*edit)
    changes = cython_changes(
        {"m.pyx": read("m.pyx", CYTHON_QUERY)}, {"m.pyx": read("m.pyx", after)}
    )
    assert changes.functions == (("m.pyx", "query"),)
    # Outside every function too (the hash used when tokenize cannot read
    # the file).
    outside = "X = '''\n# a\n'''\n"
    edited = outside.replace("# a", "")
    assert read("m.pyx", outside).outside_hash != read("m.pyx", edited).outside_hash


def test_evp7_a_hash_inside_a_cython_string_hides_no_name():
    # ``#`` inside a one-line string is not a comment: what follows is still
    # mentioned (a nogil callee is found through the functions naming it).
    module = read("m.pyx", 'cdef int f():\n    s = "#"; return g()\n')
    (function,) = module.functions
    assert "g" in function.names


# --------------------------------------------------------------------------- EVP-9


def test_evp9_plan_says_the_environment_was_not_checked(repo):
    repo.commit(
        {
            **BASE,
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_ops.py": "from pkg.ops import add\n\n\ndef test_add():\n"
            "    assert add(1, 2) == 3\n",
        }
    )
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    plan = _plan(repo, ev.commit, head, ev)
    assert plan.evidence["environment_checked"] is False
    assert "environment NOT checked" in to_text(plan)
    # ``run`` checks it in the test process, and says so in its report.
    out = repo.path.parent / "ran.json"
    args = ["run", "--repo", str(repo.path), "--base", ev.commit, "--head", head]
    args += ["--discover", "pytest", "--command", f"{sys.executable} -m pytest"]
    assert main([*args, "--evidence", "auto", "--no-cache", "-o", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["analysis"]["evidence"]["environment_checked"] is True


# --------------------------------------------------------------------------- EVP-4

CACHED_LIB = """\
_STATE = {}


def compute():
    return 42


def get():
    if "v" not in _STATE:
        _STATE["v"] = compute()
    return _STATE["v"]
"""

PASSING = "def test_a():\n    pass\n"

USES_GET = """\
from pkg.lib import get


def test_m1():
    assert get() == 42


def test_m2():
    assert get() == 42
"""


def _run_collect(repo, base, head):
    args = ["run", "--repo", str(repo.path), "--base", base, "--head", head]
    args += ["--discover", "pytest", "--command", f"{sys.executable} -m pytest"]
    return main(args + ["--evidence", "auto", "--collect", "--no-cache"])


def test_evp4_run_collect_order_checks_fresh_records(repo, capfd):
    """A reverse-checked recording advanced by ``run --collect``: the fresh
    records are checked in reverse order too. ``test_m2`` reads the value
    ``test_m1`` cached, so its forward record lacks ``compute``; in reverse
    it is the one that computes, so both are unstable."""
    c0 = repo.commit({**BASE, "pkg/lib.py": CACHED_LIB, "tests/test_a.py": PASSING})
    ev0 = repo.collect(reverse_check=True)
    assert ev0.reverse_checked
    c1 = repo.commit({"tests/test_m.py": USES_GET})
    assert _run_collect(repo, c0, c1) == 0
    assert "running the selected tests again in reverse order" in capfd.readouterr().err
    head1 = repo.git("rev-parse", c1).strip()
    (info,) = [s for s in list_stores(repo.path) if s.commit == head1]
    ev1 = load_store(info.path)
    assert ev1.reverse_checked
    unstable = {t for t, r in ev1.tests.items() if r.flags & FLAG_UNSTABLE}
    assert unstable == {"tests/test_m.py::test_m1", "tests/test_m.py::test_m2"}
    c2 = repo.commit({"pkg/lib.py": CACHED_LIB.replace("return 42", "return 41")})
    bench = asv_target("benchmarks.TimeGet.time_get", "pkg.lib.get")
    plan = _plan(repo, c1, c2, ev1, [bench])
    assert selected(plan) == {
        "tests/test_m.py::test_m1",
        "tests/test_m.py::test_m2",
        "benchmarks.TimeGet.time_get",
    }


def test_evp4_advance_marks_unchecked_fresh_records_unstable(repo):
    """``advance`` itself: fresh records from a run in one order, onto a
    reverse-checked recording, are unstable, so the store's claim holds."""
    a = "tests/test_a.py::test_a"
    repo.commit({**BASE, "pkg/lib.py": CACHED_LIB, "tests/test_a.py": PASSING})
    checked = repo.collect(reverse_check=True)
    assert not checked.tests[a].flags & FLAG_UNSTABLE
    fresh = repo.collect()
    assert not fresh.reverse_checked
    advanced = advance(checked, fresh, {a}, checked.commit)
    assert advanced.reverse_checked
    assert advanced.tests[a].flags & FLAG_UNSTABLE
    # Onto a recording that was never order-checked, nothing is claimed.
    plain = advance(fresh, fresh, {a}, fresh.commit)
    assert not plain.reverse_checked
    assert not plain.tests[a].flags & FLAG_UNSTABLE


# --------------------------------------------------------------------------- EVP-2, EVP-6

STATE = """\
MODE = ["slow"]


def set_mode(m):
    _store(m)
    return m


def _store(m):
    MODE[0] = m


def mode():
    return MODE[0]
"""

READS_MODE = """\
from pkg.state import mode


def test_b():
    assert mode() == "slow"
"""

IMPORTS_CONFIG = "import pkg.config  # noqa: F401\n\n\ndef test_a():\n    pass\n"
A, B = "tests/test_a.py::test_a", "tests/test_b.py::test_b"
# Static planning reaches a benchmark of the changed function itself (the static
# side of EVP-2 and EVP-6, reaching the readers of what it writes, is not
# evidence planning's: ASV targets keep their static decision).
BENCH = asv_target("benchmarks.TimeMode.time_set_mode", "pkg.state.set_mode")


@pytest.mark.parametrize("where", ["library", "conftest", "class"])
def test_evp2_code_run_at_import_writes_state_others_read(repo, where):
    """``set_mode`` runs only while another module is imported, and writes
    ``MODE`` (through a helper) in a third: a change to its body reaches the
    tests reading ``MODE``, which never ran it."""
    files = {**BASE, "pkg/state.py": STATE, "tests/test_b.py": READS_MODE}
    if where == "library":
        files["pkg/config.py"] = 'from pkg.state import set_mode\n\nX = set_mode("slow")\n'
        files["tests/test_a.py"] = IMPORTS_CONFIG
    elif where == "class":
        # Calling a class runs its constructor.
        files["pkg/config.py"] = (
            "from pkg.state import set_mode\n\n\nclass Mode:\n    def __init__(self, m):\n"
            "        set_mode(m)\n\n\nX = Mode('slow')\n"
        )
        files["tests/test_a.py"] = IMPORTS_CONFIG
    else:
        files["tests/conftest.py"] = 'from pkg.state import set_mode\n\nset_mode("slow")\n'
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"pkg/state.py": STATE.replace("    _store(m)\n", "    _store(m + '!')\n")})
    plan = _plan(repo, base, head, ev, [BENCH])
    want = {B, BENCH.runner_id} | ({A} if where != "conftest" else set())
    assert selected(plan) == want


IMPORT_CALLS = {
    "library variable": ("pkg/config.py", "from pkg.state import set_mode\n\nX = {}\n", True),
    "library class attribute": (
        "pkg/config.py",
        "from pkg.state import set_mode\n\n\nclass K:\n    x = {}\n",
        True,
    ),
    "test decorator": (
        "tests/test_a.py",
        "import pytest\n\nfrom pkg.state import set_mode\n\n\n"
        '@pytest.mark.parametrize("x", [{}])\ndef test_a(x):\n    pass\n',
        False,
    ),
    "test module variable": (
        "tests/test_a.py",
        "from pkg.state import set_mode\n\nX = {}\n\n\ndef test_a():\n    pass\n",
        False,
    ),
}


@pytest.mark.parametrize("case", [*IMPORT_CALLS, "added test"])
def test_evp6_an_import_time_call_with_a_changed_argument(repo, case):
    """What an import-time call writes is read by tests that never ran it:
    changing its argument, or adding a test whose decorator makes the call,
    reaches them."""
    old, new = 'set_mode("slow")', 'set_mode("faster")'
    if case == "added test":
        path = "tests/test_a.py"
        text = "import pytest\n\nfrom pkg.state import set_mode\n\n\ndef test_a():\n    pass\n"
        extra = False
        after = text + f'\n\n@pytest.mark.parametrize("x", [{new}])\ndef test_new(x):\n    pass\n'
    else:
        path, template, extra = IMPORT_CALLS[case]
        text, after = template.format(old), template.format(new)
    files = {**BASE, "pkg/state.py": STATE, "tests/test_b.py": READS_MODE, path: text}
    if extra:
        files["tests/test_a.py"] = IMPORTS_CONFIG
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({path: after})
    plan = _plan(repo, base, head, ev)
    want = {A, B} | ({"tests/test_a.py::test_new"} if case == "added test" else set())
    assert selected(plan) == want


CYTHON_STATE = """\
# cython: profile=True
MODE = ["slow"]


def set_mode(m):
    MODE[0] = m
    return m


def mode():
    return MODE[0]
"""


def test_evp2_cython_code_run_at_import_writes_state_others_read(repo):
    """The Cython side: ``set_mode`` in an extension runs only while
    ``pkg.config`` is imported and writes the extension's ``MODE``; a change
    to its body reaches ``test_b``, which reads it through ``mode``."""
    if sys.version_info < (3, 13):
        pytest.skip("a profiled Cython build reports to sys.monitoring from Python 3.13")
    pytest.importorskip("Cython")
    pytest.importorskip("setuptools")
    if shutil.which("cc") is None and shutil.which("gcc") is None:
        pytest.skip("no C compiler")
    repo.commit(
        {
            **BASE,
            ".gitignore": "__pycache__/\n.diffcone/\nbuild/\n*.c\n*.so\n*.pyd\n",
            "setup.py": "from setuptools import setup\nfrom Cython.Build import cythonize\n\n"
            'setup(ext_modules=cythonize("pkg/_state.pyx"))\n',
            "pkg/_state.pyx": CYTHON_STATE,
            "pkg/config.py": 'from pkg._state import set_mode\n\nX = set_mode("slow")\n',
            "tests/test_a.py": IMPORTS_CONFIG,
            "tests/test_b.py": READS_MODE.replace("pkg.state", "pkg._state"),
        }
    )
    subprocess.run(
        [sys.executable, "setup.py", "-q", "build_ext", "--inplace"],
        cwd=repo.path,
        check=True,
        capture_output=True,
    )
    ev = repo.collect()
    assert "pkg/_state.pyx::set_mode" in ev.import_by
    head = repo.commit({"pkg/_state.pyx": CYTHON_STATE.replace("= m\n", "= m + '!'\n")})
    plan = _plan(repo, ev.commit, head, ev)
    assert selected(plan) == {A, B}


# --------------------------------------------------------------------------- EVP-5

PLUGIN = """\
import pytest

from pkg.flags import FLAGS


@pytest.fixture(autouse=True)
def _turn_on():
    FLAGS["on"] = True
    yield
    FLAGS.clear()
"""

FLAG_FILES = {
    **BASE,
    "pkg/flags.py": "FLAGS = {}\n\n\ndef flag():\n    return FLAGS.get('on', False)\n",
    "tests/plugin.py": PLUGIN,
    "tests/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": "from pkg.flags import flag\n\n\ndef test_b():\n    assert not flag()\n",
}
REGISTERS = 'pytest_plugins = ["tests.plugin"]\n\n\ndef test_a():\n    pass\n'


@pytest.mark.parametrize("how", ["added", "edited", "deleted", "in a module without tests"])
def test_evp5_pytest_plugins_in_a_test_module_reaches_the_session(repo, how):
    """``pytest_plugins`` in a test module registers its plugins for the
    whole session: a change to it reaches every test, not only the module's."""
    files = dict(FLAG_FILES)
    path, after = "tests/test_a.py", REGISTERS
    if how == "edited":
        files[path] = REGISTERS.replace('["tests.plugin"]', "[]")
    elif how == "deleted":
        files[path], after = REGISTERS, FLAG_FILES[path]
    elif how == "in a module without tests":
        path = "tests/test_plugins.py"
        files[path], after = "pytest_plugins = []\n", 'pytest_plugins = ["tests.plugin"]\n'
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({path: after})
    bench = asv_target("benchmarks.TimeFlag.time_flag", "pkg.flags.flag")
    plan = _plan(repo, base, head, ev, [bench])
    assert _pytest(plan) == {A, "tests/test_b.py::test_b"}
    rules = {r.rule for d in plan.decisions if d.selected for r in d.reasons}
    assert "pytest_hook_changed" in rules


def _pytest(plan) -> set[str]:
    return {
        d.target.runner_id for d in plan.decisions if d.selected and d.target.runner == "pytest"
    }


# --------------------------------------------------------------------------- EVP-1

STREAM = """\
def flushed(x):
    flush = getattr(x, "flush", None)
    return flush is None or flush() is None
"""

BARE = """\
class _Base:
    pass


class _Bare(_Base):
    closed = False


def test_bare():
    assert not _Bare.closed
"""

OPEN = """\
from types import SimpleNamespace

from pkg.stream import flushed


def test_open():
    assert flushed(SimpleNamespace())
"""

HOLDS = {
    "an ancestor's __subclasses__": (
        "from tests.test_bare import _Base\n",
        "_Base.__subclasses__()",
    ),
    "type.__subclasses__": (
        "from tests.test_bare import _Base\n",
        "type.__subclasses__(_Base)",
    ),
    "object.__subclasses__, recursively": (
        "",
        "_walk(object)",
    ),
    "the collector": (
        "import gc\n",
        "[o for o in gc.get_objects() if isinstance(o, type) and o.__name__ == '_Bare']",
    ),
}
WALK = (
    "def _walk(c):\n    out = []\n    for s in type.__subclasses__(c):\n"
    "        if s.__module__.startswith('tests.'):\n            out.append(s)\n"
    "        out += _walk(s)\n    return out\n\n\n"
)
HELD = "tests/test_held.py::test_held"


@pytest.mark.parametrize("how", list(HOLDS))
def test_evp1_a_fake_found_without_naming_it(repo, how):
    """Item 13 guards a name match on a test fake's member by the code that
    can hand the fake on. Code that finds the class without naming it (a
    base's ``__subclasses__()``, the collector) is such code."""
    imports, finder = HOLDS[how]
    held = (
        f"from pkg.stream import flushed\n{imports}\n\n"
        + (WALK if "_walk" in finder else "")
        + f"def test_held():\n    assert all(flushed(c()) for c in {finder})\n"
    )
    files = {
        **BASE,
        "pkg/stream.py": STREAM,
        "tests/test_bare.py": BARE,
        "tests/test_held.py": held,
        "tests/test_open.py": OPEN,
    }
    base = repo.commit(files)
    ev = repo.collect()
    flush = "    closed = False\n\n    def flush(self):\n        return 1\n"
    head = repo.commit({"tests/test_bare.py": BARE.replace("    closed = False\n", flush)})
    bench = asv_target("benchmarks.TimeStream.time_flushed", "pkg.stream.flushed")
    plan = _plan(repo, base, head, ev, [bench])
    # test_open never holds a fake: the narrowing stands.
    assert _pytest(plan) == {HELD, "tests/test_bare.py::test_bare"}


# --------------------------------------------------------------------------- EVP-3

LOADER = """\
import importlib


def load(modname, attr):
    return getattr(importlib.import_module(modname), attr, None)


def build(modname, name):
    return getattr(importlib.import_module(modname), name)()


def call(obj, name):
    return getattr(obj, name, None)
"""

NAMES = """\
import os

from pkg.loader import load

MOD = "tests." + "test_a"


def test_b():
    assert load(MOD, os.environ.get("DIFFCONE_NAME", "HELPER")) is None
"""


def test_evp3_library_code_imports_a_test_module_by_name(repo):
    """``load`` imports a module a test names at run time and looks a name
    up on it: a name added to that test module reaches the test."""
    files = {
        **BASE,
        "pkg/loader.py": LOADER,
        "tests/test_a.py": "VALUE = 1\n\n\ndef test_a():\n    assert VALUE\n",
        "tests/test_b.py": NAMES,
        "tests/test_c.py": "def test_c():\n    pass\n",
    }
    base = repo.commit(files)
    ev = repo.collect()
    helper = files["tests/test_a.py"] + "\n\ndef HELPER():\n    pass\n"
    head = repo.commit({"tests/test_a.py": helper})
    bench = asv_target("benchmarks.TimeLoad.time_load", "pkg.loader.load")
    plan = _plan(repo, base, head, ev, [bench])
    assert _pytest(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_evp3_library_code_builds_a_fake_from_a_test_module(repo):
    """The item-13 side: library code builds the fake by importing the test
    module that holds it, so the test that asked for it holds the fake."""
    fakes = "class _Fake:\n    closed = False\n\n\ndef test_fakes():\n    assert _Fake\n"
    files = {
        **BASE,
        "pkg/loader.py": LOADER,
        "pkg/stream.py": STREAM,
        "tests/test_fakes.py": fakes,
        "tests/test_build.py": (
            "from pkg.loader import build\nfrom pkg.stream import flushed\n\n\n"
            "def test_build():\n"
            '    assert flushed(build("tests." + "test_fakes", "_" + "Fake"))\n'
        ),
        "tests/test_open.py": OPEN,
    }
    base = repo.commit(files)
    ev = repo.collect()
    flush = "    closed = False\n\n    def flush(self):\n        return 1\n"
    head = repo.commit({"tests/test_fakes.py": fakes.replace("    closed = False\n", flush)})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {"tests/test_fakes.py::test_fakes", "tests/test_build.py::test_build"}


def test_evp3_a_test_class_instance_handed_to_a_library_lookup(repo):
    """A member added to a test-code class is seen by a library lookup by a
    name nothing bounds, on an instance a test handed it."""
    helpers = "class Helper:\n    x = 0\n\n\ndef test_helpers():\n    assert Helper.x == 0\n"
    files = {
        **BASE,
        "pkg/loader.py": LOADER,
        "tests/test_helpers.py": helpers,
        "tests/test_h.py": (
            "import os\n\nfrom pkg.loader import call\nfrom tests.test_helpers import Helper\n\n\n"
            "def test_h():\n"
            '    assert call(Helper(), os.environ.get("DIFFCONE_NAME", "go")) is None\n'
        ),
        "tests/test_other.py": (
            "from pkg.loader import call\n\n\ndef test_other():\n"
            '    assert call(object(), "x") is None\n'
        ),
    }
    base = repo.commit(files)
    ev = repo.collect()
    go = "    x = 0\n\n    def go(self):\n        return 1\n"
    head = repo.commit({"tests/test_helpers.py": helpers.replace("    x = 0\n", go)})
    plan = _plan(repo, base, head, ev)
    # test_other runs the same lookup, but never holds a Helper.
    assert selected(plan) == {"tests/test_h.py::test_h", "tests/test_helpers.py::test_helpers"}

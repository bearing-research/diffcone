"""Cython sources at function level (roadmap item 7, stage 1): the tolerant
reader, function-level changes between two snapshots, and the index and its
cache carrying them."""

from __future__ import annotations

import sys

import pytest

from diffcone.cache import index_from_dict, index_to_dict
from diffcone.cython import CythonChanges, cython_changes, read
from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
from diffcone.testing import executed, rules, selected

PYX = '''\
# cython: language_level=3
cimport cython
from libc.stdlib cimport malloc

cdef int LIMIT = 10


@cython.boundscheck(False)
@cython.wraparound(False)
def total(values):
    """Sum.

Continued at column 0, as pandas' docstrings sometimes are.
    """
    cdef int64_t **rows = <int64_t**>malloc(8)
    return _add(values[0], values[1])


cdef inline int _add(int a, int b) noexcept nogil:
    return a + b


cdef (Py_ssize_t, Py_ssize_t) bounds(
    slice s,
    Py_ssize_t n,
):
    def helper(x):
        return x
    return helper(0), n


cdef class Box:
    cdef public double v

    def __init__(self, v):
        self.v = v

    cpdef double scaled(self, double k):
        return self.v * k

    @property
    def doubled(self):
        """Twice.
"""
        return self.v * 2

    @doubled.setter
    def doubled(self, value):
        self.v = value / 2


class Plain:
    def method(self):
        return LIMIT
'''

PXD = """\
cdef int _add(int a, int b) noexcept nogil
cpdef double scaled(double k)

cdef inline bint is_small(int x) noexcept:
    return x < 3
"""


def test_the_reader_finds_functions_methods_and_their_flags():
    module = read("pkg/_ext.pyx", PYX)
    functions = module.by_name()
    assert list(functions) == [
        "total",
        "_add",
        "bounds",
        "Box.__init__",
        "Box.scaled",
        "Box.doubled",
        "Box.doubled#2",  # the setter after the getter
        "Plain.method",
    ]
    # A span starts at the first decorator: a code object's first line.
    assert functions["total"].start == 8
    # The docstring's column-0 line does not end the function or the class.
    assert functions["total"].end == 16
    assert functions["Box.doubled"].end == functions["Box.doubled#2"].start - 2
    assert (functions["_add"].nogil, functions["_add"].cpdef) == (True, False)
    assert (functions["Box.scaled"].nogil, functions["Box.scaled"].cpdef) == (False, True)
    # A nested function is part of its parent; a C tuple return type is not a name.
    assert "helper" in functions["bounds"].names and "helper" not in functions
    # The names a body mentions, for the callers of nogil and cpdef functions.
    assert {"_add", "malloc"} <= functions["total"].names
    assert module.function_at(17) is None  # the blank line after total
    at_19 = module.function_at(19)
    assert at_19 is not None and at_19.name == "_add"


def test_declarations_without_a_body_are_not_functions():
    module = read("pkg/_ext.pxd", PXD)
    assert list(module.by_name()) == ["is_small"]


def test_changes_are_function_level_when_only_bodies_change():
    before = {"pkg/_ext.pyx": read("pkg/_ext.pyx", PYX)}

    def changed(text):
        return cython_changes(before, {"pkg/_ext.pyx": read("pkg/_ext.pyx", text)})

    body = changed(PYX.replace("return self.v * k", "return k * self.v"))
    assert body.functions == (("pkg/_ext.pyx", "Box.scaled"),)
    assert body.files == () and body.names == ()
    # Comments, blank lines and moving a function down are not changes.
    moved = changed(PYX.replace("cdef class Box:", "\n\n# A box.\ncdef class Box:"))
    assert moved == CythonChanges()
    # A compiler directive is a comment that changes every function.
    for text in (
        PYX.replace("language_level=3", "language_level=3, cdivision=True"),
        "# cython: boundscheck=False\n" + PYX,
        "# distutils: language = c++\n" + PYX,
    ):
        (path, why), *_ = changed(text).files
        assert path == "pkg/_ext.pyx" and "a compiler directive" in why
    gone = cython_changes(before, {})
    assert gone.files == (("pkg/_ext.pyx", "deleted"),)


OUTSIDE = """\
\"\"\"A module docstring.\"\"\"
# cython: language_level=3
cimport numpy as cnp
from libc.math cimport (
    isnan,
    sqrt as root,
)
import numpy as np
from pkg.util import helper
from .np_datetime cimport npy_datetimestruct
from pandas._libs cimport util

cdef extern from "fast.h" nogil:
    int fast_add(int a, int b)
    ctypedef struct pair_t:
        int left
        int right

ctypedef (int, int) span_t
ctypedef int (*cmp_t)(int, int)
DEF WIDTH = 8

cdef:
    double SCALE = 2.0
    int[16] TABLE

cdef enum Colour:
    RED
    GREEN = 3

cpdef enum Shape:
    SQUARE

cdef int64_t **rows = NULL
LIMIT = 10
A = B = 1
COUNTS[0] += 1

cnp.import_array()

cdef class Box(Base):
    \"\"\"A box.\"\"\"
    cdef public double v
    cdef readonly int n
    cdef object _cache
    __array_priority__ = 100
    kind = "box"

    def get(self):
        return self.v


IF UNAME_SYSNAME == "Windows":
    include "windows.pxi"
"""


def test_the_reader_finds_what_statements_bind():
    module = read("pkg/_ext.pyx", OUTSIDE)
    statements = module.statements
    assert statements is not None
    got = [(s.scope, s.kind, s.names, s.visible, s.why) for s in statements]
    assert got == [
        ("", "code", (), False, "a compiler directive"),
        ("", "code", (), False, "a docstring"),
        ("", "import", ("cnp",), False, ""),
        ("", "import", ("isnan",), False, ""),
        ("", "import", ("root",), False, ""),
        ("", "import", ("np",), True, ""),
        ("", "import", ("helper",), True, ""),
        ("", "import", ("npy_datetimestruct",), False, ""),
        ("", "import", ("util",), False, ""),
        ("", "declaration", ("fast_add",), False, ""),
        ("", "type", ("left", "pair_t", "right"), False, ""),
        ("", "declaration", ("span_t",), False, ""),
        ("", "declaration", ("cmp_t",), False, ""),
        ("", "declaration", ("WIDTH",), False, ""),
        ("", "declaration", ("SCALE",), False, ""),
        ("", "declaration", ("TABLE",), False, ""),
        ("", "type", ("Colour", "GREEN", "RED"), False, ""),
        ("", "type", ("SQUARE", "Shape"), True, ""),
        ("", "declaration", ("rows",), False, ""),
        ("", "assignment", ("LIMIT",), True, ""),
        ("", "assignment", ("A", "B"), True, ""),
        ("", "assignment", ("COUNTS",), True, ""),
        ("", "code", (), False, "an expression statement that runs at import"),
        ("", "class", ("Box",), True, ""),
        ("Box", "code", (), False, "a docstring"),
        ("Box", "declaration", ("v",), True, ""),
        ("Box", "declaration", ("n",), True, ""),
        ("Box", "declaration", ("_cache",), False, ""),
        ("Box", "code", (), False, "binds a special name (__array_priority__)"),
        ("Box", "assignment", ("kind",), True, ""),
        ("", "code", (), False, "compile-time IF"),
        ("", "code", (), False, "include"),
    ]
    box = next(s for s in statements if s.kind == "class")
    assert box.bases == ("Base",)
    imports = [s.module for s in statements if s.kind == "import"]
    assert imports[-2:] == [".np_datetime npy_datetimestruct", "pandas._libs util"]
    # Statements inside functions are the functions' own.
    assert [f.name for f in module.functions] == ["Box.get"]
    # A file tokenize cannot read has no statements.
    assert read("pkg/_bad.pyx", 'X = """never closed\n').statements is None


def test_outside_changes_are_the_names_whose_binding_changed():
    before = {"pkg/_ext.pyx": read("pkg/_ext.pyx", OUTSIDE)}

    def changed(text, path="pkg/_ext.pyx"):
        return cython_changes(before, {path: read(path, text)})

    def names(text):
        result = changed(text)
        assert result.files == (), result.files
        return {(n.scope, n.name, n.change, n.visible, n.attribute) for n in result.names}

    # Reformatting an import list changes nothing; adding to it, only that name.
    assert names(OUTSIDE.replace("    isnan,\n", "    isinf,\n    isnan,\n")) == {
        ("", "isinf", "added", False, False)
    }
    assert names(OUTSIDE.replace("(\n    isnan,\n    sqrt as root,\n)", "isnan, sqrt as root")) == (
        set()
    )
    assert names(OUTSIDE.replace("double SCALE = 2.0", "double SCALE = 3.0")) == {
        ("", "SCALE", "changed", False, False)
    }
    assert names(OUTSIDE.replace("LIMIT = 10", "LIMIT = 11")) == {
        ("", "LIMIT", "changed", True, False)
    }
    # A struct, enum or union is one statement: member order is layout and value.
    assert names(OUTSIDE.replace("    RED\n    GREEN = 3", "    GREEN = 3\n    RED")) == {
        ("", n, "changed", False, False) for n in ("Colour", "GREEN", "RED")
    }
    # A class attribute.
    assert names(OUTSIDE.replace("cdef object _cache", "cdef dict _cache")) == {
        ("Box", "_cache", "changed", False, True)
    }
    # Functions added or deleted are names too: def visible, cdef not.
    assert names(OUTSIDE + "\n\ndef extra():\n    return 1\n") == {
        ("", "extra", "added", True, False)
    }
    assert names(OUTSIDE.replace("    def get(self):", "    cdef get(self):")) == {
        ("Box", "get", "changed", True, False)
    }

    def file_level(text, path="pkg/_ext.pyx"):
        (got_path, why), *_ = changed(text, path).files
        assert got_path == path
        return why

    # Code that binds nothing by name, a special name, a class statement.
    assert "an expression statement" in file_level(OUTSIDE.replace("cnp.import_array()", "f()"))
    assert "a docstring" in file_level(OUTSIDE.replace("A box.", "A big box."))
    assert "special name" in file_level(OUTSIDE.replace("= 100", "= 200"))
    assert "class statement of Box" in file_level(OUTSIDE.replace("Box(Base)", "Box(Other)"))
    assert "a star import" in file_level(OUTSIDE.replace("cimport util", "cimport *"))
    # Reordering statements can change a value read at import.
    swapped = OUTSIDE.replace("LIMIT = 10\nA = B = 1", "A = B = 1\nLIMIT = 10")
    assert file_level(swapped).endswith("statements reordered")
    # A deleted name Python can see may be imported from the module by
    # Python code, which diffcone does not index.
    assert "LIMIT deleted" in file_level(OUTSIDE.replace("LIMIT = 10\n", ""))
    # ... while a .pxd file's declarations are names even when it is added.
    added = cython_changes({}, {"pkg/_ext.pxd": read("pkg/_ext.pxd", PXD)})
    assert added.files == ()
    assert {n.name for n in added.names} == {"_add", "scaled", "is_small"}


def test_snapshots_and_the_cache_carry_cython_modules(repo):
    repo.commit({"pkg/__init__.py": "", "pkg/_ext.pyx": PYX, "pkg/_ext.pxd": PXD})
    index = build_index(read_snapshot(repo.path, "HEAD", ["."]))
    assert set(index.cython) == {"pkg/_ext.pyx", "pkg/_ext.pxd"}
    assert index.cython["pkg/_ext.pyx"] == read("pkg/_ext.pyx", PYX)
    assert index_from_dict(index_to_dict(index)).cython == index.cython

    (repo.path / "pkg" / "_ext.pyx").write_text(PYX.replace("a + b", "b + a"), "utf-8")
    worktree = build_index(read_snapshot(repo.path, "WORKTREE", ["."]))
    changes = cython_changes(index.cython, worktree.cython)
    assert changes.functions == (("pkg/_ext.pyx", "_add"),)
    repo.git("add", "pkg/_ext.pyx")
    staged = build_index(read_snapshot(repo.path, "INDEX", ["."]))
    assert staged.cython == worktree.cython


# --------------------------------------------------------------------------- recording

FAST = """\
# cython: profile=True
from libc.math cimport sqrt

cdef double OFFSET = 1
LABEL = "box"


cdef class Box:
    cdef public double v

    def __init__(self, v):
        self.v = v

    def __add__(self, other):
        return Box(self.v + other.v)

    cpdef double scaled(self, double k):
        return self.v * k


cdef class Big(Box):
    cpdef double scaled(self, double k):
        return Box.scaled(self, k) + OFFSET


cdef double _fast(double x) noexcept nogil:
    return x * 3


def tripled(double x):
    cdef double r
    with nogil:
        r = _fast(x)
    return r
"""

FAST_TESTS = """\
from pkg import _fast
from pkg._fast import Big, Box, tripled


def test_add():
    assert (Box(1) + Box(2)).v == 3


def test_tripled():
    assert tripled(2) == 6


def test_big():
    assert Big(1).scaled(2) == 3


def test_plain():
    assert 1 + 1 == 2


def test_label():
    assert _fast.LABEL == "box"


def test_lookup():
    name = "".join(["LA", "BEL"])
    assert getattr(_fast, name) == "box"
"""

SETUP = """\
from setuptools import setup
from Cython.Build import cythonize

setup(ext_modules=cythonize("pkg/_fast.pyx"))
"""


def _needs_a_build(profile: bool) -> None:
    """Skip unless an extension can be built and recorded here: evidence
    needs sys.monitoring (3.12+), and a profiled Cython build reports its
    calls through it only from 3.13 (on 3.12 it uses the legacy profiler,
    so the store holds no Cython record and every Cython edit selects all)."""
    import shutil

    if sys.version_info < ((3, 13) if profile else (3, 12)):
        pytest.skip("a profiled Cython build reports to sys.monitoring from Python 3.13")
    pytest.importorskip("Cython")
    pytest.importorskip("setuptools")
    if shutil.which("cc") is None and shutil.which("gcc") is None:
        pytest.skip("no C compiler")


def _built_extension(repo, profile=True):
    """A fixture repository with a Cython extension built in place, profiled
    unless ``profile`` is false."""
    import subprocess

    _needs_a_build(profile)
    repo.commit(
        {
            ".gitignore": "__pycache__/\n.diffcone/\nbuild/\n*.c\n*.so\n*.pyd\n",
            "setup.py": SETUP,
            "pkg/__init__.py": "",
            "pkg/_fast.pyx": FAST if profile else FAST.replace("# cython: profile=True\n", ""),
            "tests/__init__.py": "",
            "tests/test_fast.py": FAST_TESTS,
        }
    )
    subprocess.run(
        [sys.executable, "setup.py", "-q", "build_ext", "--inplace"],
        cwd=repo.path,
        check=True,
        capture_output=True,
    )


def test_a_profiled_build_records_cython_functions(repo):
    _built_extension(repo)
    ev = repo.collect()
    T = "tests/test_fast.py::"
    cy = {
        t: {s for s in executed(ev, T + "test_" + t) if ".pyx::" in s}
        for t in ("add", "tripled", "big", "plain", "label", "lookup")
    }
    # A slot reached by `+` and the constructor it calls.
    assert cy["add"] == {"pkg/_fast.pyx::Box.__init__", "pkg/_fast.pyx::Box.__add__"}
    # A nogil function raises no start: only its (traced) caller is recorded.
    assert cy["tripled"] == {"pkg/_fast.pyx::tripled"}
    # A cpdef's C body called with skip_dispatch (Box.scaled(self, k)) raises
    # none either; the caller rule covers both (roadmap item 7).
    assert cy["big"] == {"pkg/_fast.pyx::Box.__init__", "pkg/_fast.pyx::Big.scaled"}
    assert cy["plain"] == cy["label"] == cy["lookup"] == set()


# --------------------------------------------------------------------------- planning


def _plan(repo, ev, text):
    head = repo.commit({"pkg/_fast.pyx": text})
    return repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)


def test_a_body_change_selects_the_tests_that_executed_the_function(repo):
    _built_extension(repo)
    ev = repo.collect()
    plan = _plan(repo, ev, FAST.replace("Box(self.v + other.v)", "Box(other.v + self.v)"))
    assert selected(plan) == {"tests/test_fast.py::test_add"}
    assert rules(plan, "tests/test_fast.py::test_add") == {"executed_changed"}
    assert not plan.fallbacks
    # Static planning cannot connect a test to a Cython function.
    static = repo.plan(ev.commit, plan.head.commit, [], discover_runners=["pytest"])
    assert len(selected(static)) == 6


def test_nogil_and_cpdef_functions_are_found_through_their_callers(repo):
    _built_extension(repo)
    ev = repo.collect()
    # _fast is nogil: tripled names it and was recorded.
    plan = _plan(repo, ev, FAST.replace("return x * 3", "return 3 * x"))
    assert selected(plan) == {"tests/test_fast.py::test_tripled"}
    assert rules(plan, "tests/test_fast.py::test_tripled") == {"cython_caller"}
    # Box.scaled ran only as Box.scaled(self, k) inside Big.scaled, which a
    # profiled build does not report: Big.scaled names it.
    plan = _plan(repo, ev, FAST.replace("return self.v * k", "return k * self.v"))
    assert selected(plan) == {"tests/test_fast.py::test_big"}


def test_outside_edits_select_the_readers_of_the_names_they_change(repo):
    _built_extension(repo)
    ev = repo.collect()
    T = "tests/test_fast.py::"
    # A comment is not a change.
    plan = _plan(repo, ev, FAST.replace("cdef class Big(Box):", "# Bigger.\ncdef class Big(Box):"))
    assert selected(plan) == set() and not plan.fallbacks
    # A C global: the Cython functions naming it.
    plan = _plan(repo, ev, FAST.replace("OFFSET = 1", "OFFSET = 2"))
    assert selected(plan) == {T + "test_big"}
    assert rules(plan, T + "test_big") == {"executed_reader"}
    assert not plan.fallbacks
    # A name Python can see: the Python code reading it by that name, and
    # the lookups by a name nothing bounds.
    plan = _plan(repo, ev, FAST.replace('LABEL = "box"', 'LABEL = "crate"'))
    assert selected(plan) == {T + "test_label", T + "test_lookup"}
    assert rules(plan, T + "test_lookup") == {"lookup_site"}
    assert not plan.fallbacks
    plan = _plan(repo, ev, FAST + "\n\ndef extra():\n    return 1\n")
    assert selected(plan) == {T + "test_lookup"} and not plan.fallbacks
    # A class attribute: whatever holds an instance of the class or a
    # subclass, and (a public one) the lookups.
    plan = _plan(repo, ev, FAST.replace("cdef public double v", "cdef public double v, w"))
    assert selected(plan) == {T + "test_add", T + "test_big", T + "test_lookup"}
    assert not plan.fallbacks
    plan = _plan(
        repo, ev, FAST.replace("cdef public double v", "cdef public double v\n    cdef int w")
    )
    assert selected(plan) == {T + "test_add", T + "test_big"}
    # A C name nothing mentions yet: nothing.
    for text in (
        FAST.replace("cimport sqrt", "cimport fabs, sqrt"),
        FAST.replace("LABEL = ", "cdef int UNUSED = 0\nLABEL = "),
        FAST + "\n\ncdef double _slow(double x):\n    return x\n",
    ):
        plan = _plan(repo, ev, text)
        assert selected(plan) == set() and not plan.fallbacks


SCALE_PXD = "cdef double FACTOR\n"
SCALE = "# cython: profile=True\ncdef double FACTOR = 3\n"
USE = """\
# cython: profile=True
from pkg._scale cimport FACTOR


def apply(double x):
    return x * FACTOR
"""
USE_TESTS = """\
from pkg._use import apply


def test_apply():
    assert apply(2) == 6


def test_plain():
    assert 1 + 1 == 2
"""


def test_a_c_global_its_pxd_declares_reaches_the_modules_cimporting_it(repo):
    """``_scale.pyx`` initialises a global its ``.pxd`` declares, and
    ``_use.pyx`` cimports it: changing the value reaches ``_use``'s readers,
    although the edit is in a ``.pyx`` (regression for roadmap item 8)."""
    import subprocess

    _needs_a_build(profile=True)
    repo.commit(
        {
            ".gitignore": "__pycache__/\n.diffcone/\nbuild/\n*.c\n*.so\n*.pyd\n",
            "setup.py": SETUP.replace('"pkg/_fast.pyx"', '["pkg/_scale.pyx", "pkg/_use.pyx"]'),
            "pkg/__init__.py": "",
            "pkg/_scale.pxd": SCALE_PXD,
            "pkg/_scale.pyx": SCALE,
            "pkg/_use.pyx": USE,
            "tests/__init__.py": "",
            "tests/test_use.py": USE_TESTS,
        }
    )
    subprocess.run(
        [sys.executable, "setup.py", "-q", "build_ext", "--inplace"],
        cwd=repo.path,
        check=True,
        capture_output=True,
    )
    ev = repo.collect()
    head = repo.commit({"pkg/_scale.pyx": SCALE.replace("= 3", "= 4")})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {"tests/test_use.py::test_apply"}
    assert not plan.fallbacks


def test_outside_edits_nothing_bounds_select_everything(repo):
    _built_extension(repo)
    ev = repo.collect()
    for text, why in (
        (FAST.replace("profile=True", "profile=True, cdivision=True"), "a compiler directive"),
        (
            FAST.replace("cdef class Big(Box):", 'cdef class Big(Box):\n    """Big."""'),
            "a docstring",
        ),
        (FAST.replace('LABEL = "box"\n', 'LABEL = "box"\nprint(LABEL)\n'), "an expression"),
        (FAST.replace("def tripled", "def _tripled"), "tripled deleted"),
    ):
        plan = _plan(repo, ev, text)
        assert len(selected(plan)) == 6
        assert [f.rule for f in plan.fallbacks] == ["unobserved_file_changed"]
        assert why in plan.fallbacks[0].detail


def test_a_store_without_cython_records_selects_everything(repo):
    _built_extension(repo, profile=False)
    ev = repo.collect()
    assert not any(".pyx::" in s for s in ev.symbols)
    plan = _plan(
        repo,
        ev,
        FAST.replace("# cython: profile=True\n", "").replace("return x * 3", "return 3 * x"),
    )
    assert len(selected(plan)) == 6
    assert "profile=True" in plan.fallbacks[0].detail

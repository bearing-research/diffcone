"""Cython sources at function level (roadmap item 7, stage 1): the tolerant
reader, function-level changes between two snapshots, and the index and its
cache carrying them."""

from __future__ import annotations

import sys

import pytest

from diffcone.cache import index_from_dict, index_to_dict
from diffcone.cython import cython_changes, read
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
    assert module.function_at(19).name == "_add"


def test_declarations_without_a_body_are_not_functions():
    module = read("pkg/_ext.pxd", PXD)
    assert list(module.by_name()) == ["is_small"]


def test_changes_are_function_level_when_only_bodies_change():
    before = {"pkg/_ext.pyx": read("pkg/_ext.pyx", PYX)}

    def changed(text):
        return cython_changes(before, {"pkg/_ext.pyx": read("pkg/_ext.pyx", text)})

    body = changed(PYX.replace("return self.v * k", "return k * self.v"))
    assert body.functions == (("pkg/_ext.pyx", "Box.scaled"),) and body.files == ()
    # Comments, blank lines and moving a function down are not changes.
    moved = PYX.replace("cdef class Box:", "\n\n# A box.\ncdef class Box:")
    assert changed(moved).functions == () and changed(moved).files == ()
    # Anything outside a function is not attributed to one.
    outside = changed(PYX.replace("cdef int LIMIT = 10", "cdef int LIMIT = 11"))
    assert outside.files == (("pkg/_ext.pyx", "changed outside its functions"),)
    # A compiler directive is a comment that changes every function.
    for text in (
        PYX.replace("language_level=3", "language_level=3, cdivision=True"),
        "# cython: boundscheck=False\n" + PYX,
        "# distutils: language = c++\n" + PYX,
    ):
        assert changed(text).files == (("pkg/_ext.pyx", "changed outside its functions"),)
    # A new function (an override changes dispatch) is a file-level change.
    added = changed(PYX + "\n\ndef extra():\n    return 1\n")
    assert added.files == (("pkg/_ext.pyx", "functions added or deleted (extra added)"),)
    gone = cython_changes(before, {})
    assert gone.files == (("pkg/_ext.pyx", "deleted"),)


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
        return Box.scaled(self, k) + 1


cdef double _fast(double x) noexcept nogil:
    return x * 3


def tripled(double x):
    cdef double r
    with nogil:
        r = _fast(x)
    return r
"""

FAST_TESTS = """\
from pkg._fast import Big, Box, tripled


def test_add():
    assert (Box(1) + Box(2)).v == 3


def test_tripled():
    assert tripled(2) == 6


def test_big():
    assert Big(1).scaled(2) == 3


def test_plain():
    assert 1 + 1 == 2
"""

SETUP = """\
from setuptools import setup
from Cython.Build import cythonize

setup(ext_modules=cythonize("pkg/_fast.pyx"))
"""


def _built_extension(repo, profile=True):
    """A fixture repository with a Cython extension built in place, profiled
    unless ``profile`` is false."""
    import shutil
    import subprocess

    pytest.importorskip("Cython")
    pytest.importorskip("setuptools")
    if shutil.which("cc") is None and shutil.which("gcc") is None:
        pytest.skip("no C compiler")
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
        for t in ("add", "tripled", "big", "plain")
    }
    # A slot reached by `+` and the constructor it calls.
    assert cy["add"] == {"pkg/_fast.pyx::Box.__init__", "pkg/_fast.pyx::Box.__add__"}
    # A nogil function raises no start: only its (traced) caller is recorded.
    assert cy["tripled"] == {"pkg/_fast.pyx::tripled"}
    # A cpdef's C body called with skip_dispatch (Box.scaled(self, k)) raises
    # none either; the caller rule covers both (roadmap item 7).
    assert cy["big"] == {"pkg/_fast.pyx::Box.__init__", "pkg/_fast.pyx::Big.scaled"}
    assert cy["plain"] == set()


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
    assert len(selected(static)) == 4


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


def test_other_cython_edits_select_everything_or_nothing(repo):
    _built_extension(repo)
    ev = repo.collect()
    # A comment is not a change.
    plan = _plan(repo, ev, FAST.replace("cdef class Big(Box):", "# Bigger.\ncdef class Big(Box):"))
    assert selected(plan) == set() and not plan.fallbacks
    # A compiler directive changes how every function is compiled.
    plan = _plan(repo, ev, FAST.replace("profile=True", "profile=True, cdivision=True"))
    assert len(selected(plan)) == 4
    assert [f.rule for f in plan.fallbacks] == ["unobserved_file_changed"]
    # A change outside every function is not attributed to one.
    plan = _plan(repo, ev, FAST.replace("cdef public double v", "cdef public double v, w"))
    assert len(selected(plan)) == 4
    assert [f.rule for f in plan.fallbacks] == ["unobserved_file_changed"]


def test_a_store_without_cython_records_selects_everything(repo):
    _built_extension(repo, profile=False)
    ev = repo.collect()
    assert not any(".pyx::" in s for s in ev.symbols)
    plan = _plan(
        repo,
        ev,
        FAST.replace("# cython: profile=True\n", "").replace("return x * 3", "return 3 * x"),
    )
    assert len(selected(plan)) == 4
    assert "profile=True" in plan.fallbacks[0].detail

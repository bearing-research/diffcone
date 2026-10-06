"""Cython sources at function level (roadmap item 7).

A tolerant reader of ``.pyx``, ``.pxd`` and ``.pxi`` files that works from
indentation alone: diffcone is stdlib only, so it does not use Cython's
parser, and it does not need to. What evidence planning asks of a Cython file
is which functions changed and what each function's body mentions.

* A *function* is a ``def``, ``cpdef`` or ``cdef`` statement with a parameter
  list and a body: its header (which may span lines) ends with ``:`` at
  bracket depth 0. A ``cdef`` declaration without a body (``.pxd`` files), a
  variable, a struct, enum, union, extern or fused block, and a ``ctypedef``
  are not functions.
* ``cdef class`` and ``class`` blocks are scopes: the functions in them are
  methods, named ``Class.method``. A name that repeats in one scope (a
  property's setter after its getter) is numbered: ``Class.attr#2``.
* A function's span starts at its first decorator, where a code object's
  first line is, and ends at its last indented line. A function nested in
  another belongs to it: its text is part of the parent's body, as nested
  functions are part of their parent in the Python index.
* ``nogil`` and ``cpdef`` are recorded, and so is every name the body
  mentions: a profiled build raises no call event for a ``nogil`` function,
  nor for a ``cpdef`` method's C body under ``skip_dispatch`` (an explicit
  ``Base.method(self, ...)`` call), so those are found through the functions
  that name them, which also covers a function taken as a pointer.
* Blank lines and comment-only lines are ignored by both hashes, except
  compiler directives (``# cython: boundscheck=False``, ``# distutils:``),
  which change how every function in the file is compiled. Everything
  outside the functions (directives, ``cimport``, ``ctypedef``, structs,
  class headers and attribute declarations, constants, ``include``) is
  hashed together: a change there is not attributed to any function.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

CYTHON_SUFFIXES = (".pyx", ".pxd", ".pxi")

_HEAD = re.compile(r"^(\s*)(cpdef|cdef|def)\b")
_CLASS = re.compile(
    r"^(\s*)(?:cdef\s+(?:(?:public|readonly|final)\s+)*class|class)\s+([A-Za-z_]\w*)"
)
_NOT_FUNC = re.compile(
    r"^\s*cdef\s+(?:class|struct|enum|union|extern|packed|fused|cppclass|"
    r"public\s+(?:struct|enum))\b"
)
_CALLABLE = re.compile(r"([A-Za-z_]\w*)\s*\(")
_WORD = re.compile(r"\b[A-Za-z_]\w*\b")
_DIRECTIVE = re.compile(r"^\s*#\s*(?:cython|distutils)\s*:")


def is_cython(path: str) -> bool:
    return path.endswith(CYTHON_SUFFIXES)


@dataclass(frozen=True)
class CythonFunction:
    name: str  # qualified within the file
    start: int  # first decorator line (1-based)
    end: int
    body_hash: str
    nogil: bool
    cpdef: bool
    names: frozenset[str] = field(default_factory=frozenset, compare=False)


@dataclass(frozen=True)
class CythonModule:
    path: str
    functions: tuple[CythonFunction, ...]
    outside_hash: str

    def by_name(self) -> dict[str, CythonFunction]:
        return {f.name: f for f in self.functions}

    def function_at(self, line: int) -> CythonFunction | None:
        """The function whose span holds ``line`` (spans do not nest)."""
        for f in self.functions:
            if f.start <= line <= f.end:
                return f
        return None


def symbol_id(path: str, function: str) -> str:
    """How evidence names a Cython function: ``<path>::<qualified name>``."""
    return f"{path}::{function}"


def _code(line: str) -> str:
    """The line without its comment (a ``#`` inside a string is rare enough
    in a header or a mention to be read as a comment)."""
    return line.split("#", 1)[0]


def _significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _in_strings(lines: list[str]) -> list[bool]:
    """For each line, whether it starts inside a triple-quoted string: such
    a line (a docstring's continuation, often at column 0) says nothing about
    where a block ends."""
    out = []
    quote: str | None = None  # the open triple quote
    for line in lines:
        out.append(quote is not None)
        k = 0
        while k < len(line):
            if quote is not None:
                end = line.find(quote, k)
                if end < 0:
                    break
                quote, k = None, end + 3
                continue
            ch = line[k]
            if ch == "#":
                break
            if line.startswith(('"""', "'''"), k):
                quote, k = line[k : k + 3], k + 3
                continue
            if ch in "\"'":  # a one-line string: skip to its closing quote
                close = k + 1
                while close < len(line) and line[close] != ch:
                    close += 2 if line[close] == "\\" else 1
                k = close + 1
                continue
            k += 1
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _hash(lines: Iterable[str], *, directives: bool = False) -> str:
    digest = hashlib.sha256()
    for line in lines:
        if _significant(line) or (directives and _DIRECTIVE.match(line)):
            digest.update(line.rstrip().encode("utf-8", "surrogateescape") + b"\n")
    return digest.hexdigest()[:24]


def _function_name(header: str) -> str | None:
    """The name a function header defines, or None for anything else (a
    declaration such as ``cdef int64_t **x = <int64_t**>malloc(n)``)."""
    rest = re.sub(r"^\s*(cpdef|cdef|def)\b", "", header)
    rest = rest.lstrip()
    if rest.startswith("("):  # a C tuple return type: cdef (int, int) f(...)
        depth = 0
        for k, ch in enumerate(rest):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth == 0:
                rest = rest[k + 1 :]
                break
    m = _CALLABLE.search(rest)
    if m is None or "=" in rest[: m.start()]:
        return None
    return m.group(1)


def read(path: str, text: str) -> CythonModule:
    lines = text.split("\n")
    n = len(lines)
    in_string = _in_strings(lines)

    def structural(k: int) -> bool:
        return _significant(lines[k]) and not in_string[k]

    functions: list[CythonFunction] = []
    seen: dict[str, int] = {}
    covered: set[int] = set()  # 0-based line indexes inside a function's span
    scopes: list[tuple[int, str]] = []  # (indent, class name)
    i = 0
    while i < n:
        line = lines[i]
        if not structural(i):
            i += 1
            continue
        indent = _indent(line)
        while scopes and indent <= scopes[-1][0]:
            scopes.pop()
        m = _CLASS.match(line)
        if m and _code(line).rstrip().endswith(":"):
            scopes.append((indent, m.group(2)))
            i += 1
            continue
        if not _HEAD.match(line) or _NOT_FUNC.match(line) or "(" not in _code(line):
            i += 1
            continue
        # The header, up to ':' at bracket depth 0.
        header, j, depth, is_function = "", i, 0, False
        while j < n:
            part = _code(lines[j])
            header += " " + part.strip()
            depth += part.count("(") + part.count("[") - part.count(")") - part.count("]")
            stripped = part.rstrip()
            if depth <= 0 and not stripped.endswith("\\"):
                is_function = stripped.endswith(":")
                break
            j += 1
        name = _function_name(header) if is_function else None
        if name is None:
            i += 1
            continue
        last = j
        k = j + 1
        while k < n:
            if structural(k):
                if _indent(lines[k]) <= indent:
                    break
                last = k
            elif _significant(lines[k]):
                last = k  # inside a string that started in the body
            k += 1
        first = i
        while first > 0 and lines[first - 1].strip().startswith("@"):
            if _indent(lines[first - 1]) != indent:
                break
            first -= 1
        qualified = ".".join([c for _, c in scopes] + [name])
        seen[qualified] = seen.get(qualified, 0) + 1
        if seen[qualified] > 1:
            qualified = f"{qualified}#{seen[qualified]}"
        names: set[str] = set()
        for b in range(j + 1, last + 1):
            names.update(_WORD.findall(_code(lines[b])))
        tail = header.rsplit(")", 1)[-1]
        functions.append(
            CythonFunction(
                name=qualified,
                start=first + 1,
                end=last + 1,
                body_hash=_hash(lines[first : last + 1]),
                nogil=bool(re.search(r"\bnogil\b", tail)),
                cpdef=header.lstrip().startswith("cpdef"),
                names=frozenset(names),
            )
        )
        covered.update(range(first, last + 1))
        i = last + 1  # what is nested in the function is part of it
    outside = _hash((line for k, line in enumerate(lines) if k not in covered), directives=True)
    return CythonModule(path, tuple(functions), outside)


@dataclass(frozen=True)
class CythonChanges:
    """What differs between two snapshots' Cython sources."""

    # Functions whose body changed, present on both sides: (path, name).
    functions: tuple[tuple[str, str], ...] = ()
    # Files changed in a way not attributed to a function: path -> why.
    files: tuple[tuple[str, str], ...] = ()


def cython_changes(
    before: dict[str, CythonModule], after: dict[str, CythonModule]
) -> CythonChanges:
    """Function-level differences. An added or deleted file, a change outside
    every function, and an added, deleted or renamed function are file-level:
    a new override changes dispatch for code that never names it, and a
    vanished one is no longer a function the evidence can be asked about."""
    functions: list[tuple[str, str]] = []
    files: list[tuple[str, str]] = []
    for path in sorted(before.keys() | after.keys()):
        a, b = before.get(path), after.get(path)
        if a is None or b is None:
            files.append((path, "added" if a is None else "deleted"))
            continue
        if a.outside_hash != b.outside_hash:
            files.append((path, "changed outside its functions"))
            continue
        fa, fb = a.by_name(), b.by_name()
        if fa.keys() != fb.keys():
            added = sorted(fb.keys() - fa.keys())
            gone = sorted(fa.keys() - fb.keys())
            what = ", ".join([f"{x} added" for x in added[:2]] + [f"{x} deleted" for x in gone[:2]])
            files.append((path, f"functions added or deleted ({what})"))
            continue
        functions.extend((path, f) for f in sorted(fa) if fa[f].body_hash != fb[f].body_hash)
    return CythonChanges(tuple(functions), tuple(files))

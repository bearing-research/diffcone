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
  which change how every function in the file is compiled, and lines inside
  a triple-quoted string, which are part of its value. Everything
  outside the functions (directives, ``cimport``, ``ctypedef``, structs,
  class headers and attribute declarations, constants, ``include``) is
  hashed together.
* Outside the functions the file is also read as statements, with Python's
  ``tokenize`` (Cython's lexical syntax is Python's): each statement's
  scope (the module or a class), the names it binds, whether Python code
  can see them, and a hash (roadmap item 8). One statement per item of an
  ``import``/``cimport`` list; the declarator of a ``cdef``, ``ctypedef`` or
  ``DEF`` declaration and of an extern or ``.pxd`` function declaration;
  assignment and ``del`` targets; a struct, union, enum, fused type or
  ``cppclass`` with its members as one statement (their order sets enum
  values and layout); a class header with its bases. A statement that binds
  nothing by name (a bare call, a docstring, ``include``, ``IF``, a
  compound statement, a star import) is *unbounded*. A file ``tokenize``
  cannot read has no statements, and any change outside its functions
  stays file-level.
"""

from __future__ import annotations

import hashlib
import io
import keyword
import re
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass, field

CYTHON_SUFFIXES = (".pyx", ".pxd", ".pxi")

_HEAD = re.compile(r"^(\s*)(cpdef|cdef|def)\b")
_UNPROFILED = re.compile(r"@\s*(?:cython\.)?(?:profile|linetrace)\s*\(\s*False\s*\)")
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
    python: bool = False  # def or cpdef: Python code can see it
    # ``@cython.profile(False)`` or ``@cython.linetrace(False)``: a profiled
    # build never reports it, like a ``nogil`` function.
    unprofiled: bool = False

    @property
    def scope(self) -> str:
        """The class holding it, or "" for a module-level function."""
        return self.name.rsplit(".", 1)[0] if "." in self.name else ""

    @property
    def simple_name(self) -> str:
        return self.name.rsplit(".", 1)[-1].split("#")[0]


# Statement kinds.
IMPORT = "import"  # one item of an import or cimport list
DECLARATION = "declaration"  # cdef, ctypedef, DEF, extern and .pxd declarations
ASSIGNMENT = "assignment"  # Python assignment or del at module or class level
TYPE = "type"  # struct, union, enum, fused type or cppclass, members included
CLASS = "class"  # a class header
CODE = "code"  # binds nothing by name: unbounded


@dataclass(frozen=True)
class CythonStatement:
    """A statement outside every function."""

    scope: str  # "" for the module, else the class's qualified name
    kind: str
    names: tuple[str, ...]  # the names it binds; () when unbounded
    visible: bool  # whether Python code can see those names
    hash: str
    line: int = field(compare=False)
    why: str = ""  # for CODE: what it is
    bases: tuple[str, ...] = ()  # for CLASS: the names in its bases
    # For IMPORT: the module named as written, then the name imported from
    # it (``.np_datetime npy_datetimestruct``, ``pandas._libs util``).
    module: str = ""


@dataclass(frozen=True)
class CythonModule:
    path: str
    functions: tuple[CythonFunction, ...]
    outside_hash: str
    # None when tokenize could not read the file.
    statements: tuple[CythonStatement, ...] | None = None

    def by_name(self) -> dict[str, CythonFunction]:
        return {f.name: f for f in self.functions}

    def function_at(self, line: int) -> CythonFunction | None:
        """The function whose span holds ``line`` (spans do not nest)."""
        for f in self.functions:
            if f.start <= line <= f.end:
                return f
        return None


def pxd_stem(path: str) -> str:
    """The name a ``.pxd`` file is cimported by: its stem, or for a
    package's ``__init__.pxd`` the package directory's name."""
    parts = path.rsplit(".", 1)[0].split("/")
    return parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]


def names_module(statement: CythonStatement, stem: str) -> bool:
    """Whether an import statement may name the module ``stem`` (any
    component of what it names, so ``from pandas._libs cimport util`` names
    ``util``, and a relative import is not resolved)."""
    return stem in re.split(r"[.\s]+", statement.module)


def symbol_id(path: str, function: str) -> str:
    """How evidence names a Cython function: ``<path>::<qualified name>``."""
    return f"{path}::{function}"


def _significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _in_strings(lines: list[str]) -> tuple[list[bool], list[str]]:
    """For each line, whether it starts inside a triple-quoted string (such
    a line, a docstring's continuation often at column 0, says nothing about
    where a block ends), and the line without its comment: a ``#`` inside a
    string, one-line or triple-quoted, is not one."""
    out: list[bool] = []
    code: list[str] = []
    quote: str | None = None  # the open triple quote
    for line in lines:
        out.append(quote is not None)
        cut = len(line)
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
                cut = k
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
        code.append(line[:cut])
    return out, code


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _hash(lines: Iterable[tuple[str, bool]], *, directives: bool = False) -> str:
    """Hash (line, inside a string) pairs. Blank and comment-only lines say
    nothing, except inside a triple-quoted string, where every line, blank or
    starting with ``#``, is part of the value, trailing whitespace included."""
    digest = hashlib.sha256()
    for line, inside in lines:
        if inside:
            digest.update(b"\x00" + line.encode("utf-8", "surrogateescape") + b"\n")
        elif _significant(line) or (directives and _DIRECTIVE.match(line)):
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
    # Cython reads source with universal newlines, as Python does: a
    # checkout with CRLF line endings holds the same strings as one with LF.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    n = len(lines)
    in_string, code = _in_strings(lines)
    # A line that ends inside a string keeps its trailing whitespace there.
    raw = [in_string[k] or (k + 1 < n and in_string[k + 1]) for k in range(n)]

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
        if m and code[i].rstrip().endswith(":"):
            scopes.append((indent, m.group(2)))
            i += 1
            continue
        if not _HEAD.match(line) or _NOT_FUNC.match(line) or "(" not in code[i]:
            i += 1
            continue
        # The header, up to ':' at bracket depth 0.
        header, j, depth, is_function = "", i, 0, False
        while j < n:
            part = code[j]
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
        # What the header (types, defaults, decorators) and body mention; the
        # name the header defines is not a mention of itself.
        names: set[str] = set()
        for b in range(first, j + 1):
            names.update(_WORD.findall(code[b]))
        names.discard(name)
        for b in range(j + 1, last + 1):
            names.update(_WORD.findall(code[b]))
        tail = header.rsplit(")", 1)[-1]
        functions.append(
            CythonFunction(
                name=qualified,
                start=first + 1,
                end=last + 1,
                body_hash=_hash(zip(lines[first : last + 1], raw[first : last + 1], strict=True)),
                nogil=bool(re.search(r"\bnogil\b", tail)),
                cpdef=header.lstrip().startswith("cpdef"),
                names=frozenset(names),
                python=header.lstrip().startswith(("def", "cpdef")),
                unprofiled=any(_UNPROFILED.search(code[b]) for b in range(first, j + 1)),
            )
        )
        covered.update(range(first, last + 1))
        i = last + 1  # what is nested in the function is part of it
    outside = _hash(
        ((line, raw[k]) for k, line in enumerate(lines) if k not in covered),
        directives=True,
    )
    return CythonModule(path, tuple(functions), outside, _statements(text, covered))


# --------------------------------------------------------------------------- statements

# Words that qualify a C declaration and are never the name it declares.
_QUALIFIERS = frozenset(
    "cdef cpdef ctypedef public readonly api extern inline const volatile unsigned "
    "signed long short struct enum union packed nogil noexcept fused cppclass except "
    "gil static DEF class".split()
)
_AGGREGATES = frozenset({"struct", "union", "enum", "fused", "cppclass"})
_COMPOUND = frozenset("if elif else try except finally for while with async def match case".split())
_SKIP_TOKENS = (
    tokenize.NL,
    tokenize.COMMENT,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
)
_STRING_TOKENS = {tokenize.STRING} | {
    getattr(tokenize, n)
    for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
    if hasattr(tokenize, n)
}


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _depths(words: list[str]) -> list[int]:
    """Bracket depth before each word."""
    out, depth = [], 0
    for w in words:
        if w in (")", "]", "}"):
            depth -= 1
        out.append(depth)
        if w in ("(", "[", "{"):
            depth += 1
    return out


def _split(words: list[str], sep: str) -> list[list[str]]:
    """Split at ``sep`` outside brackets."""
    parts: list[list[str]] = [[]]
    for w, d in zip(words, _depths(words), strict=True):
        if w == sep and d == 0:
            parts.append([])
        else:
            parts[-1].append(w)
    return parts


def _identifier(word: str) -> bool:
    return word.isidentifier() and not keyword.iskeyword(word)


def _declarators(words: list[str]) -> list[str] | None:
    """The names a C declaration declares (``cdef int64_t a, b = 1``,
    ``int f(int x) nogil``, ``ctypedef (int, int) pair_t``, ``ctypedef int
    (*func_t)(int)``), or None when it cannot be read."""
    decl = _split(words, "=")[0]
    depths = _depths(decl)
    if decl and decl[0] == "(":  # a C tuple type comes first
        k = 1
        while k < len(decl) and not (decl[k] == ")" and depths[k] == 0):
            k += 1
        decl, depths = decl[k + 1 :], depths[k + 1 :]
    for k, w in enumerate(decl):
        if w == "(" and depths[k] == 0:
            if k + 1 < len(decl) and decl[k + 1] in ("*", "&"):  # a function pointer
                inner = [x for x in decl[k + 1 :] if _identifier(x) and x not in _QUALIFIERS]
                return inner[:1] or None
            before = [x for x in decl[:k] if _identifier(x) and x not in _QUALIFIERS]
            return before[-1:] or None
    names = []
    for part in _split(decl, ","):
        top = [w for w, d in zip(part, _depths(part), strict=True) if d == 0]
        found = [w for w in top if _identifier(w) and w not in _QUALIFIERS]
        if not found:
            return None
        names.append(found[-1])
    return names or None


def _import_items(words: list[str]) -> list[tuple[str, str, str]] | None:
    """(bound name, module, item text) per item of an import or cimport
    statement, or None for a star import."""
    if words[0] == "from":
        k = words.index("import") if "import" in words else words.index("cimport")
        module = "".join(words[1:k])
        items = [w for w in words[k + 1 :] if w not in ("(", ")")]
        out = []
        for part in _split(items, ","):
            if not part:
                continue
            if part == ["*"]:
                return None
            # The module and the name imported from it (a submodule, maybe).
            target = f"{module} {part[0]}"
            out.append((part[-1], target, f"{' '.join(words[: k + 1])} {' '.join(part)}"))
        return out
    out = []
    for part in _split(words[1:], ","):
        if not part:
            continue
        module = "".join(part[: part.index("as")] if "as" in part else part)
        # import a.b binds a; import a.b as c binds c.
        out.append((part[-1] if "as" in part else part[0], module, f"{words[0]} {' '.join(part)}"))
    return out


def _assigned(words: list[str]) -> list[str] | None:
    """The names a Python assignment, augmented or annotated, binds or
    changes (``a = b = 1``, ``x[k] += 1`` changes ``x``), or None."""
    depths = _depths(words)
    for k, w in enumerate(words):
        if depths[k] == 0 and w.endswith("=") and w not in ("==", "<=", ">=", "!=") and w != "=":
            targets = [words[:k]]  # augmented
            break
    else:
        parts = _split(words, "=")
        if len(parts) > 1:
            targets = parts[:-1]
        elif ":" in words:  # x: int
            targets = [words]
        else:
            return None
    names = []
    for target in targets:
        target = _split(target, ":")[0]  # an annotation
        top = [w for w, d in zip(target, _depths(target), strict=True) if d == 0]
        names += (
            [w for w in top if _identifier(w)][:1]
            if "." in top or "[" in top
            else [w for w in top if _identifier(w)]
        )
    return names or None


def _statements(text: str, covered: set[int]) -> tuple[CythonStatement, ...] | None:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    logical: list[list[tokenize.TokenInfo]] = []
    current: list[tokenize.TokenInfo] = []
    directives = [
        (tok.start[0], tok.string.strip())
        for tok in tokens
        if tok.type == tokenize.COMMENT and _DIRECTIVE.match(tok.string)
    ]
    for tok in tokens:
        if tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
            if current:
                logical.append(current)
                current = []
        elif tok.type not in _SKIP_TOKENS:
            current.append(tok)

    out: list[CythonStatement] = []
    scopes: list[tuple[int, str]] = []  # (column, class qualified name)
    # (column, kind, header words, statement row) for extern, declaration
    # and aggregate blocks; an aggregate collects its members' words.
    blocks: list[tuple[int, str, list[str], int]] = []
    members: list[list[str]] = []

    def scope() -> str:
        return scopes[-1][1] if scopes else ""

    def digest(*parts: str) -> str:
        return hashlib.sha256("\x00".join(parts).encode("utf-8", "surrogateescape")).hexdigest()[
            :24
        ]

    def add(
        kind: str, names, visible: bool, row: int, words, why: str = "", bases=(), module=""
    ) -> None:
        names = tuple(sorted(set(names)))
        if kind != CODE and any(_is_dunder(n) for n in names):
            kind, why = (
                CODE,
                f"binds a special name ({', '.join(n for n in names if _is_dunder(n))})",
            )
        if kind == CODE:
            names, visible = (), False
        text = " ".join(words) if isinstance(words, list) else words
        out.append(
            CythonStatement(
                scope(), kind, names, visible, digest(kind, text), row, why, bases, module
            )
        )

    for row, text in directives:
        add(CODE, (), False, row, text, why="a compiler directive")

    def close_aggregate() -> None:
        col, kind, header, row = blocks.pop()
        name = [w for w in header if _identifier(w) and w not in _QUALIFIERS and w != ":"]
        names = name[-1:]
        enum = "enum" in header
        for member in members:
            if enum:
                names += [w for w in _split(member, "=")[0] if _identifier(w)]
            elif "fused" not in header:
                names += _declarators([w for w in member if w not in _QUALIFIERS]) or []
        text = " ".join(header) + " | " + " | ".join(" ".join(m) for m in members)
        add(TYPE, names, header[0] == "cpdef", row, text)
        members.clear()

    for line in logical:
        row, col = line[0].start
        while blocks and col <= blocks[-1][0]:
            if blocks[-1][1] == TYPE:
                close_aggregate()
            else:
                blocks.pop()
        while scopes and col <= scopes[-1][0]:
            scopes.pop()
        if row - 1 in covered:
            continue
        words = [t.string for t in line]
        if blocks and blocks[-1][1] == TYPE:
            members.append(words)
            continue
        block = blocks[-1] if blocks else None
        first = words[0]
        header = words[-1] == ":"
        cdef = first in ("cdef", "cpdef", "ctypedef")

        if (first == "class" or (cdef and "class" in words[:4])) and header:
            k = words.index("class")
            name = words[k + 1]
            bases = tuple(w for w in words[k + 2 : -1] if _identifier(w))
            add(CLASS, [name], True, row, words, bases=bases)
            scopes.append((col, f"{scope()}.{name}" if scope() else name))
            continue
        if header and cdef and words[1:2] == ["extern"]:
            blocks.append((col, DECLARATION, words[:-1], row))
            continue
        if header and cdef and _AGGREGATES & set(words[:-1]):
            blocks.append((col, TYPE, words[:-1], row))
            continue
        if header and cdef and all(w in _QUALIFIERS for w in words[:-1]):  # cdef:, cdef public:
            blocks.append((col, DECLARATION, words[:-1], row))
            continue
        if first in ("IF", "ELIF", "ELSE"):
            add(CODE, (), False, row, words, why="compile-time IF")
            continue
        if header or first in _COMPOUND:
            add(CODE, (), False, row, words, why="code that runs at import")
            continue
        if first == "include":
            add(CODE, (), False, row, words, why="include")
            continue
        if first in ("import", "cimport") or (
            first == "from" and ("import" in words or "cimport" in words)
        ):
            items = _import_items(words)
            if items is None:
                add(CODE, (), False, row, words, why="a star import")
                continue
            python = "cimport" not in words
            for name, module, item in items:
                add(IMPORT, [name], python, row, item, module=module)
            continue
        if first in ("pass", "...") and len(words) == 1:
            continue
        if all(t.type in _STRING_TOKENS for t in line):
            add(CODE, (), False, row, words, why="a docstring")
            continue
        prefix = block[2] if block is not None else []
        if cdef or first == "DEF" or (block is not None and block[1] == DECLARATION):
            body = [w for w in words if w not in _QUALIFIERS] if first != "DEF" else words[1:2]
            if cdef and words[1:2] == ["class"]:  # cdef class X (a forward declaration)
                body = words[2:3]
            names = _declarators(body) if first != "DEF" else body
            if not names:
                add(CODE, (), False, row, words, why="a declaration diffcone cannot read")
                continue
            qualifiers = set(prefix) | set(words)
            visible = bool({"public", "readonly", "cpdef"} & qualifiers)
            add(DECLARATION, names, visible, row, prefix + ["|"] + words)
            continue
        if first == "del":
            add(ASSIGNMENT, [w for w in words[1:] if _identifier(w)], True, row, words)
            continue
        names = _assigned(words)
        if names:
            add(ASSIGNMENT, names, True, row, words)
        else:
            add(CODE, (), False, row, words, why="an expression statement that runs at import")
    while blocks:
        if blocks[-1][1] == TYPE:
            close_aggregate()
        else:
            blocks.pop()
    return tuple(out)


@dataclass(frozen=True)
class CythonName:
    """A name bound outside every function body whose binding changed, or a
    function added or deleted (roadmap item 8)."""

    path: str
    scope: str  # "" for the module, else the class's qualified name
    name: str
    change: str  # added, deleted or changed
    visible: bool  # Python code can see it
    attribute: bool  # a class attribute declared or assigned in the class body


@dataclass(frozen=True)
class CythonChanges:
    """What differs between two snapshots' Cython sources."""

    # Functions whose body changed, present on both sides: (path, name).
    functions: tuple[tuple[str, str], ...] = ()
    # Files changed in a way not attributed to a function or name: path -> why.
    files: tuple[tuple[str, str], ...] = ()
    names: tuple[CythonName, ...] = ()


def _empty(path: str) -> CythonModule:
    return CythonModule(path, (), "", ())


def cython_changes(
    before: dict[str, CythonModule], after: dict[str, CythonModule]
) -> CythonChanges:
    """Function bodies that changed, names whose binding changed outside the
    functions, and what neither bounds, file by file.

    An added or deleted ``.pyx`` or ``.pxi`` file is file-level (a new
    extension module, or an ``include`` elsewhere); a ``.pxd`` file's
    declarations are names like any other. A file without statements
    (``tokenize`` could not read it) is file-level for any change outside
    its functions."""
    functions: list[tuple[str, str]] = []
    files: list[tuple[str, str]] = []
    names: list[CythonName] = []
    for path in sorted(before.keys() | after.keys()):
        a, b = before.get(path), after.get(path)
        if a is None or b is None:
            if not path.endswith(".pxd"):
                files.append((path, "added" if a is None else "deleted"))
                continue
            a, b = a or _empty(path), b or _empty(path)
        fa, fb = a.by_name(), b.by_name()
        functions.extend(
            (path, f) for f in sorted(fa.keys() & fb.keys()) if fa[f].body_hash != fb[f].body_hash
        )
        if a.statements is None or b.statements is None:
            if a.outside_hash != b.outside_hash or fa.keys() != fb.keys():
                files.append((path, "changed outside its functions"))
            continue
        why = _outside(path, a, b, names)
        if why is not None:
            files.append((path, why))
    return CythonChanges(tuple(functions), tuple(files), tuple(names))


def _outside(path: str, a: CythonModule, b: CythonModule, out: list[CythonName]) -> str | None:
    """Append the names whose binding changed between ``a`` and ``b``; return
    why the change is file-level instead, or None."""
    sa, sb = a.statements or (), b.statements or ()
    # Code that binds nothing by name: any change is unbounded.
    code_a = [(s.scope, s.hash) for s in sa if s.kind == CODE]
    code_b = [(s.scope, s.hash) for s in sb if s.kind == CODE]
    if code_a != code_b:
        changed = [
            s
            for s in (*sa, *sb)
            if s.kind == CODE and (s.scope, s.hash) not in set(code_a) & set(code_b)
        ]
        what = changed[0].why if changed else "statements that bind no name moved"
        return f"changed outside its functions: {what} (line {changed[0].line if changed else '?'})"

    def bindings(statements, functions):
        by_key: dict[tuple[str, str], list[str]] = {}
        meta: dict[tuple[str, str], tuple[bool, bool, str]] = {}  # visible, attribute, kind
        for s in statements:
            if s.kind == CODE:
                continue
            for n in s.names:
                by_key.setdefault((s.scope, n), []).append(s.hash)
                visible, attribute, _ = meta.get((s.scope, n), (False, False, ""))
                meta[(s.scope, n)] = (
                    visible or s.visible,
                    attribute or (bool(s.scope) and s.kind != CLASS),
                    s.kind,
                )
        for f in functions:
            key = (f.scope, f.simple_name)
            # Whether Python can see it is part of the binding (def -> cdef).
            by_key.setdefault(key, []).append(f"function {f.name} {f.python}")
            visible, attribute, kind = meta.get(key, (False, False, "function"))
            meta[key] = (visible or f.python, attribute, kind)
        return by_key, meta

    ba, ma = bindings(sa, a.functions)
    bb, mb = bindings(sb, b.functions)
    changed = {k for k in ba.keys() | bb.keys() if ba.get(k) != bb.get(k)}
    for key in sorted(changed):
        scope, name = key
        visible_a, attribute_a, kind_a = ma.get(key, (False, False, ""))
        visible_b, attribute_b, kind_b = mb.get(key, (False, False, ""))
        label = f"{scope}.{name}" if scope else name
        if _is_dunder(name):
            return f"changed outside its functions: special name {label} bound differently"
        if key in ba and key in bb and CLASS in (kind_a, kind_b):
            return f"changed outside its functions: the class statement of {label} changed"
        change = "added" if key not in ba else "deleted" if key not in bb else "changed"
        visible = visible_a or visible_b
        if change == "deleted" and visible and not scope and path.endswith(".pyx"):
            return (
                f"{label} deleted: Python code importing it from the compiled module fails "
                "at import, and imports of compiled modules' names are not indexed"
            )
        out.append(CythonName(path, scope, name, change, visible, attribute_a or attribute_b))
    # The other statements keep their order (a value read at import can
    # depend on it); imports are exempt, their order binds nothing.
    order_a = [s.hash for s in sa if s.kind not in (CODE, IMPORT) and not changed & _keys(s)]
    order_b = [s.hash for s in sb if s.kind not in (CODE, IMPORT) and not changed & _keys(s)]
    if order_a != order_b:
        return "changed outside its functions: statements reordered"
    return None


def _keys(statement: CythonStatement) -> set[tuple[str, str]]:
    return {(statement.scope, n) for n in statement.names}

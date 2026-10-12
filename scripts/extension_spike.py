"""Spike for roadmap item 18: compiled changes planned at the extension
module's Python surface, from a recording made on an ordinary build.

Subcommands (each writes JSON under ``--data``):

* ``buildmap``: which sources make which extension module at a commit
  (Cython's ``pkg/mod.pyx`` convention plus a tolerant reader of
  ``meson.build`` files), the include graph (``cimport``, ``include``,
  ``cdef extern from``, ``#include "..."``, followed by name), and the
  dependencies between extension modules (``cimport`` of another module's
  ``.pxd``, a Python-level import of it, its dotted name in a string such as
  a ``PyCapsule_Import``).
* ``surface``: imports every mapped module in the project's environment
  (``--python``) and lists its names and, per type it defines, the names
  in the type's own ``__dict__``. A name is the module's own when its
  ``__module__`` is the module or its code (functions, methods, property
  getters) was compiled from a file the build map gives the module: pandas
  relabels ``Timestamp``, ``Period``, ... as ``pandas``.
* ``precision``: for every compiled-source commit among the last ``--last``
  first-parent commits before X, the coarse selection at X; also each
  module alone (``per_module.json``).
* ``oracle``: folds a line-traced run (``extension_spike/extspike_oracle.py``)
  into per-test compiled functions.
* ``recall``: item 7's replay set (Cython body edits of functions that still
  exist at X) against the oracle; ``misses`` classifies what the direct
  count missed; ``variants`` scores narrower stand-ins for rules 2 and 3.
* ``special``: the special-method check (tests running a file's compiled
  code with no reader of its closure's surface in their record).
* ``outcomes``: a commit's coarse selection against two JUnit files (a full
  run of C with its compiled files put back to C^, and of C).

The coarse rule (item 18, "Planning (evidence mode)"): the changed files'
modules and every module importing or cimporting one of them, transitively
(the closure); their surface names; the Python symbols at X reading one,
in three variants (``Selector.readers_for``: ``all`` as item 18 is written,
``module`` without the types' attribute names, ``receiver`` only where the
reference can be to the module). Two counts per selection:

* *direct* (rule 1 alone): the tests whose record executed a reader, plus
  every test the evidence planner always selects (no record, text code,
  subprocess). Readers that ran at import or in a hook are flagged.
* *planned* (rules 1 to 4 as the evidence planner would apply them): each
  reader handed to ``_Observers._reader`` and settled, so import-time
  readers escalate, hook readers select everything, and the planner's
  cross-test state rule and lookup sites apply.

The drivers in ``extension_spike/`` (``common.sh``: paths, X) rebuild the
environments and data under the worktree's ``.spike/``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pickle
import re
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMPILED_SUFFIXES = (".pyx", ".pxd", ".pxi", ".pxi.in", ".c", ".cpp", ".h", ".hpp")
BUILD_FILES = ("meson.build", "meson.options", "meson_options.txt", "setup.py")


def is_compiled(path: str) -> bool:
    return path.endswith(COMPILED_SUFFIXES) or path.rsplit("/", 1)[-1] in BUILD_FILES


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def files_at(repo: Path, rev: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", rev).split("\n")[:-1]


def contents_at(repo: Path, rev: str, paths: Iterable[str]) -> dict[str, str]:
    """The text of ``paths`` at ``rev``, through one ``cat-file --batch``."""
    wanted = list(paths)
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        input="".join(f"{rev}:{p}\n" for p in wanted).encode(),
        capture_output=True,
        check=True,
    )
    out, data, pos = {}, proc.stdout, 0
    for path in wanted:
        end = data.index(b"\n", pos)
        header = data[pos:end].split()
        pos = end + 1
        if header[-1] == b"missing":
            continue
        size = int(header[2])
        out[path] = data[pos : pos + size].decode("utf-8", "surrogateescape")
        pos += size + 1
    return out


# --------------------------------------------------------------------------- meson


@dataclass
class Unknown:
    why: str


@dataclass
class Choice:
    options: list[Any]


@dataclass
class Generated:
    inputs: list[Any]
    outputs: list[str]
    directory: str


@dataclass
class Library:
    sources: list[Any]
    deps: list[Any]


@dataclass
class Dependency:
    sources: list[Any]
    link_with: list[Any]
    deps: list[Any]


@dataclass
class IncludeDirs:
    paths: list[str]


@dataclass
class Opaque:
    """An object the reader does not model (``py``, ``cc``, a subproject)."""

    what: str


@dataclass
class Extension:
    name: Any
    sources: list[Any]
    deps: list[Any]
    subdir: Any
    directory: str
    line: int


_TOKEN = re.compile(
    r"""(?P<ws>[ \t\r]+|\\\n)
    |(?P<comment>\#[^\n]*)
    |(?P<nl>\n)
    |(?P<string>'''.*?'''|'(?:\\.|[^'\\\n])*')
    |(?P<number>\d+)
    |(?P<ident>[A-Za-z_]\w*)
    |(?P<op>\+=|==|!=|<=|>=|[()\[\]{},:.=+\-*/%<>?])""",
    re.S | re.X,
)
_KEYWORDS = {"if", "elif", "else", "endif", "foreach", "endforeach", "and", "or", "not", "in"}


def _tokens(text: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    depth, pos, line = 0, 0, 1
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise SyntaxError(f"line {line}: cannot read {text[pos : pos + 20]!r}")
        kind = m.lastgroup or ""
        value = m.group()
        pos = m.end()
        if kind in ("ws", "comment"):
            line += value.count("\n")
            continue
        if kind == "nl":
            line += 1
            if depth == 0 and out and out[-1][0] != "nl":
                out.append(("nl", "", line))
            continue
        if kind == "op" and value in "([{":
            depth += 1
        elif kind == "op" and value in ")]}":
            depth -= 1
        if kind == "string":
            line += value.count("\n")
            value = value[3:-3] if value.startswith("'''") else value[1:-1]
        if kind == "ident" and value in _KEYWORDS:
            kind = "kw"
        out.append((kind, value, line))
    out.append(("nl", "", line))
    out.append(("eof", "", line))
    return out


class _Parser:
    """Meson's grammar, enough of it to read build files as data."""

    def __init__(self, text: str) -> None:
        self.t = _tokens(text)
        self.i = 0

    def peek(self) -> tuple[str, str, int]:
        return self.t[self.i]

    def take(self, value: str | None = None) -> tuple[str, str, int]:
        tok = self.t[self.i]
        if value is not None and tok[1] != value:
            raise SyntaxError(f"line {tok[2]}: expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def block(self, ends: tuple[str, ...]) -> list:
        out = []
        while True:
            kind, value, _ = self.peek()
            if kind == "nl":
                self.i += 1
                continue
            if kind == "eof" or (kind == "kw" and value in ends):
                return out
            out.append(self.statement())

    def statement(self) -> tuple:
        kind, value, line = self.peek()
        if kind == "kw" and value == "if":
            branches = []
            while True:
                kw = self.take()[1]
                cond = self.expr() if kw in ("if", "elif") else None
                body = self.block(("elif", "else", "endif"))
                branches.append((cond, body))
                if self.peek()[1] == "endif":
                    self.take()
                    return ("if", branches, line)
        if kind == "kw" and value == "foreach":
            self.take()
            names = [self.take()[1]]
            while self.peek()[1] == ",":
                self.take()
                names.append(self.take()[1])
            self.take(":")
            items = self.expr()
            body = self.block(("endforeach",))
            self.take("endforeach")
            return ("foreach", names, items, body, line)
        if kind == "ident" and self.t[self.i + 1][1] in ("=", "+="):
            self.take()
            op = self.take()[1]
            return ("assign", value, op, self.expr(), line)
        return ("expr", self.expr(), line)

    def expr(self) -> tuple:
        cond = self.binary(0)
        if self.peek()[1] == "?":
            self.take()
            a = self.expr()
            self.take(":")
            return ("ternary", cond, a, self.expr())
        return cond

    _LEVELS: tuple[tuple[str, ...], ...] = (
        ("or",),
        ("and",),
        ("==", "!=", "<", ">", "<=", ">=", "in"),
        ("+", "-"),
        ("*", "/", "%"),
    )

    def binary(self, level: int) -> tuple:
        if level == len(self._LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek()[1] in self._LEVELS[level]:
            op = self.take()[1]
            left = ("bin", op, left, self.binary(level + 1))
        return left

    def unary(self) -> tuple:
        if self.peek()[1] in ("not", "-"):
            op = self.take()[1]
            return ("unary", op, self.unary())
        return self.postfix()

    def postfix(self) -> tuple:
        node = self.primary()
        while True:
            value = self.peek()[1]
            if value == "(":
                node = ("call", node, *self.args())
            elif value == ".":
                self.take()
                name = self.take()[1]
                if self.peek()[1] == "(":
                    node = ("method", node, name, *self.args())
                else:
                    node = ("attr", node, name)
            elif value == "[":
                self.take()
                index = self.expr()
                self.take("]")
                node = ("index", node, index)
            else:
                return node

    def args(self) -> tuple[list, dict]:
        self.take("(")
        pos, kw = [], {}
        while self.peek()[1] != ")":
            if self.peek()[0] == "ident" and self.t[self.i + 1][1] == ":":
                name = self.take()[1]
                self.take(":")
                kw[name] = self.expr()
            else:
                pos.append(self.expr())
            if self.peek()[1] == ",":
                self.take()
        self.take(")")
        return pos, kw

    def primary(self) -> tuple:
        kind, value, line = self.take()
        if kind == "string":
            return ("lit", value)
        if kind == "number":
            return ("lit", int(value))
        if kind == "ident":
            if value in ("true", "false"):
                return ("lit", value == "true")
            return ("name", value, line)
        if value == "(":
            inner = self.expr()
            self.take(")")
            return inner
        if value == "[":
            items = []
            while self.peek()[1] != "]":
                items.append(self.expr())
                if self.peek()[1] == ",":
                    self.take()
            self.take("]")
            return ("list", items)
        if value == "{":
            pairs = []
            while self.peek()[1] != "}":
                key = self.expr()
                self.take(":")
                pairs.append((key, self.expr()))
                if self.peek()[1] == ",":
                    self.take()
            self.take("}")
            return ("dict", pairs)
        raise SyntaxError(f"line {line}: unexpected {value!r}")


def _norm(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _join(directory: str, name: str) -> str:
    return _norm(f"{directory}/{name}" if directory else name)


def _flatten(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [x for v in value for x in _flatten(v)]
    if isinstance(value, Choice):
        return [x for v in value.options for x in _flatten(v)]
    if value is None:
        return []
    return [value]


class MesonReader:
    """Reads a project's ``meson.build`` files as data. Conditions are not
    evaluated: every branch of an ``if`` runs, and a name the branches bind
    differently becomes a ``Choice`` of their values, so a source chosen by
    platform is claimed on every platform."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.extensions: list[Extension] = []
        self.include_dirs: set[str] = set()
        self.generated: dict[str, list[str]] = {}  # output path (in the source dir) -> inputs
        self.notes: list[str] = []
        self.env: dict[str, Any] = {}
        self.directory = ""

    def run(self, directory: str = "") -> None:
        path = _join(directory, "meson.build")
        text = self.files.get(path)
        if text is None:
            self.notes.append(f"{path}: missing")
            return
        try:
            program = _Parser(text).block(())
        except SyntaxError as e:
            self.notes.append(f"{path}: not read ({e})")
            return
        saved, self.directory = self.directory, directory
        self._run(program, path)
        self.directory = saved

    def _run(self, program: list, path: str) -> None:
        for stmt in program:
            self._statement(stmt, path)

    def _statement(self, stmt: tuple, path: str) -> None:
        kind = stmt[0]
        if kind == "assign":
            _, name, op, expr, _ = stmt
            value = self._eval(expr, path)
            if op == "+=":
                old = self.env.get(name)
                value = self._add(old, value)
            self.env[name] = value
        elif kind == "expr":
            self._eval(stmt[1], path)
        elif kind == "if":
            before = dict(self.env)
            ends = []
            has_else = False
            for cond, body in stmt[1]:
                if cond is None:
                    has_else = True
                else:
                    self._eval(cond, path)
                self.env = dict(before)
                self._run(body, path)
                ends.append(self.env)
            if not has_else:
                ends.append(before)
            merged: dict[str, Any] = {}
            for name in {k for e in ends for k in e}:
                values = [e.get(name) for e in ends]
                unique: list[Any] = []
                for v in values:
                    if v is not None and not any(v is u or v == u for u in unique):
                        unique.append(v)
                merged[name] = unique[0] if len(unique) == 1 else Choice(unique)
            self.env = merged
        elif kind == "foreach":
            _, names, items_expr, body, line = stmt
            items = self._eval(items_expr, path)
            if isinstance(items, dict):
                pairs: list[tuple[Any, ...]] = list(items.items())
            elif isinstance(items, list):
                pairs = [(x,) for x in items]
            else:
                self.notes.append(f"{path}:{line}: foreach over {items!r}")
                pairs = [tuple(Unknown("foreach item") for _ in names)]
            for pair in pairs:
                for name, value in zip(names, pair, strict=False):
                    self.env[name] = value
                self._run(body, path)

    def _add(self, a: Any, b: Any) -> Any:
        if isinstance(a, list):
            return a + (b if isinstance(b, list) else [b])
        if isinstance(a, str) and isinstance(b, str):
            return a + b
        if isinstance(a, Choice):
            return Choice([self._add(o, b) for o in a.options])
        if isinstance(b, Choice):
            return Choice([self._add(a, o) for o in b.options])
        return Unknown(f"{a!r} + {b!r}")

    def _eval(self, node: tuple, path: str) -> Any:
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "name":
            if node[1] in self.env:
                return self.env[node[1]]
            return Opaque(node[1])
        if kind == "list":
            return [self._eval(x, path) for x in node[1]]
        if kind == "dict":
            out = {}
            for k, v in node[1]:
                key = self._eval(k, path)
                if isinstance(key, str):
                    out[key] = self._eval(v, path)
                else:
                    self.notes.append(f"{path}: dict key {key!r}")
            return out
        if kind == "ternary":
            self._eval(node[1], path)
            return Choice([self._eval(node[2], path), self._eval(node[3], path)])
        if kind == "bin":
            a, b = self._eval(node[2], path), self._eval(node[3], path)
            if node[1] == "+":
                return self._add(a, b)
            if node[1] == "/" and isinstance(a, str) and isinstance(b, str):
                return f"{a}/{b}"
            return Unknown(f"operator {node[1]}")
        if kind == "unary":
            self._eval(node[2], path)
            return Unknown(f"operator {node[1]}")
        if kind == "index":
            container, key = self._eval(node[1], path), self._eval(node[2], path)
            if isinstance(container, (dict, list)):
                try:
                    return container[key]
                except (KeyError, IndexError, TypeError):
                    return Unknown("index")
            return Unknown("index")
        if kind == "attr":
            return Opaque(f"{node[2]}")
        if kind == "method":
            return self._method(node, path)
        if kind == "call":
            return self._call(node, path)
        return Unknown(kind)

    def _method(self, node: tuple, path: str) -> Any:
        _, target_node, name, pos_nodes, kw_nodes = node
        target = self._eval(target_node, path)
        pos = [self._eval(x, path) for x in pos_nodes]
        kw = {k: self._eval(v, path) for k, v in kw_nodes.items()}
        if isinstance(target, Choice):
            return Choice([self._method_on(o, name, pos, kw, node, path) for o in target.options])
        return self._method_on(target, name, pos, kw, node, path)

    def _method_on(
        self, target: Any, name: str, pos: list, kw: dict, node: tuple, path: str
    ) -> Any:
        if isinstance(target, dict) and name == "get":
            key = pos[0] if pos else None
            if isinstance(key, str) and key in target:
                return target[key]
            return pos[1] if len(pos) > 1 else Unknown(f"get({key!r})")
        if isinstance(target, str) and name == "format":
            options: list[str] = [target]
            for i, arg in enumerate(pos):
                values = arg.options if isinstance(arg, Choice) else [arg]
                if not all(isinstance(v, str) for v in values):
                    return Unknown("format")
                options = [o.replace(f"@{i}@", v) for o in options for v in values]
            return options[0] if len(options) == 1 else Choice(list(options))
        if name == "extension_module" and isinstance(target, (Opaque, Unknown)):
            return self._extension(pos, kw, node, path)
        if isinstance(target, Opaque) and target.what == "meson":
            if name == "current_source_dir":
                return self.directory
            return Unknown(f"meson.{name}()")
        if isinstance(target, (Opaque, Unknown)):
            return Opaque(f"{name}()")  # a module or compiler object, or what it returns
        return Unknown(f"{name}()")

    def _call(self, node: tuple, path: str) -> Any:
        _, fn_node, pos_nodes, kw_nodes = node
        fn = fn_node[1] if fn_node[0] == "name" else ""
        if fn == "subdir":
            arg = self._eval(pos_nodes[0], path)
            if isinstance(arg, str):
                self.run(_join(self.directory, arg))
            else:
                self.notes.append(f"{path}: subdir({arg!r})")
            return None
        pos = [self._eval(x, path) for x in pos_nodes]
        kw = {k: self._eval(v, path) for k, v in kw_nodes.items()}
        d = self.directory
        if fn == "files":
            return [_join(d, p) if isinstance(p, str) else p for p in _flatten(pos)]
        if fn == "include_directories":
            dirs = [_join(d, p) for p in _flatten(pos) if isinstance(p, str)]
            self.include_dirs.update(dirs)
            return IncludeDirs(dirs)
        if fn in ("custom_target", "configure_file"):
            inputs = [_join(d, p) if isinstance(p, str) else p for p in _flatten(kw.get("input"))]
            outputs = [p for p in _flatten(kw.get("output")) if isinstance(p, str)]
            for out in outputs:
                # A file generated from the build file alone (configure_file
                # with configuration data) is the build file's: build files
                # are claimed by no module.
                self.generated[_join(d, out)] = [p for p in inputs if isinstance(p, str)]
            return Generated(inputs, outputs, d)
        if fn in ("static_library", "library", "shared_library"):
            sources = [_join(d, p) if isinstance(p, str) else p for p in _flatten(pos[1:])]
            sources += [
                _join(d, p) if isinstance(p, str) else p for p in _flatten(kw.get("sources"))
            ]
            deps = _flatten(kw.get("dependencies")) + _flatten(kw.get("link_with"))
            return Library(sources, deps)
        if fn == "declare_dependency":
            return Dependency(
                [_join(d, p) if isinstance(p, str) else p for p in _flatten(kw.get("sources"))],
                _flatten(kw.get("link_with")),
                _flatten(kw.get("dependencies")),
            )
        return Opaque(f"{fn}()")

    def _extension(self, pos: list, kw: dict, node: tuple, path: str) -> Extension:
        d = self.directory
        sources = [_join(d, p) if isinstance(p, str) else p for p in _flatten(pos[1:])]
        sources += [_join(d, p) if isinstance(p, str) else p for p in _flatten(kw.get("sources"))]
        ext = Extension(
            pos[0] if pos else Unknown("no name"),
            sources,
            _flatten(kw.get("dependencies")) + _flatten(kw.get("link_with")),
            kw.get("subdir"),
            d,
            node[1][2] if node[1][0] == "name" else 0,
        )
        self.extensions.append(ext)
        return ext


# --------------------------------------------------------------------------- includes

_CIMPORT_FROM = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+cimport\s+(.+)$")
_CIMPORT = re.compile(r"^\s*cimport\s+(.+)$")
_PYIMPORT_FROM = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\s+(.+)$")
_PYIMPORT = re.compile(r"^\s*import\s+(.+)$")
_INCLUDE = re.compile(r"""^\s*include\s+["']([^"']+)["']""")
_EXTERN = re.compile(r"""\bextern\s+from\s+["']([^"'*]+)["']""")
_C_INCLUDE = re.compile(r'^\s*#\s*include\s+(?:"([^"]+)"|<([^>]+)>)')
_STRING_MODULE = re.compile(r"""["']([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+)["']""")


def _logical_lines(text: str) -> Iterator[str]:
    """Lines with comments dropped and bracketed continuations joined."""
    buf, depth = "", 0
    for raw in text.split("\n"):
        line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#include") else raw
        buf = f"{buf} {line}" if buf else line
        depth += line.count("(") - line.count(")")
        if line.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1]
            continue
        if depth <= 0:
            yield buf
            buf, depth = "", 0
    if buf:
        yield buf


def _names(clause: str) -> list[str]:
    clause = clause.strip().strip("()")
    out = []
    for item in clause.split(","):
        words = item.split()
        if words:
            out.append(words[0])
    return out


def _package(path: str) -> str:
    return path.rsplit("/", 1)[0].replace("/", ".") if "/" in path else ""


def _relative(module: str, path: str) -> str:
    if not module.startswith("."):
        return module
    dots = len(module) - len(module.lstrip("."))
    base = _package(path).split(".")
    base = base[: len(base) - (dots - 1)] if dots > 1 else base
    rest = module[dots:]
    return ".".join([*base, rest] if rest else base)


@dataclass
class FileRefs:
    includes: set[str] = field(default_factory=set)  # repo paths this file includes
    py_imports: set[str] = field(default_factory=set)  # dotted names imported at Python level
    strings: set[str] = field(default_factory=set)  # dotted names in string literals
    unresolved: set[str] = field(default_factory=set)


def scan(
    path: str,
    text: str,
    exists: set[str],
    include_dirs: Iterable[str],
    generated: dict[str, list[str]],
) -> FileRefs:
    refs = FileRefs()
    directory = path.rsplit("/", 1)[0] if "/" in path else ""

    def pxd(module: str) -> None:
        stem = module.replace(".", "/")
        for cand in (f"{stem}.pxd", f"{stem}/__init__.pxd"):
            if cand in exists:
                refs.includes.add(cand)
                return

    def header(name: str, quoted: bool) -> None:
        for base in [directory, *include_dirs] if quoted else list(include_dirs):
            cand = _join(base, name)
            if cand in exists:
                refs.includes.add(cand)
                return
            if cand in generated:
                refs.includes.update(generated[cand])
                return
            if f"{cand}.in" in exists:  # a .pxi generated from .pxi.in beside it
                refs.includes.add(f"{cand}.in")
                return
        if quoted:
            refs.unresolved.add(name)

    refs.strings = set(_STRING_MODULE.findall(text))
    if path.endswith((".pyx", ".pxd", ".pxi", ".pxi.in")):
        if path.endswith(".pyx") and path[:-4] + ".pxd" in exists:
            refs.includes.add(path[:-4] + ".pxd")  # Cython includes its own .pxd
        for line in _logical_lines(text):
            m = _CIMPORT_FROM.match(line)
            if m:
                module = _relative(m.group(1), path)
                pxd(module)
                for name in _names(m.group(2)):
                    pxd(f"{module}.{name}")
                continue
            m = _CIMPORT.match(line)
            if m:
                for name in _names(m.group(1)):
                    pxd(_relative(name, path))
                continue
            m = _INCLUDE.match(line)
            if m:
                header(m.group(1), True)
                continue
            m = _EXTERN.search(line)
            if m:
                header(m.group(1), True)
                continue
            m = _PYIMPORT_FROM.match(line)
            if m:
                module = _relative(m.group(1), path)
                refs.py_imports.add(module)
                for name in _names(m.group(2)):
                    refs.py_imports.add(f"{module}.{name}")
                continue
            m = _PYIMPORT.match(line)
            if m:
                for name in _names(m.group(1)):
                    refs.py_imports.add(_relative(name, path))
    else:
        for line in text.split("\n"):
            m = _C_INCLUDE.match(line)
            if m:
                header(m.group(1) or m.group(2), m.group(1) is not None)
    return refs


# --------------------------------------------------------------------------- build map


def _reexports(init_text: str, package: str) -> dict[str, str]:
    """``from .mod import name`` in a package's ``__init__.py``: name -> module."""
    out: dict[str, str] = {}
    try:
        tree = ast.parse(init_text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            for alias in node.names:
                out[alias.asname or alias.name] = module
    return out


def build_map(repo: Path, rev: str) -> dict[str, Any]:
    tree = files_at(repo, rev)
    exists = set(tree)
    meson_files = [p for p in tree if p.rsplit("/", 1)[-1] == "meson.build"]
    texts = contents_at(
        repo,
        rev,
        [p for p in tree if is_compiled(p) or p.endswith("__init__.py")],
    )
    reader = MesonReader({p: texts[p] for p in meson_files if p in texts})
    reader.run("")
    notes = list(reader.notes)
    modules: dict[str, dict[str, Any]] = {}

    def resolve(value: Any, out: set[str], where: str, seen: set[int]) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, str):
            if value in exists:
                out.add(value)
            elif value in reader.generated:
                out.update(reader.generated[value])
            else:
                notes.append(f"{where}: source {value} not in the checkout")
        elif isinstance(value, Generated):
            for x in value.inputs:
                resolve(x, out, where, seen)
        elif isinstance(value, Library):
            for x in value.sources + value.deps:
                resolve(x, out, where, seen)
        elif isinstance(value, Dependency):
            for x in value.sources + value.link_with + value.deps:
                resolve(x, out, where, seen)
        elif isinstance(value, (list, Choice)):
            for x in _flatten(value):
                resolve(x, out, where, seen)
        elif isinstance(value, Unknown):
            notes.append(f"{where}: a source the reader cannot follow ({value.why})")
        # Opaque: an external dependency (numpy, a subproject, libm)

    for ext in reader.extensions:
        names = ext.name.options if isinstance(ext.name, Choice) else [ext.name]
        subdirs = ext.subdir.options if isinstance(ext.subdir, Choice) else [ext.subdir]
        for name in names:
            for subdir in subdirs:
                if not isinstance(name, str) or not isinstance(subdir, (str, type(None))):
                    notes.append(
                        f"{ext.directory}/meson.build:{ext.line}: extension_module name "
                        f"{name!r} subdir {subdir!r} not literal"
                    )
                    continue
                dotted = ".".join([*(subdir.split("/") if subdir else []), name])
                sources: set[str] = set()
                resolve(ext.sources + ext.deps, sources, dotted, set())
                entry = modules.setdefault(dotted, {"sources": set(), "how": "meson"})
                entry["sources"] |= sources
    # Cython's convention for a .pyx no meson file claims (setup.py projects).
    for p in tree:
        if p.endswith(".pyx"):
            dotted = p[:-4].replace("/", ".")
            if dotted not in modules:
                modules[dotted] = {"sources": {p}, "how": "convention"}
    include_dirs = sorted(reader.include_dirs)
    refs = {
        p: scan(p, texts[p], exists, include_dirs, reader.generated)
        for p in texts
        if p.endswith(COMPILED_SUFFIXES)
    }
    for module in modules.values():
        files, stack = set(), list(module["sources"])
        while stack:
            p = stack.pop()
            if p in files:
                continue
            files.add(p)
            stack.extend(refs[p].includes if p in refs else ())
        module["files"] = files
    # Dependencies between modules.
    own_pxd = {
        name: {p[:-4] + ".pxd" for p in m["sources"] if p.endswith(".pyx")}
        for name, m in modules.items()
    }
    reexports: dict[str, dict[str, str]] = {}
    for p, text in texts.items():
        if p.endswith("/__init__.py"):
            package = p[: -len("/__init__.py")].replace("/", ".")
            reexports[package] = _reexports(text, package)
    deps: dict[str, dict[str, str]] = {}
    for name, m in modules.items():
        found: dict[str, str] = {}
        for other, pxds in own_pxd.items():
            if other != name and pxds & m["files"]:
                found[other] = "cimports " + ", ".join(sorted(pxds & m["files"]))
        for p in sorted(m["files"]):
            r = refs.get(p)
            if r is None:
                continue
            for imported in sorted(r.py_imports):
                target = imported
                if target not in modules and "." in target:
                    package, attr = target.rsplit(".", 1)
                    via = reexports.get(package, {}).get(attr)
                    if via is not None:
                        target = via
                if target in modules and target != name:
                    found.setdefault(target, f"{p} imports {imported}")
            for s in sorted(r.strings):
                if s in modules and s != name:
                    found.setdefault(s, f"{p} names {s} in a string")
                elif s.rsplit(".", 1)[0] in modules and s.rsplit(".", 1)[0] != name:
                    # A capsule: "pandas._libs.pandas_datetime._pandas_datetime_CAPI".
                    found.setdefault(s.rsplit(".", 1)[0], f"{p} names {s} in a string")
        deps[name] = found
    # A capsule: a header that calls PyCapsule_Import is the client side of a
    # C API some module's C sources export with PyCapsule_New (pandas'
    # pd_datetime.h, pd_parser.h). Every module including the header uses
    # the exporting module.
    capsule_headers = {p for p, t in texts.items() if "PyCapsule_Import" in t}
    for header in sorted(capsule_headers):
        providers = [
            n
            for n, m in modules.items()
            if any(
                p.endswith((".c", ".cpp"))
                and "PyCapsule_New" in texts.get(p, "")
                and header in refs[p].includes
                for p in m["sources"]
            )
        ]
        for name, m in modules.items():
            if header in m["files"]:
                for provider in providers:
                    if provider != name:
                        deps[name].setdefault(provider, f"includes {header} (a capsule)")
    claimed: dict[str, list[str]] = defaultdict(list)
    for name, m in modules.items():
        for p in m["files"]:
            claimed[p].append(name)
    return {
        "rev": rev,
        "modules": {
            n: {
                "how": m["how"],
                "sources": sorted(m["sources"]),
                "files": sorted(m["files"]),
                "deps": deps[n],
            }
            for n, m in sorted(modules.items())
        },
        "claimed": {p: sorted(v) for p, v in sorted(claimed.items())},
        "unclaimed": sorted(p for p in tree if is_compiled(p) and p not in claimed),
        "include_dirs": include_dirs,
        "generated": reader.generated,
        "unresolved_includes": {p: sorted(r.unresolved) for p, r in refs.items() if r.unresolved},
        "notes": sorted(set(notes)),
    }


def closure(bm: dict[str, Any], changed_modules: Iterable[str]) -> set[str]:
    """The changed modules and every module that imports or cimports one."""
    users: dict[str, set[str]] = defaultdict(set)
    for name, m in bm["modules"].items():
        for dep in m["deps"]:
            users[dep].add(name)
    out, stack = set(), list(changed_modules)
    while stack:
        n = stack.pop()
        if n in out:
            continue
        out.add(n)
        stack.extend(users.get(n, ()))
    return out


# --------------------------------------------------------------------------- surface

SURFACE_CODE = r"""
import importlib, json, sys, types
claims = json.loads(sys.argv[1])  # module -> the source files the build map gives it


def code_files(obj):
    # The files the object's Cython (or Python) functions were compiled from:
    # a type whose __module__ was set elsewhere (pandas sets "pandas" on
    # Timestamp, Period, ...) still has its methods' code here.
    found = set()
    for c in [obj, *(vars(obj).values() if isinstance(obj, type) else ())]:
        for f in (getattr(c, "__func__", c), getattr(c, "fget", None)):
            code = getattr(f, "__code__", None)
            if code is not None:
                found.add(code.co_filename)
    return found


out = {}
for name in sys.argv[2:]:
    entry = out[name] = {"names": [], "types": {}, "dunders": {}, "reexports": [], "error": None}
    try:
        mod = importlib.import_module(name)
    except BaseException as e:
        entry["error"] = f"{type(e).__name__}: {e}"
        continue
    entry["file"] = getattr(mod, "__file__", None)
    for attr, obj in sorted(vars(mod).items()):
        if attr.startswith("__") and attr.endswith("__"):
            continue
        if isinstance(obj, types.ModuleType):
            entry["reexports"].append(attr)
            continue
        owner = getattr(obj, "__module__", None)
        named = isinstance(obj, type) or callable(obj)
        own = owner == name or bool(code_files(obj) & set(claims.get(name, ())))
        if own and owner != name:
            entry.setdefault("relabelled", []).append(attr)
        if not own and owner is not None and owner != name:
            home = sys.modules.get(owner)
            if named or (home is not None and getattr(home, attr, None) is obj):
                entry["reexports"].append(attr)
                continue
        entry["names"].append(attr)
        if isinstance(obj, type) and own:
            keys = [k for k in vars(obj) if isinstance(k, str)]
            dunder = [k for k in keys if k.startswith("__") and k.endswith("__")]
            entry["types"][attr] = sorted(set(keys) - set(dunder))
            entry["dunders"][attr] = sorted(dunder)
json.dump(out, sys.stdout)
"""


def surface(python: str, cwd: Path, bm: dict[str, Any]) -> dict[str, Any]:
    claims: dict[str, list[str]] = defaultdict(list)
    for path, modules in bm["claimed"].items():
        for m in modules:
            claims[m].append(path)
            if path.endswith(".pxi.in"):
                claims[m].append(path.removesuffix(".in"))
    proc = subprocess.run(
        [python, "-c", SURFACE_CODE, json.dumps(claims), *sorted(bm["modules"])],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        raise SystemExit(proc.stderr)
    return json.loads(proc.stdout)


def surface_names(
    surf: dict[str, Any], modules: Iterable[str], *, types: bool = True, dunders: bool = False
) -> set[str]:
    out: set[str] = set()
    for m in modules:
        entry = surf.get(m)
        if entry is None:
            continue
        out.add(m.rsplit(".", 1)[-1])
        out.update(entry["names"])
        if types:
            for attrs in entry["types"].values():
                out.update(attrs)
        if dunders:
            for attrs in entry["dunders"].values():
                out.update(attrs)
    return out


# --------------------------------------------------------------------------- selection


class Selector:
    """The coarse selection at X, through the evidence planner's own reader
    rule (``_Observers._reader``): for each surface name, the Python symbols
    reading it by that name (``by_name``: unresolved references) are
    readers; a function that ran in a test selects it, one that ran at
    import or in a hook escalates its module or selects all, a variable is
    followed to its readers, module or class top-level code escalates its
    module. ``selected`` is the tests whose record meets E plus the tests the
    planner always selects (no record, unstable, subprocess, text code);
    escalation is reported (``escalated_modules``, ``fallbacks``) and, with
    ``escalate=True``, planned statically from the seeds as the planner
    would."""

    def __init__(self, repo: Path, x: str, store: Path, cache: Path) -> None:
        from diffcone.cache import IndexCache
        from diffcone.evidence import load_store
        from diffcone.planner import plan

        self.repo = repo
        self.evidence = load_store(store)
        # The plan X -> X (no change) gives the targets and what the planner
        # always selects; it takes half an hour on a loaded machine, so it is
        # kept beside the store.
        saved = store.with_name(f"baseline-{x[:10]}.pkl")
        if saved.exists():
            with saved.open("rb") as f:
                kept = pickle.load(f)
            index = IndexCache(cache).load(x, ["."])
            assert index is not None, "the index cache lost X"
            self.index = index
        else:
            t = time.time()
            baseline = plan(
                repo,
                x,
                x,
                discover_runners=["pytest"],
                cache=IndexCache(cache),
                evidence=self.evidence,
            )
            why: dict[str, int] = defaultdict(int)
            for d in baseline.decisions:
                if d.selected:
                    why[str(d.reasons[0].rule)] += 1
            kept: dict[str, Any] = {
                "seconds": round(time.time() - t, 1),
                "targets": [d.target for d in baseline.decisions if d.target.runner == "pytest"],
                "always": {d.target.runner_id for d in baseline.decisions if d.selected},
                "always_why": dict(why),
                "discovery": baseline.discovery,
            }
            with saved.open("wb") as f:
                pickle.dump(kept, f)
            self.index = baseline.head_index
        self.plan_seconds: float = kept["seconds"]
        self.pytest_targets: list[Any] = kept["targets"]
        self.targets = sorted(t.runner_id for t in self.pytest_targets)
        self.always: set[str] = kept["always"]
        self.always_why: dict[str, int] = kept["always_why"]
        self.discovery: Any = kept["discovery"]
        ev = self.evidence
        self.index_of = {s: i for i, s in enumerate(ev.symbols)}
        self.ran_by: dict[int, set[str]] = defaultdict(set)  # symbol index -> tests
        target_set = set(self.targets)
        for test, record in ev.tests.items():
            if test in target_set:
                for i in record.symbols:
                    self.ran_by[i].add(test)
        t = time.time()
        template = self._observers()
        self.observers_seconds = round(time.time() - t, 1)
        self.by_name = template.by_name
        self.sites = sorted(
            site for site, kind in template.sites if kind != "import" and site in template.symbols
        )
        self.site_tests = self.tests_meeting(self.sites)
        self._escalated: dict[tuple, set[str]] = {}
        self._memo: dict[tuple, dict[str, Any]] = {}

    def _observers(self) -> Any:
        from diffcone.evidence_plan import _Observers, _runner_only, _TestCode
        from diffcone.planner import isolated_test_code

        idx = self.index
        test_code = _TestCode(self.pytest_targets, (idx, idx, idx))
        test_code.entry_modules(idx.symbols)
        test_code.isolated = isolated_test_code(self.pytest_targets, (idx, idx, idx))
        runner_only = _runner_only(idx, idx, self.pytest_targets)
        obs = _Observers(idx, idx, self.evidence, test_code, [], runner_only)
        obs.run()
        return obs

    def readers(self, names: Iterable[str]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for n in names:
            for s in self.by_name.get(n, ()):
                out[s].add(n)
        return out

    def readers_for(
        self, variant: str, surf: dict[str, Any], modules: Iterable[str]
    ) -> dict[str, set[str]]:
        """The Python readers of ``modules``' surface (symbol -> names read).

        * ``all`` (item 18 as written): every reference by name to a module,
          a module-level name or an attribute name of a type it defines.
        * ``module``: the same without the types' attribute names (an object
          of the module's type came from code naming the module's surface or
          from the closure: item 18's special-method argument).
        * ``receiver``: module-level names only where the reference can be
          to the module: a name looked up on a module (``module_attribute``:
          an import from a package re-exporting it, ``pd.Timestamp``) or an
          attribute read off an alias the module is bound to
          (``lib.maybe_convert_objects``, ``window_aggregations.roll_sum``
          after ``import pandas._libs.window.aggregations as
          window_aggregations``: the reader's module's own imports, read
          with ``ast``; where the module cannot be read, any alias named
          like the module); import statements themselves are not reads.
        """
        modules = list(modules)
        if variant == "all":
            return self.readers(surface_names(surf, modules))
        if variant == "module":
            return self.readers(surface_names(surf, modules, types=False))
        self._receiver_tables()
        out: dict[str, set[str]] = defaultdict(set)
        for m in modules:
            entry = surf.get(m)
            if entry is None:
                continue
            short = m.rsplit(".", 1)[-1]
            for n in {short, *entry["names"]}:
                for s, d in self._module_attrs.get(n, ()):
                    if not d.startswith(("from ", "import ")):
                        out[s].add(n)
            for n in entry["names"]:
                for s, d in self._attrs.get(n, ()):
                    if d.startswith(m + "."):
                        out[s].add(n)
                        continue
                    head = d.split(".", 1)[0]
                    symbol = self.index.symbols.get(s)
                    bound = self._aliases(symbol.module) if symbol is not None else None
                    if (bound is None and head == short) or (bound and bound.get(head) == m):
                        out[s].add(n)
        return out

    def _aliases(self, module: str) -> dict[str, str] | None:
        """The names ``module``'s imports bind to a module (anywhere in the
        file: function-local imports too), or None when it cannot be read."""
        if not hasattr(self, "_alias_cache"):
            self._alias_cache: dict[str, dict[str, str] | None] = {}
        if module in self._alias_cache:
            return self._alias_cache[module]
        found: dict[str, str] | None = None
        symbol = self.index.symbols.get(module)
        if symbol is not None:
            try:
                tree = ast.parse((self.repo / symbol.path).read_text("utf-8"))
            except (OSError, SyntaxError, ValueError):
                tree = None
            if tree is not None:
                found = {}
                package = (
                    module if symbol.path.endswith("__init__.py") else module.rpartition(".")[0]
                )
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for a in node.names:
                            if a.asname:
                                found[a.asname] = a.name
                    elif isinstance(node, ast.ImportFrom):
                        base = node.module or ""
                        if node.level:
                            parts = package.split(".")
                            parts = parts[: len(parts) - (node.level - 1)]
                            base = ".".join([*parts, base] if base else parts)
                        for a in node.names:
                            found[a.asname or a.name] = f"{base}.{a.name}"
        self._alias_cache[module] = found
        return found

    def _receiver_tables(self) -> None:
        if hasattr(self, "_attrs"):
            return
        from diffcone.model import UnresolvedKind

        self._attrs: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._module_attrs: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in self.index.unresolved:
            if r.kind == UnresolvedKind.ATTRIBUTE:
                self._attrs[r.name].append((r.symbol, r.detail))
            elif r.kind in (UnresolvedKind.MODULE_ATTRIBUTE, UnresolvedKind.NAME):
                self._module_attrs[r.name].append((r.symbol, r.detail))

    def tests_meeting(self, symbols: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for s in symbols:
            i = self.index_of.get(s)
            if i is not None:
                out |= self.ran_by.get(i, set())
        return out

    def select(
        self, readers: dict[str, set[str]], *, escalate: bool = False, planned: bool = True
    ) -> dict[str, Any]:
        key = (frozenset((r, frozenset(n)) for r, n in readers.items()), escalate, planned)
        if key not in self._memo:
            self._memo[key] = self._select(readers, escalate=escalate, planned=planned)
        return self._memo[key]

    def _select(
        self, readers: dict[str, set[str]], *, escalate: bool, planned: bool
    ) -> dict[str, Any]:
        """``planned=False`` skips the planner's propagation (a minute per
        call on pandas): only the direct count and the reader flags."""
        from diffcone.model import SymbolKind

        ev = self.evidence
        names = {n for read in readers.values() for n in read}
        if planned:
            obs = self._observers()
            for reader, read in sorted(readers.items()):
                for name in sorted(read):
                    obs._reader(reader, None, f"extension surface name {name}", read=name)
            obs._settle()
            E, seed_nodes, seed_changes, fallbacks = (
                obs.E,
                obs.seed_nodes,
                obs.seed_changes,
                obs.fallbacks,
            )
        else:
            E, seed_nodes, seed_changes, fallbacks = {}, {}, set(), []
        tests = self.tests_meeting(E)
        kinds: dict[str, int] = defaultdict(int)
        for r in readers:
            symbol = self.index.symbols.get(r)
            kinds[str(symbol.kind) if symbol else "?"] += 1
        import_readers = sorted(r for r in readers if ev.import_by.get(r))
        hook_readers = sorted(r for r in readers if r in ev.hook_phase)
        top_level = sorted(
            r
            for r in readers
            if (sym := self.index.symbols.get(r)) is not None
            and sym.kind in (SymbolKind.MODULE, SymbolKind.CLASS)
        )
        selected = tests | self.always if planned else set()
        # The brief's count: tests whose record executed a reader, without
        # the planner's propagation (_settle: state a reader stores for a
        # later test, lookup sites) and without escalation.
        direct = self.tests_meeting(readers) | self.always
        out: dict[str, Any] = {
            "names": len(names),
            "readers": len(readers),
            "reader_kinds": dict(kinds),
            "readers_ran_in_tests": sum(1 for r in readers if self.tests_meeting([r])),
            "planned": planned,
            "E": len(E),
            "import_readers": len(import_readers),
            "top_level_readers": len(top_level),
            "hook_readers": len(hook_readers),
            "escalated_modules": len(seed_nodes),
            "fallbacks": sorted({str(f.rule) for f in fallbacks}),
            "fallback_details": sorted({f.detail for f in fallbacks})[:5],
            "escalated_examples": sorted(seed_nodes)[:5],
            "selected": len(selected) if planned else None,
            "direct": len(direct),
            "direct_tests": direct,
            "by_readers": len(tests - self.always),
            "with_sites": len(selected | self.site_tests),
            "examples_import": import_readers[:5],
            "examples_top_level": top_level[:5],
            "examples_hook": hook_readers[:5],
            "tests": selected,
        }
        if planned and escalate and (seed_nodes or seed_changes):
            key = (frozenset(seed_changes), frozenset(seed_nodes))
            if key not in self._escalated:
                from diffcone.planner import Seeds, plan_from_indexes

                static = plan_from_indexes(
                    self.index,
                    self.index,
                    None,
                    repo=str(self.repo),
                    source_roots=["."],
                    discovered=self.discovery,
                    seeds=Seeds(frozenset(seed_changes), dict(seed_nodes)),
                )
                self._escalated[key] = {
                    d.target.runner_id
                    for d in static.decisions
                    if d.selected and d.target.runner == "pytest"
                }
            out["escalated_selected"] = len(selected | self._escalated[key])
        return out


def changed_files(repo: Path, commit: str) -> list[tuple[str, str]]:
    """(status, path) of a first-parent commit against its parent."""
    out = []
    for line in git(
        repo, "diff-tree", "--no-commit-id", "-r", "--name-status", f"{commit}^", commit
    ).split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0][0]
        for p in parts[1:]:
            out.append((status, p))
    return out


VARIANTS = ("all", "module", "receiver")
# The variants whose planned count is computed (EXTSPIKE_PLANNED=all,module,receiver).
PLANNED = set(os.environ.get("EXTSPIKE_PLANNED", "receiver").split(","))


def share(n: int | None, total: int) -> float | None:
    return None if n is None else round(n / total, 4)


def pct(x: float | None) -> str:
    return "  n/a" if x is None else f"{x:4.0%}"


def outcome(row: dict[str, Any]) -> str:
    if not row["planned"]:
        return "not planned"
    if row["fallbacks"]:
        return "select all (a reader ran in a hook)"
    if row["escalated_modules"]:
        return "escalated (a reader ran at import)"
    return "narrow"


def coarse(
    bm: dict[str, Any],
    surf: dict[str, Any],
    sel: Selector,
    files: list[str],
    exists: set[str],
    *,
    escalate: bool = False,
) -> dict[str, Any]:
    gone = [p for p in files if p not in exists]
    present = [p for p in files if p in exists]
    unclaimed = [p for p in present if p not in bm["claimed"]]
    modules = sorted({m for p in present for m in bm["claimed"].get(p, ())})
    clos = sorted(closure(bm, modules))
    row: dict[str, Any] = {
        "files": files,
        "gone": gone,
        "unclaimed": unclaimed,
        "modules": modules,
        "closure": clos,
        "variants": {},
    }
    if not modules:
        return row
    total = len(sel.targets)
    for v in VARIANTS:
        s = dict(
            sel.select(sel.readers_for(v, surf, clos), escalate=escalate, planned=v in PLANNED)
        )
        tests = s.pop("tests")
        direct = s.pop("direct_tests")
        s["outcome"] = outcome(s)
        s["flag"] = flag(s)
        s["share"] = share(s["selected"], total)
        s["direct_share"] = round(s["direct"] / total, 4)
        if "escalated_selected" in s:
            s["share_escalated"] = round(s["escalated_selected"] / total, 4)
        row["variants"][v] = s
        row[f"_tests_{v}"] = tests
        row[f"_direct_{v}"] = direct
    return row


# --------------------------------------------------------------------------- commands


def cmd_buildmap(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    rev = git(repo, "rev-parse", args.rev).strip()
    bm = build_map(repo, rev)
    _dump(args.data / f"buildmap-{rev[:10]}.json", bm)
    print(f"{len(bm['modules'])} modules, {len(bm['claimed'])} claimed files")
    print(f"unclaimed: {len(bm['unclaimed'])}: {bm['unclaimed']}")
    for note in bm["notes"]:
        print("note:", note)
    if bm["unresolved_includes"]:
        print("unresolved quoted includes:", bm["unresolved_includes"])


def cmd_surface(args: argparse.Namespace) -> None:
    bm = json.loads((args.data / f"buildmap-{args.x[:10]}.json").read_text())
    surf = surface(args.python, args.repo.resolve(), bm)
    _dump(args.data / f"surface-{args.x[:10]}.json", surf)
    for name, e in surf.items():
        types_ = len(e["types"])
        attrs = sum(len(v) for v in e["types"].values())
        print(
            f"{name}: {len(e['names'])} names, {types_} types, {attrs} type attrs; "
            f"own despite __module__: {e.get('relabelled', [])}"
        )


def _load(args: argparse.Namespace) -> tuple[Path, str, dict[str, Any], dict[str, Any], Selector]:
    repo = args.repo.resolve()
    x = git(repo, "rev-parse", args.x).strip()
    bm = json.loads((args.data / f"buildmap-{x[:10]}.json").read_text())
    surf = json.loads((args.data / f"surface-{x[:10]}.json").read_text())
    sel = Selector(repo, x, args.store, args.cache)
    print(
        f"{len(sel.targets)} targets, {len(sel.always)} always selected {dict(sel.always_why)}, "
        f"baseline plan {sel.plan_seconds}s, observers {sel.observers_seconds}s, "
        f"{len(sel.sites)} lookup sites run by {len(sel.site_tests)} tests",
        flush=True,
    )
    return repo, x, bm, surf, sel


def flag(s: dict[str, Any]) -> str:
    """The direct count's flag: a reader ran outside every test."""
    if s["hook_readers"]:
        return "hook"
    if s["import_readers"] or s["top_level_readers"]:
        return "import"
    return ""


def _brief(s: dict[str, Any]) -> str:
    return (
        f"d={s['direct']:>5} ({s['direct_share']:.0%}){s['flag'][:1] or '-'} "
        f"p={s['selected'] or 0:>5} ({pct(s['share'])}) {s['outcome'][:6]} r={s['readers']}"
        f"/{s['top_level_readers']}t/{s['import_readers']}i/{s['hook_readers']}h"
    )


def cmd_precision(args: argparse.Namespace) -> None:
    repo, x, bm, surf, sel = _load(args)
    history = args.history.resolve() if args.history else repo
    exists = set(files_at(repo, x))
    commits = git(history, "log", "--first-parent", "--format=%H", "-n", str(args.last), x).split()
    total = len(sel.targets)
    # Per module alone first: every commit's set is a union of these.
    per_module: dict[str, dict[str, Any]] = {}
    for m in sorted(bm["modules"]):
        clos = sorted(closure(bm, [m]))
        per_module[m] = {"closure": len(clos)}
        for v in VARIANTS:
            s = dict(
                sel.select(
                    sel.readers_for(v, surf, clos), escalate=args.escalate, planned=v in PLANNED
                )
            )
            s.pop("tests")
            s.pop("direct_tests")
            s["outcome"] = outcome(s)
            s["flag"] = flag(s)
            s["share"] = share(s["selected"], total)
            s["direct_share"] = round(s["direct"] / total, 4)
            per_module[m][v] = s
        print(
            f"module {m} closure {len(clos)}: "
            + " | ".join(f"{v} {_brief(per_module[m][v])}" for v in VARIANTS),
            flush=True,
        )
    _dump(args.data / "per_module.json", per_module)
    rows = []
    with (args.data / "precision.jsonl").open("w") as out:
        for c in commits:
            changed = changed_files(history, c)
            compiled = sorted({p for _, p in changed if is_compiled(p)})
            if not compiled:
                continue
            row = coarse(bm, surf, sel, compiled, exists, escalate=args.escalate)
            row["commit"] = c[:10]
            row["subject"] = git(history, "log", "-1", "--format=%s", c).strip()[:80]
            row["python_too"] = any(p.endswith(".py") for _, p in changed)
            if row["unclaimed"]:
                row["class"] = "unclaimed (select all)"
            elif not row["modules"]:
                row["class"] = "unmeasurable (gone at X)"
            else:
                row["class"] = "measured"
            for v in VARIANTS:
                row.pop(f"_tests_{v}", None)
                row.pop(f"_direct_{v}", None)
            rows.append(row)
            out.write(json.dumps(row) + "\n")
            print(
                f"{row['commit']} {row['class'][:10]} mods={len(row['modules'])} "
                f"closure={len(row['closure'])} "
                + " | ".join(f"{v} {_brief(s)}" for v, s in row["variants"].items())
                + f" :: {row['subject'][:40]}",
                flush=True,
            )
    summarize(rows, total)


def summarize(rows: list[dict[str, Any]], total: int) -> None:
    classes: dict[str, int] = defaultdict(int)
    for r in rows:
        classes[r["class"]] += 1
    print(f"\n{len(rows)} compiled-source commits; {total} targets; {dict(classes)}")
    measured = [r for r in rows if r["class"] == "measured"]
    for v in VARIANTS:
        kinds: dict[str, int] = defaultdict(int)
        for r in measured:
            kinds[r["variants"][v]["outcome"]] += 1
        flags: dict[str, int] = defaultdict(int)
        for r in measured:
            flags[r["variants"][v]["flag"] or "none"] += 1
        if not measured:
            print(f"  {v}: none")
            continue
        direct = [r["variants"][v]["direct_share"] for r in measured]
        unflagged = [
            r["variants"][v]["direct_share"] for r in measured if not r["variants"][v]["flag"]
        ]
        planned = [
            r["variants"][v]["share"] for r in measured if r["variants"][v]["share"] is not None
        ]
        line = (
            f"  {v}: direct median {statistics.median(direct):.1%} "
            f"(p25 {_q(direct, 0.25):.1%}, p75 {_q(direct, 0.75):.1%}); flags {dict(flags)}"
        )
        if unflagged:
            line += f"; unflagged n={len(unflagged)} median {statistics.median(unflagged):.1%}"
        if planned:
            line += f"; planned median {statistics.median(planned):.1%}, outcomes {dict(kinds)}"
        print(line)


def _q(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def fold(nodeid: str) -> str:
    return re.sub(r"\[.*\]$", "", nodeid, flags=re.S)


def cmd_oracle(args: argparse.Namespace) -> None:
    """Per folded test: the compiled functions it ran (``path::function``)."""
    tests: dict[str, set[str]] = defaultdict(set)
    cases = 0
    for f in sorted(args.raw.glob("oracle-*.jsonl")):
        names: dict[int, str] = {}
        for line in f.open():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a worker cut short
            if "f" in rec:
                names[rec["f"][0]] = rec["f"][1]
            else:
                cases += 1
                tests[fold(rec["t"])].update(names[i] for i in rec["x"])
    _dump(args.data / "oracle.json", {t: sorted(v) for t, v in sorted(tests.items())})
    print(
        f"{cases} cases, {len(tests)} tests, "
        f"{len({f for v in tests.values() for f in v})} functions"
    )


def module_of_path(bm: dict[str, Any], path: str) -> list[str]:
    """The modules a traced file belongs to; a generated .pxi (in the build
    directory) by its .pxi.in."""
    found = bm["claimed"].get(path)
    if found is None and path.endswith(".pxi"):
        found = bm["claimed"].get(path + ".in")
    return found or []


def cmd_recall(args: argparse.Namespace) -> None:
    from diffcone.cython import cython_changes, is_cython, read

    repo, x, bm, surf, sel = _load(args)
    history = args.history.resolve() if args.history else repo
    oracle: dict[str, list[str]] = json.loads((args.data / "oracle.json").read_text())
    texts = contents_at(repo, x, [p for p in files_at(repo, x) if is_cython(p)])
    at_x = {p: read(p, t) for p, t in texts.items()}
    ran: dict[str, set[str]] = defaultdict(set)  # function -> tests that ran it (oracle)
    for test, functions in oracle.items():
        for f in functions:
            ran[f].add(test)
    targets = set(sel.targets)
    commits = git(history, "log", "--first-parent", "--format=%H", "-n", str(args.last), x).split()
    rows = []

    def at(rev: str, path: str):
        try:
            return read(path, git(history, "show", f"{rev}:{path}"))
        except subprocess.CalledProcessError:
            return None

    with (args.data / "recall.jsonl").open("w") as out:
        for c in commits[1:]:  # X itself is not an edit
            paths = [p for _, p in changed_files(history, c) if is_cython(p)]
            if not paths:
                continue
            before = {p: m for p in paths if (m := at(f"{c}^", p))}
            after = {p: m for p in paths if (m := at(c, p))}
            changes = cython_changes(before, after)
            if changes.files or not changes.functions:
                continue
            edited = [(p, n) for p, n in changes.functions if p in at_x and n in at_x[p].by_name()]
            if not edited:
                continue
            files = sorted({p for p, _ in edited})
            modules = sorted({m for p in files for m in module_of_path(bm, p)})
            clos = sorted(closure(bm, modules))
            expected: set[str] = set()
            for p, n in edited:
                expected |= ran.get(f"{p}::{n}", set())
            expected &= targets
            row: dict[str, Any] = {
                "commit": c[:10],
                "functions": [f"{p}::{n}" for p, n in edited],
                "modules": modules,
                "closure": len(clos),
                "oracle": len(expected),
            }
            for v in VARIANTS:
                s = sel.select(sel.readers_for(v, surf, clos), planned=v in PLANNED)
                missed = sorted(expected - s["tests"]) if s["planned"] else []
                missed_direct = sorted(expected - s["direct_tests"])
                row[v] = {
                    "selected": s["selected"],
                    "share": share(s["selected"], len(sel.targets)),
                    "direct": s["direct"],
                    "direct_share": round(s["direct"] / len(sel.targets), 4),
                    "outcome": outcome(s),
                    "flag": flag(s),
                    "n_missed": len(missed) if s["planned"] else None,
                    "missed": missed[:30],
                    "n_missed_direct": len(missed_direct),
                    "missed_direct": missed_direct[:30],
                }
            rows.append(row)
            out.write(json.dumps(row) + "\n")
            print(
                f"{c[:10]} {len(edited)} fn in {[m.rsplit('.', 1)[-1] for m in modules]}: "
                f"oracle {len(expected)}; "
                + " | ".join(
                    f"{v} d={row[v]['direct']} ({row[v]['direct_share']:.0%})"
                    f"{row[v]['flag'][:1] or '-'} missed {row[v]['n_missed_direct']}"
                    + (
                        f" p={row[v]['selected']} missed {row[v]['n_missed']}"
                        if v in PLANNED
                        else ""
                    )
                    for v in VARIANTS
                ),
                flush=True,
            )
    print(f"\n{len(rows)} replayed commits, {sum(r['oracle'] for r in rows)} expectations")
    for v in VARIANTS:
        if rows:
            print(
                f"  {v}: direct median {statistics.median(r[v]['direct_share'] for r in rows):.1%},"
                f" misses {sum(r[v]['n_missed_direct'] for r in rows)} on "
                f"{sum(1 for r in rows if r[v]['n_missed_direct'])} commits"
                + (
                    f"; planned median {statistics.median(r[v]['share'] for r in rows):.1%}, "
                    f"misses {sum(r[v]['n_missed'] for r in rows)} on "
                    f"{sum(1 for r in rows if r[v]['n_missed'])} commits"
                    if v in PLANNED
                    else ""
                )
            )


def special(
    bm: dict[str, Any],
    surf: dict[str, Any],
    sel: Selector,
    oracle: dict[str, list[str]],
    args: argparse.Namespace,
    variant: str,
) -> None:
    """Item 18's special-method argument, per (test, compiled file F whose
    functions the oracle saw the test run): the test's record should hold a
    reader of the surface of the closure of F's modules (what an edit of F
    selects). A function of a ``.pxd`` or ``.pxi`` is compiled into every
    module including it and the trace cannot tell which copy ran, so pairs
    are by file. A pair without such a reader is a path by which F's code
    reached the test that no surface reader in the test shows; it is still
    covered when F's closure has a reader that ran in a hook (item 18
    selects all) or at import (item 18 escalates that module)."""
    ev = sel.evidence
    per_file: dict[str, dict[str, Any]] = {}
    pairs = 0
    misses: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    no_record = 0
    for test, functions in oracle.items():
        record = ev.tests.get(test)
        by_file: dict[str, list[str]] = defaultdict(list)
        for f in functions:
            by_file[f.split("::", 1)[0]].append(f)
        if record is None:
            no_record += 1
            continue
        for path, fs in by_file.items():
            modules = module_of_path(bm, path)
            if not modules:
                continue  # numpy's .pxd: not the project's
            pairs += 1
            if path not in per_file:
                found = sel.readers_for(variant, surf, closure(bm, modules))
                s = sel.select(found, planned=False)
                per_file[path] = {
                    "readers": {sel.index_of[r] for r in found if r in sel.index_of},
                    "flag": flag(s),
                }
            if not (record.symbols & per_file[path]["readers"]):
                misses[path].append((test, sorted(fs)[:5]))
    total = sum(len(v) for v in misses.values())
    by_flag: dict[str, int] = defaultdict(int)
    for path, v in misses.items():
        by_flag[per_file[path]["flag"] or "none"] += len(v)
    print(
        f"\nspecial-method check ({variant}): {pairs} (test, file) pairs with a record; "
        f"{total} without a surface reader in the record "
        f"({len({t for v in misses.values() for t, _ in v})} tests); by the file's flag "
        f"{dict(by_flag)}; {no_record} oracle tests without a record"
    )
    for path, v in sorted(misses.items(), key=lambda kv: -len(kv[1])):
        print(f"  {path} [{per_file[path]['flag'] or 'unflagged'}]: {len(v)}; e.g. {v[:2]}")
    _dump(
        args.data / f"special-{variant}.json",
        {p: {"flag": per_file[p]["flag"], "pairs": v} for p, v in misses.items()},
    )


def cmd_special(args: argparse.Namespace) -> None:
    repo, x, bm, surf, sel = _load(args)
    oracle: dict[str, list[str]] = json.loads((args.data / "oracle.json").read_text())
    for v in VARIANTS:
        special(bm, surf, sel, oracle, args, v)


def test_module(nodeid: str) -> str:
    return nodeid.split("::", 1)[0].removesuffix(".py").replace("/", ".")


def cmd_misses(args: argparse.Namespace) -> None:
    """Why the direct count missed what it missed in recall.jsonl: per
    missed (commit, test), whether the test's record holds a reader of a
    type attribute (an instance's method, read by name: the ``all``
    variant), whether a reader in the test's own module ran at import (its
    parametrisation or module state: item 18 escalates the module), or
    whether the closure has a hook reader (item 18 selects all)."""
    from diffcone.model import SymbolKind

    repo, x, bm, surf, sel = _load(args)
    oracle: dict[str, list[str]] = json.loads((args.data / "oracle.json").read_text())
    ran: dict[str, set[str]] = defaultdict(set)
    for test, functions in oracle.items():
        for f in functions:
            ran[f].add(test)
    ev = sel.evidence
    targets = set(sel.targets)
    counts: dict[str, dict[str, int]] = {v: defaultdict(int) for v in VARIANTS}
    examples: dict[str, list[Any]] = defaultdict(list)
    for line in (args.data / "recall.jsonl").open():
        row = json.loads(line)
        files = sorted({f.split("::", 1)[0] for f in row["functions"]})
        clos = sorted(closure(bm, [m for p in files for m in module_of_path(bm, p)]))
        expected: set[str] = set()
        for f in row["functions"]:
            expected |= ran.get(f, set())
        expected &= targets
        every = sel.readers_for("all", surf, clos)
        every_ids = {sel.index_of[r] for r in every if r in sel.index_of}
        for v in VARIANTS:
            found = sel.readers_for(v, surf, clos)
            s = sel.select(found, planned=False)
            at_import = {
                sym.module
                for r in found
                if (sym := sel.index.symbols.get(r)) is not None
                and (
                    ev.import_by.get(r)
                    or r in ev.hook_phase
                    or sym.kind in (SymbolKind.MODULE, SymbolKind.CLASS)
                )
            }
            for t in sorted(expected - s["direct_tests"]):
                record = ev.tests.get(t)
                if record is not None and record.symbols & every_ids:
                    why = "type-attribute reader in the record"
                elif test_module(t) in at_import:
                    why = "reader in the test's module ran at import"
                elif s["hook_readers"]:
                    why = "closure has a hook reader (select all)"
                elif s["import_readers"] or s["top_level_readers"]:
                    why = "closure has an import reader elsewhere (escalated)"
                else:
                    why = "unexplained"
                counts[v][why] += 1
                if v == "receiver" and len(examples[why]) < 6:
                    examples[why].append((row["commit"], t, row["functions"][:2]))
    for v in VARIANTS:
        print(f"{v}: {dict(counts[v])}")
    for why, ex in examples.items():
        print(f"  {why}:")
        for e in ex:
            print(f"    {e}")


def cmd_variants(args: argparse.Namespace) -> None:
    """Narrower stand-ins for item 18's rules 2 and 3, scored on the
    precision commits and against the oracle (recall.jsonl): a reader that
    ran at import (rule 2) or in a hook (rule 3) selects, instead of an
    escalation or everything, the tests that executed any symbol of the
    reader's module ("module touch": what the module's import-time state can
    reach through its own code), plus the tests of that module.

    * ``touch-import``: rule 2 by module touch, rule 3 still everything;
    * ``touch-both``: rules 2 and 3 by module touch."""
    from diffcone.model import SymbolKind

    repo, x, bm, surf, sel = _load(args)
    ev = sel.evidence
    targets = set(sel.targets)
    total = len(sel.targets)
    module_of = {}
    for i, s in enumerate(ev.symbols):
        sym = sel.index.symbols.get(s)
        if sym is not None:
            module_of[i] = sym.module
    touch: dict[str, set[str]] = defaultdict(set)
    for test, record in ev.tests.items():
        if test in targets:
            for i in record.symbols:
                m = module_of.get(i)
                if m is not None:
                    touch[m].add(test)
    for t in targets:
        touch[test_module(t)].add(t)

    def select(clos: list[str]) -> dict[str, Any]:
        readers = sel.readers_for("receiver", surf, clos)
        s = sel.select(readers, planned=False)
        direct = set(s["direct_tests"])
        imp, hook = set(), set()
        for r in readers:
            sym = sel.index.symbols.get(r)
            if sym is None:
                continue
            if r in ev.hook_phase:
                hook.add(sym.module)
            elif ev.import_by.get(r) or sym.kind in (SymbolKind.MODULE, SymbolKind.CLASS):
                imp.add(sym.module)
        out = {}
        a = direct | {t for m in imp for t in touch.get(m, ())}
        out["touch-import"] = set(targets) if hook else a
        out["touch-both"] = a | {t for m in hook for t in touch.get(m, ())}
        out["_modules"] = (sorted(imp), sorted(hook))
        return out

    shares: dict[str, list[float]] = defaultdict(list)
    rows = [json.loads(line) for line in (args.data / "precision.jsonl").open()]
    for r in rows:
        if r["class"] != "measured":
            continue
        got = select(r["closure"])
        line = []
        for v in ("touch-import", "touch-both"):
            shares[v].append(len(got[v]) / total)
            line.append(f"{v} {len(got[v]) / total:.1%}")
        imp, hook = got["_modules"]
        print(
            f"{r['commit']} {','.join(m.rsplit('.', 1)[-1] for m in r['modules'])[:30]:30} "
            + " ".join(line)
            + f" import-modules {len(imp)} hook-modules {len(hook)}"
        )
    for v, xs in shares.items():
        print(
            f"precision {v}: median {statistics.median(xs):.1%}, mean {statistics.mean(xs):.1%}, "
            f"p25 {_q(xs, 0.25):.1%}, p75 {_q(xs, 0.75):.1%}"
        )
    oracle: dict[str, list[str]] = json.loads((args.data / "oracle.json").read_text())
    ran: dict[str, set[str]] = defaultdict(set)
    for test, functions in oracle.items():
        for f in functions:
            ran[f].add(test)
    misses: dict[str, list[Any]] = defaultdict(list)
    rshares: dict[str, list[float]] = defaultdict(list)
    for line in (args.data / "recall.jsonl").open():
        row = json.loads(line)
        files = sorted({f.split("::", 1)[0] for f in row["functions"]})
        clos = sorted(closure(bm, [m for p in files for m in module_of_path(bm, p)]))
        expected: set[str] = set()
        for f in row["functions"]:
            expected |= ran.get(f, set())
        expected &= targets
        got = select(clos)
        for v in ("touch-import", "touch-both"):
            rshares[v].append(len(got[v]) / total)
            for t in sorted(expected - got[v]):
                misses[v].append((row["commit"], t))
    for v in ("touch-import", "touch-both"):
        m = misses[v]
        print(
            f"recall {v}: median share {statistics.median(rshares[v]):.1%}; misses {len(m)} "
            f"on {len({c for c, _ in m})} commits; e.g. {m[:4]}"
        )


def junit_outcomes(path: Path) -> dict[str, str]:
    """Folded test -> sorted outcomes of its cases, from a JUnit file."""
    out: dict[str, set[str]] = defaultdict(set)
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        nodeid = _nodeid(classname.split("."), name)
        status = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                status = child.tag
            elif child.tag == "skipped":
                status = "xfail" if "xfail" in (child.get("type") or "") else "skipped"
        out[fold(nodeid)].add(status)
    return {k: ",".join(sorted(v)) for k, v in out.items()}


def _nodeid(parts: list[str], name: str) -> str:
    # pytest's classname is the dotted module path plus classes: the longest
    # prefix that is a file of the checkout (the cwd) is the module.
    for k in range(len(parts), 0, -1):
        candidate = "/".join(parts[:k]) + ".py"
        if Path(candidate).exists():
            return "::".join([candidate, *parts[k:], name])
    return "::".join([*parts, name])


def cmd_outcomes(args: argparse.Namespace) -> None:
    repo, x, bm, surf, sel = _load(args)
    exists = set(files_at(repo, x))
    history = args.history.resolve() if args.history else repo
    os.chdir(repo)
    for commit in args.commit:
        before = junit_outcomes(args.data / f"outcome-{commit}-before.xml")
        after = junit_outcomes(args.data / f"outcome-{commit}-after.xml")
        changed = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
        files = sorted({p for _, p in changed_files(history, commit) if is_compiled(p)})
        row = coarse(bm, surf, sel, files, exists)
        targets = set(sel.targets)
        print(f"\n{commit}: {files} -> modules {row['modules']}, closure {len(row['closure'])}")
        print(
            f"  {len(changed)} tests changed outcome "
            f"({len([t for t in changed if t in targets])} targets at X)"
        )
        for v in VARIANTS:
            selected = row[f"_direct_{v}"]
            planned = row[f"_tests_{v}"]
            missed_planned = [t for t in changed if t in targets and t not in planned]
            print(f"  {v} planned: selects {len(planned)}; not selected: {missed_planned[:10]}")
            missed = [t for t in changed if t in targets and t not in selected]
            print(
                f"  {v}: selects {len(selected)}; "
                f"outcome changes not selected: {len(missed)} {missed[:10]}"
            )
        for t in changed[:25]:
            where = "target" if t in targets else "not a target at X"
            print(f"    {t}: {before.get(t)} -> {after.get(t)} ({where})")


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True, default=sorted))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("buildmap")
    s.add_argument("--repo", type=Path, required=True)
    s.add_argument("--rev", default="HEAD")
    s = sub.add_parser("surface")
    s.add_argument("--repo", type=Path, required=True)
    s.add_argument("--x", required=True)
    s.add_argument("--python", required=True)
    for name in ("precision", "recall", "outcomes", "special", "misses", "variants"):
        s = sub.add_parser(name)
        s.add_argument("--repo", type=Path, required=True, help="the repository the store is of")
        s.add_argument("--history", type=Path, help="a clone with the commits (default --repo)")
        s.add_argument("--x", required=True)
        s.add_argument("--store", type=Path, required=True)
        s.add_argument("--cache", type=Path, required=True)
        s.add_argument("--last", type=int, default=500)
        s.add_argument("--escalate", action="store_true", help="plan escalations statically")
        if name == "outcomes":
            s.add_argument("--commit", action="append", required=True)
    s = sub.add_parser("oracle")
    s.add_argument("--raw", type=Path, required=True)
    args = p.parse_args(argv)
    if os.environ.get("EXTSPIKE_STACKS"):
        import faulthandler

        faulthandler.dump_traceback_later(int(os.environ["EXTSPIKE_STACKS"]), repeat=True)
    {
        "buildmap": cmd_buildmap,
        "surface": cmd_surface,
        "precision": cmd_precision,
        "oracle": cmd_oracle,
        "recall": cmd_recall,
        "outcomes": cmd_outcomes,
        "special": cmd_special,
        "misses": cmd_misses,
        "variants": cmd_variants,
    }[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

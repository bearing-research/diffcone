"""Shared AST helpers for static discovery."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from diffcone.indexer import (
    DEF_NODES,
    FUNC_NODES,
    TOO_DEEP,
    decode_source,
    iter_scope_statements,
    resolve_relative_module,
)
from diffcone.snapshot import Snapshot, child_modules, member_symbol_id, module_name_for


@dataclass
class ParsedModule:
    path: str
    module: str
    tree: ast.Module
    # Submodule names of this module (a package): see member_symbol_id.
    submodules: frozenset[str] = field(default_factory=frozenset)
    # The source it was parsed from, to skip walking a tree for what its
    # source never mentions.
    source: bytes = b""

    def member_id(self, name: str) -> str:
        """The index identity of this module's top-level ``name``."""
        return member_symbol_id(self.module, name, self.submodules)

    def import_source(self, node: ast.ImportFrom) -> str:
        """The absolute module ``node`` imports from, a relative import
        resolved against this module's package (the module itself when it
        is a package's ``__init__``)."""
        is_package = PurePosixPath(self.path).name == "__init__.py"
        return resolve_relative_module(self.module, is_package, node.module, node.level)


# Set on a module's function nodes when its source never mentions
# getfixturevalue, so discovery can skip walking each test body for it.
NO_GETFIXTUREVALUE = "_diffcone_no_getfixturevalue"


def _mark_functions(body: list[ast.stmt]) -> None:
    """Mark the functions defined at statement level (module, class and
    block bodies), without walking expressions."""
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            setattr(node, NO_GETFIXTUREVALUE, True)
            stack.extend(node.body)
        elif isinstance(node, ast.ClassDef):
            stack.extend(node.body)
        else:
            for name in ("body", "orelse", "finalbody", "handlers"):
                stack.extend(getattr(node, name, ()) or ())


# How deep a syntax tree discovery walks once a walk has run out of stack
# (``RecursionError``): discover() then runs the runner again with
# CHECK_DEPTH set, and parse_modules leaves out every module nested deeper
# than this as one that does not parse. Real modules stay far below it (the
# deepest of some 10,000 files, site-packages included, is 48 levels), and
# the recursive walks spend a few frames a level, well inside Python's
# default limit of 1000. Measuring depth costs a third of parsing, so it is
# only paid on that second run.
MAX_DEPTH = 100
CHECK_DEPTH: ContextVar[bool] = ContextVar("CHECK_DEPTH", default=False)


def deeper_than(tree: ast.AST, bound: int) -> bool:
    """Whether ``tree`` nests more than ``bound`` levels (without recursing)."""
    level = [tree]
    for _ in range(bound + 1):
        level = [child for node in level for child in ast.iter_child_nodes(node)]
        if not level:
            return False
    return True


def parse_modules(
    snapshot: Snapshot, paths: list[str]
) -> tuple[list[ParsedModule], dict[str, str]]:
    """Parse the given snapshot paths. Returns (parsed, failed paths), each
    failed path with why: it cannot be named from a source root (empty), it
    does not parse, or it nests too deeply to walk (TOO_DEEP)."""
    parsed: list[ParsedModule] = []
    failed: dict[str, str] = {}
    children = child_modules(snapshot)
    check_depth = CHECK_DEPTH.get()
    for path in sorted(paths):
        module = module_name_for(path, snapshot.source_roots)
        if module is None:
            failed[path] = ""
            continue
        try:
            tree = ast.parse(decode_source(snapshot.files[path]), filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            failed[path] = "did not parse"
            continue
        except RecursionError:
            failed[path] = TOO_DEEP
            continue
        if check_depth and deeper_than(tree, MAX_DEPTH):
            failed[path] = TOO_DEEP
            continue
        if b"getfixturevalue" not in snapshot.files[path]:
            _mark_functions(tree.body)
        parsed.append(
            ParsedModule(
                path, module, tree, children.get(module, frozenset()), snapshot.files[path]
            )
        )
    return parsed, failed


def decorator_chain(node: ast.expr) -> tuple[list[str], ast.Call | None]:
    """Return the dotted name parts of a decorator and its call node, if any.

    ``@pytest.fixture(name="x")`` -> (["pytest", "fixture"], Call)
    ``@fixture`` -> (["fixture"], None)
    """
    call = None
    if isinstance(node, ast.Call):
        call = node
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts)), call
    return [], call


def keyword_value(call: ast.Call | None, name: str) -> ast.expr | None:
    if call is None:
        return None
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def string_literals(nodes: list[ast.expr]) -> list[str]:
    out: list[str] = []
    for n in nodes:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, (ast.List, ast.Tuple)):
            out.extend(string_literals(list(n.elts)))
    return out


def scope_functions(body: list[ast.stmt]) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for stmt in iter_scope_statements(body):
        if isinstance(stmt, FUNC_NODES):
            yield stmt


def scope_classes(body: list[ast.stmt]) -> Iterator[ast.ClassDef]:
    for stmt in iter_scope_statements(body):
        if isinstance(stmt, ast.ClassDef):
            yield stmt


def scope_assignments(body: list[ast.stmt]) -> Iterator[tuple[str, ast.expr]]:
    """Yield (name, value) for simple ``name = value`` statements in a scope."""
    for stmt in iter_scope_statements(body):
        if isinstance(stmt, DEF_NODES):
            continue
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    yield target.id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.target, ast.Name):
                yield stmt.target.id, stmt.value

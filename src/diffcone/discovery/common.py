"""Shared AST helpers for static discovery."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

from diffcone.indexer import DEF_NODES, FUNC_NODES, iter_scope_statements
from diffcone.model import AnalysisError
from diffcone.snapshot import Snapshot, module_name_for


@dataclass
class ParsedModule:
    path: str
    module: str
    tree: ast.Module


def parse_modules(snapshot: Snapshot, paths: list[str]) -> tuple[list[ParsedModule], list[str]]:
    """Parse the given snapshot paths. Returns (parsed, failed paths)."""
    parsed: list[ParsedModule] = []
    failed: list[str] = []
    for path in sorted(paths):
        module = module_name_for(path, snapshot.source_roots)
        if module is None:
            failed.append(path)
            continue
        try:
            tree = ast.parse(snapshot.files[path].decode("utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            failed.append(path)
            continue
        parsed.append(ParsedModule(path, module, tree))
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


def parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = node.args
    return [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]


def error(revision: str, path: str, message: str) -> AnalysisError:
    return AnalysisError(revision=revision, path=path, message=message)

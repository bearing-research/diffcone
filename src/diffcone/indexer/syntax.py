"""AST helpers the indexer and discovery share: name tables, source
decoding, scope statements and the hashes of bodies and definitions."""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import io
import tokenize

BUILTIN_NAMES = frozenset(dir(builtins))


# Builtins whose argument nothing can bound: what they run is a string of
# code or the module's own namespace. ``__import__`` is not among them -- it
# names a module, and a name is exactly what the literal machinery bounds
# (pandas imports its hard dependencies with ``__import__`` in a loop over a
# literal tuple, and treating that as unbounded selected its whole suite).
DYNAMIC_CALLS = frozenset({"eval", "exec", "globals", "vars"})


# Reflection that observes names or signatures without naming them. Recorded
# for evidence mode only (SourceIndex.reflection); static planning ignores it.
REFLECTIVE_BUILTINS = frozenset({"dir", "hasattr", "vars"})


REFLECTIVE_CALLS = frozenset(
    {
        "inspect.getmembers",
        "inspect.getmembers_static",
        "inspect.signature",
        "inspect.getfullargspec",
        "inspect.getcallargs",
        "inspect.get_annotations",
        "typing.get_type_hints",
        "annotationlib.get_annotations",
    }
)


REFLECTIVE_ATTRIBUTES = frozenset(
    {
        "__dict__",
        "__annotations__",
        "__signature__",
        "__code__",
        "__defaults__",
        "__kwdefaults__",
        "modules",  # sys.modules: any module, found by name
        # Hands out classes without naming them (``Base.__subclasses__()``,
        # ``type.__subclasses__(c)``): evidence mode's holders of a test fake.
        "__subclasses__",
    }
)


# How far to follow a module name passed from caller to caller before giving
# up and leaving the dynamic reference where it is.
IMPORT_ATTRIBUTION_DEPTH = 4


DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def decode_source(data: bytes) -> str:
    """A source file as text, in the encoding it declares. Python reads a
    coding cookie (PEP 263) or a BOM before it reads the source, and a file
    that declares one is not UTF-8 (pip's latin-1 test package)."""
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class _StripDefs(ast.NodeTransformer):
    """Remove nested definitions (and optionally imports) from a scope body.

    Definitions are hashed as their own symbols and imports as the module's
    definition hash, so leaving them here would double-count changes.
    """

    def __init__(self, strip_imports: bool) -> None:
        self.strip_imports = strip_imports

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt | None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.stmt | None:
        return self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.stmt | None:
        return None

    def visit_Import(self, node: ast.Import) -> ast.stmt | None:
        return None if self.strip_imports else node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.stmt | None:
        return None if self.strip_imports else node


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def hash_nodes(nodes: list[ast.AST]) -> str:
    return _digest("\n".join(_dump(n) for n in nodes))


def type_params(node: ast.AST) -> list[ast.AST]:
    """The PEP 695 type parameters of a ``def``, ``class`` or ``type``
    statement (none before Python 3.12)."""
    return list(getattr(node, "type_params", None) or ())


def type_param_exprs(node: ast.AST) -> list[ast.expr]:
    """The expressions inside a statement's type parameters: bounds,
    constraints and (PEP 696) defaults. Python evaluates them lazily, when
    ``__bound__``/``__default__`` is read, but what they name is a
    dependency of the statement all the same."""
    exprs: list[ast.expr] = []
    for param in type_params(node):
        for field in ("bound", "default_value"):
            value = getattr(param, field, None)
            if isinstance(value, ast.expr):
                exprs.append(value)
    return exprs


def _contains_definition_or_import(stmt: ast.stmt, strip_imports: bool) -> bool:
    for node in ast.walk(stmt):
        if isinstance(node, DEF_NODES):
            return True
        if strip_imports and isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
    return False


def _split_docstring(stmts: list[ast.stmt]) -> tuple[str, list[ast.stmt]]:
    """(docstring text, statements without it) for a module/class/function body."""
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        return stmts[0].value.value, stmts[1:]
    return "", stmts


def _docstring_hash(bodies: list[list[ast.stmt]]) -> str:
    docs = [_split_docstring(b)[0] for b in bodies]
    return _digest("\n".join(docs)) if any(docs) else ""


def hash_scope_body(stmts: list[ast.stmt], strip_imports: bool) -> str:
    """Hash a scope's statements with nested definitions (and optionally
    imports) removed. Only statements that actually contain one are copied
    and stripped; the rest are dumped as they are, which avoids a deep copy
    of every statement (the dominant cost of indexing)."""
    kept: list[ast.AST] = []
    for stmt in stmts:
        if isinstance(stmt, DEF_NODES) or (
            strip_imports and isinstance(stmt, (ast.Import, ast.ImportFrom))
        ):
            continue
        if _contains_definition_or_import(stmt, strip_imports):
            stripped = _StripDefs(strip_imports).visit(copy.deepcopy(stmt))
            if stripped is not None:
                kept.append(stripped)
        else:
            kept.append(stmt)
    return hash_nodes(kept)


def iter_scope_statements(body: list[ast.stmt]):
    """Yield statements of a scope, descending into compound statements but
    never into function or class definitions."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, DEF_NODES):
            continue
        for attr in ("body", "orelse", "finalbody"):
            child = getattr(stmt, attr, None)
            if isinstance(child, list):
                yield from iter_scope_statements(child)
        for handler in getattr(stmt, "handlers", []) or []:
            yield from iter_scope_statements(handler.body)
        for case in getattr(stmt, "cases", []) or []:
            yield from iter_scope_statements(case.body)

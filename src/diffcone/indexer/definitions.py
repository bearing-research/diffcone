"""Facts about definitions the indexer hashes and classifies: line spans,
decorators, class attributes, variable statements, inert definitions."""

from __future__ import annotations

import ast
from collections import defaultdict

from diffcone.indexer.literals import _collect_store_names
from diffcone.indexer.scopes import ModuleScope, VariableStatement, _absolute_module
from diffcone.indexer.syntax import DEF_NODES, _digest, iter_scope_statements
from diffcone.model import CLASS_STATEMENT, OPAQUE_ATTRIBUTE


def _canonical_imports(scope: ModuleScope) -> set[str]:
    """One string per imported binding, independent of statement grouping or
    order: adding a name to ``from m import (a, b)`` adds one entry."""
    out: set[str] = set()
    for node in scope.import_nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(scope, node.module, node.level)
            for alias in node.names:
                entry = f"from {base} import {alias.name}"
                out.add(entry + (f" as {alias.asname}" if alias.asname else ""))
    return out


def _import_layout(scope: ModuleScope) -> tuple[str, ...]:
    """Each module-level import binding, in source order, prefixed with the
    blocks it sits in (``if <test>``, ``try``, ``except``, ``else``, ...):
    what runs at import depends on where an import is, not only on which
    names are bound."""
    assert scope.tree is not None
    out: list[str] = []

    def walk(body: list[ast.stmt], context: str) -> None:
        # Ordinary statements before an import in its block: an import moved
        # across ``os.environ[...] = ...`` or ``sys.path.insert`` changes what
        # it runs with. Counted, not hashed: editing them is a body change.
        before = 0
        for stmt in body:
            if isinstance(stmt, DEF_NODES):
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for entry in _import_entries(scope, stmt):
                    out.append(f"{context}#{before}|{entry}")
                continue
            before += 1
            label = type(stmt).__name__
            if isinstance(stmt, (ast.If, ast.While)):
                label += f"({ast.unparse(stmt.test)})"
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                label += f"({', '.join(ast.unparse(i) for i in stmt.items)})"
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                label += f"({ast.unparse(stmt.target)} in {ast.unparse(stmt.iter)})"
            elif isinstance(stmt, ast.Match):
                label += f"({ast.unparse(stmt.subject)})"
            for attr in ("body", "orelse", "finalbody"):
                child = getattr(stmt, attr, None)
                if isinstance(child, list) and child:
                    walk(child, f"{context}/{label}.{attr}")
            for i, handler in enumerate(getattr(stmt, "handlers", []) or []):
                walk(handler.body, f"{context}/{label}.except{i}")
            for i, case in enumerate(getattr(stmt, "cases", []) or []):
                walk(case.body, f"{context}/{label}.case{i}")

    walk(scope.tree.body, "")
    return tuple(out)


def _import_entries(scope: ModuleScope, node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [f"import {a.name}" + (f" as {a.asname}" if a.asname else "") for a in node.names]
    base = _absolute_module(scope, node.module, node.level)
    return [
        f"from {base} import {a.name}" + (f" as {a.asname}" if a.asname else "") for a in node.names
    ]


def _class_attributes(node: ast.ClassDef) -> dict[str, str]:
    """Each name a class body binds by plain assignment, with a hash of the
    statements binding it; every other non-definition statement (a loop, a
    call, a ``del``, a conditional binding) is hashed under OPAQUE_ATTRIBUTE,
    and the class statement itself (bases, keywords, decorators) under
    CLASS_STATEMENT.
    The docstring is not an attribute here: it has its own hash."""
    parts: dict[str, list[str]] = defaultdict(list)
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    for stmt in body:
        if isinstance(stmt, DEF_NODES):
            continue
        dumped = ast.dump(stmt)
        names: list[str] = []
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                elements = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                plain = [e.id for e in elements if isinstance(e, ast.Name)]
                if len(plain) != len(elements):
                    names = []
                    break
                names += plain
        for name in names or [OPAQUE_ATTRIBUTE]:
            parts[name].append(dumped)
    parts[CLASS_STATEMENT] = [
        ast.dump(n) for n in [*node.bases, *node.keywords, *node.decorator_list]
    ]
    return {name: _digest("\n".join(dumps)) for name, dumps in sorted(parts.items())}


def _start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    """First line of a definition including its decorators, which belong to
    the definition (they are part of its definition hash)."""
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _variable_statements(scope: ModuleScope, body: list[ast.stmt]) -> dict[str, VariableStatement]:
    """Top-level ``NAME = <expr>`` / ``NAME: T = <expr>`` statements whose name
    is bound exactly once in the module: candidates for variable symbols.
    Names bound any other way too (in a loop, by unpacking, inside a block)
    stay on the module symbol."""
    assert scope.tree is not None
    simple: dict[str, list[VariableStatement]] = defaultdict(list)
    for stmt in body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target = stmt.target
        else:
            continue
        if isinstance(target, ast.Name):
            simple[target.id].append(stmt)
    simple_stmts = {id(s) for stmts in simple.values() for s in stmts}
    other_bound: set[str] = set()
    for stmt in iter_scope_statements(scope.tree.body):
        if isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
            continue
        if id(stmt) in simple_stmts:
            continue
        other_bound |= _collect_store_names(stmt)
    return {
        name: stmts[0]
        for name, stmts in simple.items()
        if len(stmts) == 1 and name not in other_bound and name not in scope.imports
    }


def _end_line(node: ast.stmt | ast.Module) -> int:
    """Last source line of a definition; a Module has no position of its own."""
    if isinstance(node, ast.Module):
        return node.body[-1].end_lineno or 1 if node.body else 1
    return node.end_lineno or node.lineno


def _has_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == name:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == name:
            return True
    return False


# Bases outside the source roots that only add structure and never call
# a subclass's methods.
_STRUCTURAL_BASES = frozenset(
    {
        "abc.ABC",
        "typing.Generic",
        "typing.Protocol",
        "typing.NamedTuple",
        "typing.TypedDict",
        "typing_extensions.Generic",
        "typing_extensions.Protocol",
        "typing_extensions.NamedTuple",
        "typing_extensions.TypedDict",
    }
)


# Reached explicitly: construction and class creation record their own edges.
_EXPLICIT_SPECIAL_METHODS = frozenset({"__init__", "__new__", "__init_subclass__"})


def _is_special_method(name: str) -> bool:
    return (
        len(name) > 4
        and name.startswith("__")
        and name.endswith("__")
        and name not in _EXPLICIT_SPECIAL_METHODS
    )


def _annotated_args(args: ast.arguments) -> list[ast.arg]:
    """The parameters that carry an annotation, in signature order."""
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [a for a in params if a is not None and a.annotation is not None]


# Decorators that run no code with the function beyond returning it (or
# recording it in typing's overload registry), by canonical name.
_INERT_DECORATORS = frozenset(
    f"{module}.{name}"
    for module in ("typing", "typing_extensions")
    for name in ("overload", "override", "final")
)


def _is_inert_decorator(node: ast.expr, scope: ModuleScope) -> bool:
    """Only the ``typing``/``typing_extensions`` decorators, resolved through
    the module's imports: a project decorator named ``final`` may do anything."""
    parts = _flatten_chain(node)
    if parts is None:
        return False
    binding = scope.imports.get(parts[0])
    if binding is None:
        return False
    base = binding.module if binding.attr is None else f"{binding.module}.{binding.attr}"
    return ".".join([base, *parts[1:]]) in _INERT_DECORATORS


def _is_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_literal(k) for k in node.keys) and all(
            _is_literal(v) for v in node.values
        )
    return False


def _inert_def(node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ModuleScope) -> bool:
    """Whether executing the ``def`` runs no code beyond binding the name:
    inert decorators and literal defaults (annotations are checked apart)."""
    defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    return all(_is_inert_decorator(d, scope) for d in node.decorator_list) and all(
        _is_literal(d) for d in defaults
    )


def _has_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return node.returns is not None or any(
        isinstance(a, ast.arg) and a.annotation is not None for a in ast.walk(node.args)
    )


def _future_annotations(scope: ModuleScope) -> bool:
    binding = scope.imports.get("annotations")
    return binding is not None and binding.module == "__future__"


def _is_staticmethod(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return _has_decorator(node, "staticmethod")


# Defining one of these changes what ``self.<attr>`` reads or writes.
_ATTRIBUTE_HOOKS = frozenset({"__setattr__", "__delattr__", "__getattr__", "__getattribute__"})


def _rebound_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every name the function's body (nested scopes included, which only
    over-approximates) binds, deletes or imports."""
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and not isinstance(inner.ctx, ast.Load):
            names.add(inner.id)
        elif isinstance(inner, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and inner.name:
            names.add(inner.name)
        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in inner.names}
    return names


def _flatten_chain(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None

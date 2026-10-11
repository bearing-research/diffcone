"""What a scope binds its names to, for the names bound to a module by
another name (``m = ops``): the resolver reads an attribute off such a name
as off the module (``Resolver.module_alias``). Pure syntax; which chains
are modules is the resolver's to say.

For a module, a function or a lambda, each binding the scope makes of a
name is one of

* the name chain it assigns: ``m = ops``, ``m = pkg.ops``, ``m: T = ops``,
  ``(m := ops)``, ``m, n = ops, other`` (a literal tuple or list unpacked
  into one of the same length), ``for m in (ops, other)`` (each element);
* ``IMPORTED``: an import statement binds it;
* ``OPAQUE``: anything else (a parameter, a call's result, ``with ... as``,
  a ``def`` or ``class``, an augmented assignment, an exception, a match
  capture, unpacking anything but a literal of the same length).

A binding through ``global`` (``nonlocal``) in a function or class body
nested in the scope counts for the module (for the enclosing function binding the name;
every enclosing function that binds it, which can only add bindings). A
chain assigned there counts only when its first name is bound in no
function on the way, so that it means there what it means in the scope
that owns the name; otherwise the binding is ``OPAQUE``. A comprehension's
own variables are its own, but a walrus inside one binds in the enclosing
scope; ``del`` binds nothing. Only names with at least one chain are kept.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from diffcone.indexer.literals import _LocalBindings
from diffcone.indexer.syntax import FUNC_NODES, _flatten_chain

Value = tuple[str, ...]
OPAQUE: Value = ()
IMPORTED: Value = ("<import>",)

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def scope_aliases(scope: ast.AST) -> dict[str, tuple[Value, ...]]:
    """Every binding of each name ``scope`` (a module, function or lambda)
    binds, for the names one of whose bindings is a name chain."""
    found: dict[str, list[Value]] = {}
    declared: set[str] = set()
    if not isinstance(scope, ast.Module):
        for node in _own_nodes(scope):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                declared.update(node.names)
    for name, value in _bindings(scope):
        if name not in declared:
            found.setdefault(name, []).append(value)
    keyword = ast.Global if isinstance(scope, ast.Module) else ast.Nonlocal
    for fn, between in _nested_functions(scope, frozenset()):
        names = {n for node in _own_nodes(fn) if isinstance(node, keyword) for n in node.names}
        if not names:
            continue
        local = _LocalBindings().collect(fn) | between
        for name, value in _bindings(fn):
            if name not in names or (keyword is ast.Nonlocal and name not in found):
                continue
            if value not in (OPAQUE, IMPORTED) and value[0] in local:
                value = OPAQUE  # its first name is the function's own
            found.setdefault(name, []).append(value)
    return {
        name: tuple(values)
        for name, values in found.items()
        if any(v not in (OPAQUE, IMPORTED) for v in values)
    }


def _nested_functions(
    scope: ast.AST, between: frozenset[str]
) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, frozenset[str]]]:
    """Each function and class body nested in ``scope`` at any depth (a
    class body may declare ``global`` or ``nonlocal`` too), with the names
    the functions between bind (a class body's names are not visible in
    the scopes nested in it)."""
    for node in _own_nodes(scope):
        if isinstance(node, FUNC_NODES):
            yield node, between
            yield from _nested_functions(node, between | _LocalBindings().collect(node))
        elif isinstance(node, ast.ClassDef):
            yield node, between
            yield from _nested_functions(node, between)


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes in ``scope``'s own scope, nested ``def``/``class``/
    ``lambda`` statements included but not their bodies (their decorators,
    defaults and bases are evaluated here)."""
    if isinstance(scope, ast.Lambda):
        stack: list[ast.AST] = [scope.body]
    else:
        stack = list(getattr(scope, "body", []))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            outer: list[ast.AST] = []
            if not isinstance(node, ast.Lambda):
                outer += node.decorator_list
            if isinstance(node, ast.ClassDef):
                outer += node.bases + [k.value for k in node.keywords]
            else:
                outer += node.args.defaults + [d for d in node.args.kw_defaults if d]
            stack.extend(outer)
            continue
        stack.extend(ast.iter_child_nodes(node))


def _chain(value: ast.expr) -> Value:
    while isinstance(value, ast.NamedExpr):
        value = value.value
    parts = _flatten_chain(value)
    return tuple(parts) if parts else OPAQUE


def _pairs(target: ast.expr, value: ast.expr, out: dict[int, list[Value]]) -> None:
    """The chain each name of an assignment target is given."""
    if isinstance(target, ast.Name):
        out.setdefault(id(target), []).append(_chain(value))
    elif (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
        and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))
    ):
        for t, v in zip(target.elts, value.elts, strict=True):
            _pairs(t, v, out)


def _bindings(scope: ast.AST) -> Iterator[tuple[str, Value]]:
    """(name, value) for each binding ``scope`` makes in its own scope."""
    if isinstance(scope, FUNC_NODES + (ast.Lambda,)):
        for arg in ast.walk(scope.args):
            if isinstance(arg, ast.arg):
                yield arg.arg, OPAQUE
    nodes = list(_own_nodes(scope))
    assigned: dict[int, list[Value]] = {}
    skipped: set[int] = set()  # comprehension targets: the comprehension's own
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _pairs(target, node.value, assigned)
        elif isinstance(node, ast.AnnAssign):
            if node.value is None:
                skipped.add(id(node.target))  # ``m: T`` alone binds nothing
            else:
                _pairs(node.target, node.value, assigned)
        elif isinstance(node, ast.NamedExpr):
            _pairs(node.target, node.value, assigned)
        elif isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
            elements = (
                node.iter.elts if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)) else []
            )
            if elements and not any(isinstance(e, ast.Starred) for e in elements):
                assigned[id(node.target)] = [_chain(e) for e in elements]
        elif isinstance(node, ast.comprehension):
            skipped.update(id(n) for n in ast.walk(node.target))
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if id(node) in skipped:
                continue
            yield from ((node.id, v) for v in assigned.get(id(node), [OPAQUE]))
        elif node is not scope and isinstance(node, _SCOPES) and not isinstance(node, ast.Lambda):
            yield node.name, OPAQUE
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    yield alias.asname or alias.name.split(".")[0], IMPORTED
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name, OPAQUE
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest, OPAQUE

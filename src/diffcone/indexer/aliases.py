"""What a scope binds its names to, for the names bound to a module by
another name (``m = ops``): the resolver reads an attribute off such a name
as off the module (``Resolver.module_alias``). Pure syntax; which chains
are modules is the resolver's to say. ``name_sources`` (at the end) is the
wider record the write tracing in references.py reads: every expression a
binding takes its object from, and how.

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
from diffcone.indexer.uses import CONTAINER_MUTATORS

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


# How a binding gives a name its value (name_sources): the value itself
# (``c = X``, ``(c := X)``), an element of it (``for c in X``, ``a, b = X``,
# a ``case`` capture of ``match X``), or what entering it gives (``with X as
# c``: ``X.__enter__()``, which may be ``X`` or anything it holds); and an
# object the name's own object is stored into (``X[k] = entry = {}``, ``X[k]
# = buf``, ``X.append(buf)``, ``setattr(X, n, buf)``), which holds it then.
VALUE, ELEMENT, ENTERED, STORED = "value", "element", "entered", "stored"
Sources = dict[str, list[tuple[ast.expr, str]]]

_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def name_sources(scope: ast.AST) -> Sources:
    """What each binding ``scope`` (a function, a lambda or a comprehension)
    makes of a name in its own scope gives it: name -> (expression, how)
    with ``how`` VALUE (the expression's value), ELEMENT (an element of it:
    a ``for`` or comprehension target, a match capture), ENTERED (``with
    expr as name``) or STORED (the name's object is stored into the
    expression: ``X[k] = name``, ``X.append(name)``, ``setattr(X, n,
    name)``, so it is part of X). A binding that takes nothing
    from an expression the scope evaluates (a parameter, an import, a
    ``def``, an exception, an augmented assignment) gives nothing. For the
    writes made through a name bound to (part of) another object
    (references.py, ``_alias_roots``)."""
    found: Sources = {}
    if isinstance(scope, _COMPREHENSIONS):
        for gen in scope.generators:
            for name in _target_names(gen.target):
                found.setdefault(name, []).append((gen.iter, ELEMENT))
        return found
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _target_sources(target, node.value, VALUE, found)
            # ``X[k] = entry = {}``, ``X[k] = buf``: the object is X's then.
            holders = [
                t.value for t in node.targets if isinstance(t, (ast.Subscript, ast.Attribute))
            ]
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            names += _stored_names(node.value)
            for holder in holders:
                for name in names:
                    found.setdefault(name, []).append((holder, STORED))
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            holder: ast.expr | None = None
            values: list[ast.expr] = []
            if isinstance(func, ast.Attribute) and func.attr in CONTAINER_MUTATORS:
                holder = func.value  # ``X.append(buf)``, ``X.setdefault(k, entry)``
                values = [*node.args, *(k.value for k in node.keywords)]
            elif isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) == 3:
                holder, values = node.args[0], node.args[2:]
            if holder is not None:
                for value in values:
                    for name in _stored_names(value):
                        found.setdefault(name, []).append((holder, STORED))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _target_sources(node.target, node.value, VALUE, found)
        elif isinstance(node, ast.NamedExpr):
            _target_sources(node.target, node.value, VALUE, found)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _target_names(node.target):
                found.setdefault(name, []).append((node.iter, ELEMENT))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for name in _target_names(item.optional_vars):
                        found.setdefault(name, []).append((item.context_expr, ENTERED))
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for name in _target_names(case.pattern):
                    found.setdefault(name, []).append((node.subject, ELEMENT))
    return found


def _stored_names(value: ast.expr) -> list[str]:
    """The names whose objects ``value`` stores as they are: the name itself,
    or one in a display it builds (``(a, buf)``, ``{"k": buf}``)."""
    if isinstance(value, ast.Name):
        return [value.id]
    if isinstance(value, ast.Starred):
        return _stored_names(value.value)
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return [n for e in value.elts for n in _stored_names(e)]
    if isinstance(value, ast.Dict):
        return [n for v in value.values for n in _stored_names(v)]
    return []


def _target_names(target: ast.AST) -> set[str]:
    """The names an assignment target or a ``case`` pattern binds."""
    names: set[str] = set()
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return names


def _target_sources(target: ast.expr, value: ast.expr, how: str, out: Sources) -> None:
    """What ``target = value`` gives the names it binds: the value for a
    name, the matching element of a literal of the same length, and an
    element of the value for a name unpacked from anything else."""
    if isinstance(target, ast.Name):
        out.setdefault(target.id, []).append((value, how))
    elif isinstance(target, ast.Starred):
        _target_sources(target.value, value, ELEMENT, out)
    elif isinstance(target, (ast.Tuple, ast.List)):
        if (
            isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
            and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))
        ):
            for t, v in zip(target.elts, value.elts, strict=True):
                _target_sources(t, v, how, out)
        else:
            for name in _target_names(target):
                out.setdefault(name, []).append((value, ELEMENT))

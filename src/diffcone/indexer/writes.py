"""Writes through a parameter or a receiver.

A function that changes an object it was handed, in place, writes whatever
its caller handed it: ``set_mode(REG)`` fills ``REG`` when ``set_mode(d)``
does ``d["k"] = v``, and ``registry.register(x)`` changes ``registry`` when
``register`` appends to ``self.items``. The reader of ``REG`` depends on the
caller as it depends on a direct writer (a ``mutated_by`` edge).

Pass 2 records, per function (cacheable per module):

* the parameters it writes in place (``param_writes``): an item or
  attribute assigned, augmented or deleted (``p[k] = v``, ``p.a = v``,
  ``del p[k]``, ``p += [x]``), a container mutator called on it or on
  something it holds (``p.append(x)``, ``p.items.add(x)``), ``setattr``/
  ``delattr``/``vars(p)``/``p.__dict__``, or a standard-library function
  that changes its first argument (``heapq.heappush(p, x)``). ``self`` is a
  parameter: a method that writes ``self.x`` writes its receiver. A local
  bound once to a parameter (``d = p``) is that parameter;
* every object it hands to a call (``passes``): one of its parameters, or a
  module-level variable (``REG``, ``cfg.REG``, ``REG["sub"]``), as an
  argument or as the receiver of a method call, with the callee when it
  resolves and its name otherwise.

After pass 2 (``apply_writes``), a parameter handed to a callee's written
parameter is written too, until nothing changes; then every variable handed
to a written parameter gets a ``mutated_by`` edge to the caller. A callee
that does not resolve (``obj.fill(REG)``, ``registry.register(x)``) may be
any in-scope function or method of that name, unless the receiver is a
variable whose value is known: an instance of an in-scope class (only that
class's method), a dict, list or set display (only the container
mutators, which pass 2 already records as writes), or a third-party value
(a call into a module outside the roots, or a project factory whose every
``return`` is one, ``cast(T, ...)`` included: only the call is trusted), whose
methods are those of in-scope classes deriving from a third-party class
(``logging.setLoggerClass(StructuredLogger)``).

Not modelled: an object kept and written through later under another name
(``self.d = d`` in ``__init__``, then ``self.d[k] = v``; a parameter
returned to the caller), and a third-party function that changes an
argument in place.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from typing import TYPE_CHECKING

from diffcone.indexer.definitions import _flatten_chain
from diffcone.indexer.scopes import External, Resolved, Scope
from diffcone.indexer.uses import CONTAINER_MUTATORS
from diffcone.model import CLASS, FUNCTION, METHOD, REFERENCES, VARIABLE, Edge

if TYPE_CHECKING:
    from diffcone.indexer.references import _ReferenceCollector
    from diffcone.indexer.resolver import Resolver

# Standard-library functions that change their first argument in place.
ARGUMENT_MUTATORS = frozenset(
    {
        "heapq.heappush",
        "heapq.heappop",
        "heapq.heapify",
        "heapq.heappushpop",
        "heapq.heapreplace",
        "bisect.insort",
        "bisect.insort_left",
        "bisect.insort_right",
        "random.shuffle",
        "operator.setitem",
        "operator.delitem",
        "operator.iadd",
        "operator.iconcat",
        "operator.ior",
        "operator.iand",
        "operator.isub",
        "operator.ixor",
        "setattr",
        "delattr",
        "object.__setattr__",
        "object.__delattr__",
        "dict.update",
        "dict.setdefault",
        "dict.pop",
        "dict.popitem",
        "dict.clear",
        "dict.__setitem__",
        "dict.__delitem__",
        "list.append",
        "list.extend",
        "list.insert",
        "list.remove",
        "list.pop",
        "list.clear",
        "list.sort",
        "list.reverse",
        "list.__setitem__",
        "list.__delitem__",
        "set.add",
        "set.discard",
        "set.remove",
        "set.pop",
        "set.clear",
        "set.update",
        "set.difference_update",
        "set.intersection_update",
        "set.symmetric_difference_update",
    }
)

# Slot of the receiver in a pass record.
RECEIVER = "self"

# A pass record: (caller, what is handed on, callee or "", called name,
# slot, receiver bound). What is handed on is ``param:<name>`` or
# ``var:<symbol id>``; the slot is a positional index, ``=<keyword>`` or
# RECEIVER.
Pass = tuple[str, str, str, str, str, bool]


# --------------------------------------------------------------------------- pass 2


def _own_param(collector: _ReferenceCollector, name: str) -> str | None:
    """``name`` when it is a parameter of the function being walked (a
    nested scope that binds the name hides it)."""
    scope = collector.scope
    while scope is not None:
        if name in scope.params:
            return name
        if name in scope.literal_bound:
            return None
        scope = scope.literal_parent
    return None


def _root_name(expr: ast.expr) -> ast.Name | None:
    """The name an item or attribute chain starts from (``p`` of
    ``p.items[0].x``)."""
    while isinstance(expr, (ast.Subscript, ast.Attribute)):
        expr = expr.value
    return expr if isinstance(expr, ast.Name) else None


def _param_of(collector: _ReferenceCollector, expr: ast.expr) -> str | None:
    """The function's parameter ``expr`` is, or holds part of: ``p``,
    ``p.x``, ``p[k]``, or a local bound once to one of those."""
    root = _root_name(expr)
    if root is None:
        return None
    param = _own_param(collector, root.id)
    if param is not None:
        return param
    if root.id in collector.scope.locals:
        source = collector._local_source(root.id)
        if source is not None and source is not expr:
            inner = _root_name(source)
            if inner is not None and inner is not root:
                return _own_param(collector, inner.id)
    return None


def _variable_of(collector: _ReferenceCollector, expr: ast.expr, depth: int = 0) -> str | None:
    """The module-level variable ``expr`` is or holds part of (``REG``,
    ``cfg.REG``, ``REG["sub"]``, a local bound once to one of those)."""
    while isinstance(expr, ast.Subscript):
        expr = expr.value
    if isinstance(expr, ast.Name) and expr.id in collector.scope.locals:
        if _own_param(collector, expr.id) is not None or depth > 3:
            return None  # (``a = b[0]`` and ``b = a[0]`` would go round)
        source = collector._local_source(expr.id)
        if source is None:
            return None
        return _variable_of(collector, source, depth + 1)
    parts = _flatten_chain(expr)
    if parts is None:
        return None
    if parts[0] in collector.scope.locals and parts[0] not in collector.scope.param_aliases:
        return None
    target = collector.indexer.resolve_chain(parts, collector.scope)
    if not isinstance(target, Resolved):
        return None
    symbol = collector.indexer.index.symbols.get(target.symbol)
    if symbol is None or symbol.kind != VARIABLE or symbol.id == collector.source:
        return None
    return symbol.id


def _write(collector: _ReferenceCollector, expr: ast.expr) -> None:
    """``expr`` (or what it holds) is changed in place."""
    param = _param_of(collector, expr)
    if param is not None:
        collector.indexer.out.param_writes.add((collector.source, param))


def observe_assign(collector: _ReferenceCollector, targets: list[ast.expr]) -> None:
    """An assignment or ``del``: an item or attribute of a parameter
    changes it."""
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, (ast.Subscript, ast.Attribute)) and not isinstance(
                sub.ctx, ast.Load
            ):
                _write(collector, sub.value)


def observe_augassign(collector: _ReferenceCollector, node: ast.AugAssign) -> None:
    """``p += [x]`` changes a list or dict parameter in place; so does an
    augmented item or attribute."""
    target = node.target
    _write(
        collector, target.value if isinstance(target, (ast.Subscript, ast.Attribute)) else target
    )


def observe_call(collector: _ReferenceCollector, node: ast.Call, parts: list[str] | None) -> None:
    """A call that writes a parameter, or hands a parameter or a module
    variable on to a callee."""
    func = node.func
    indexer = collector.indexer
    canonical = collector._canonical_name(parts) if parts is not None else None
    if (
        parts is not None
        and canonical in ARGUMENT_MUTATORS
        and node.args
        and (
            len(parts) > 1
            or canonical != parts[0]  # imported: ``from heapq import heappush``
            or not collector._is_shadowed(parts[0])  # the builtin ``setattr``
        )
    ):
        first = node.args[0]
        _write(collector, first)
        _dict_receiver(collector, first)
        if not isinstance(first, ast.Starred):
            collector._mutation_target(first)  # a module variable: ``heappush(QUEUE, x)``
    if isinstance(func, ast.Attribute):
        if func.attr in CONTAINER_MUTATORS or func.attr in ("__setattr__", "__delattr__"):
            _write(collector, func.value)
            _dict_receiver(collector, func.value)
    passes: list[tuple[str, ast.expr, str]] = []
    for i, arg in enumerate(node.args):
        if isinstance(arg, ast.Starred):
            break
        passes.append((str(i), arg, ""))
    for kw in node.keywords:
        if kw.arg is not None:
            passes.append((f"={kw.arg}", kw.value, ""))
    handed: list[tuple[str, str]] = []  # (slot, what)
    for slot, expr, _ in passes:
        what = _handed(collector, expr)
        if what is not None:
            handed.append((slot, what))
    if isinstance(func, ast.Attribute) and func.attr not in CONTAINER_MUTATORS:
        what = _handed(collector, func.value)
        if what is not None:
            handed.append((RECEIVER, what))
    if not handed:
        return
    name = func.attr if isinstance(func, ast.Attribute) else (parts[-1] if parts else "")
    if not name:
        return
    callees: list[str] = []
    bound = isinstance(func, ast.Attribute)
    if parts is not None:
        target = indexer.resolve_chain(parts, collector.scope)
        if isinstance(target, External):
            return  # a third-party callee runs none of our code
        if isinstance(target, Resolved) and not target.detail:
            symbol = indexer.index.symbols.get(target.symbol)
            if symbol is not None and symbol.kind == VARIABLE and len(parts) > 1:
                pass  # a method of the variable's value: by name (apply_writes)
            elif symbol is not None and symbol.kind == CLASS:
                init = indexer.lookup_in_class(symbol.id, "__init__")
                if isinstance(init, Resolved) and not init.detail:
                    callees.append(init.symbol)
                bound = True
                if not callees:
                    return  # no in-scope constructor: nothing of ours runs
            elif symbol is not None and symbol.kind in (FUNCTION, METHOD):
                callees.append(symbol.id)
                callees += [o for o, detail in target.overrides if not detail]
                if symbol.kind == METHOD and len(parts) == 2:
                    base = indexer._lookup_base(parts[0], collector.scope)
                    bound = not (
                        isinstance(base, Resolved)
                        and not base.detail
                        and base.symbol in indexer.class_scopes
                        and parts[0] != collector.scope.self_name
                    )
            elif symbol is not None:
                return  # a variable or module called: not a function of ours
    out = indexer.out.passes
    for slot, what in handed:
        for callee in callees or [""]:
            out.add((collector.source, what, callee, name, slot, bound))


def _dict_receiver(collector: _ReferenceCollector, expr: ast.expr) -> None:
    """``vars(p).update(...)`` / ``p.__dict__[k] = v`` write ``p``."""
    while isinstance(expr, ast.Subscript):
        expr = expr.value
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "vars"
        and expr.args
    ):
        _write(collector, expr.args[0])


def _handed(collector: _ReferenceCollector, expr: ast.expr) -> str | None:
    """What a call is handed in ``expr``: one of the function's parameters
    or a module-level variable (or part of one)."""
    if isinstance(expr, ast.Call):
        # ``vars(p)``/``p.__dict__``-style views hand on the object itself.
        if isinstance(expr.func, ast.Name) and expr.func.id == "vars" and expr.args:
            expr = expr.args[0]
        else:
            return None
    param = _param_of(collector, expr)
    if param is not None:
        return f"param:{param}"
    variable = _variable_of(collector, expr)
    if variable is not None:
        return f"var:{variable}"
    return None


def returns_third_party(indexer: Resolver, node: ast.AST, scope: Scope) -> bool:
    """Whether every ``return`` of a function yields what a call into a
    module outside the roots returns: ``return logging.getLogger(name)``,
    or that under ``typing.cast(T, ...)`` (the annotation ``T`` is not
    trusted, only the call). Such a value is a third-party object, or an
    instance of an in-scope class deriving from a third-party one
    (``logging.setLoggerClass``)."""
    returns: list[ast.Return] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        inner = stack.pop()
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(inner, (ast.Yield, ast.YieldFrom)):
            return False
        if isinstance(inner, ast.Return):
            returns.append(inner)
        stack.extend(ast.iter_child_nodes(inner))
    if not returns:
        return False
    for ret in returns:
        value = ret.value
        while (
            isinstance(value, ast.Call)
            and (parts := _flatten_chain(value.func)) is not None
            and parts[-1] == "cast"
            and len(value.args) == 2
        ):
            value = value.args[1]
        if not isinstance(value, ast.Call):
            return False
        parts = _flatten_chain(value.func)
        if parts is None or not isinstance(indexer.resolve_chain(parts, scope), External):
            return False
    return True


# --------------------------------------------------------------------------- after pass 2


def apply_writes(indexer: Resolver) -> None:
    """Close parameter writes over the calls that hand parameters on, then
    give each variable handed to a written parameter a ``mutated_by`` edge
    to the caller (see the module docstring)."""
    out = indexer.out
    params = out.func_params
    writes: set[tuple[str, str]] = set(out.param_writes)
    if not writes:
        return
    symbols = indexer.index.symbols
    by_name: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols.values():
        if symbol.kind in (FUNCTION, METHOD) and symbol.id in params:
            by_name[symbol.name].append(symbol.id)
    for ids in by_name.values():
        ids.sort()
    passes = sorted(out.passes)
    edges_from: dict[str, list[Edge]] = defaultdict(list)
    for edge in indexer.index.edges:
        if edge.kind == REFERENCES:
            edges_from[edge.source].append(edge)
    external_users = {x.symbol for x in indexer.index.external}

    def slot_param(callee: str, slot: str, bound: bool) -> str | None:
        info = params.get(callee)
        if info is None:
            return None
        if slot == RECEIVER:
            return info.positional[0] if info.bound and info.positional else None
        if slot.startswith("="):
            return slot[1:]
        index = int(slot) + (1 if info.bound and bound else 0)
        return info.positional[index] if index < len(info.positional) else None

    def third_party(variable: str) -> bool:
        """``log = logging.getLogger(__name__)``, or a project factory whose
        every return is such a call (``get_logger(__name__)``): nothing of
        ours is called but third-party calls and such factories."""
        found = edges_from.get(variable, [])
        ours = [
            target
            for e in found
            if (target := symbols.get(e.target)) is not None
            and target.kind in (FUNCTION, METHOD, CLASS)
        ]
        if any(t.id not in out.external_returns for t in ours):
            return False
        return bool(ours) or variable in external_users

    def value_classes(variable: str) -> list[str] | None:
        """The classes a variable's value is an instance of, when its
        initialiser says so (a constructor call and nothing else callable)."""
        found = edges_from.get(variable, [])
        if not any(e.detail == "constructor" for e in found):
            return None
        classes: list[str] = []
        for e in found:
            target = symbols.get(e.target)
            if target is None or e.detail:
                continue
            if target.kind in (FUNCTION, METHOD):
                return None
            if target.kind == CLASS:
                classes.append(target.id)
        return sorted(classes) or None

    def callees(record: Pass) -> list[str]:
        _, what, callee, name, slot, _ = record
        if callee:
            return [callee]
        if slot == RECEIVER and what.startswith("var:"):
            variable = symbols.get(what[4:])
            if variable is not None:
                scope = indexer.scopes.get(variable.module)
                if scope is not None and variable.name in scope.containers:
                    return []  # a display: only the container mutators write it
            if third_party(what[4:]):
                # Its methods are a third-party class's, or those of an
                # in-scope class deriving from one (a logger class set with
                # ``logging.setLoggerClass``).
                return [m for m in by_name.get(name, []) if derives_from_third_party(m)]
            classes = value_classes(what[4:])
            if classes is not None:
                found: list[str] = []
                for cls in classes:
                    hit = indexer.lookup_in_class(cls, name)
                    if isinstance(hit, Resolved) and not hit.detail:
                        found.append(hit.symbol)
                return found
        return by_name.get(name, [])

    def derives_from_third_party(method: str) -> bool:
        container = symbols[method].container if method in symbols else None
        if container is None or container not in indexer.class_scopes:
            return False
        return any(
            indexer.class_scopes[c].opaque
            for c in indexer._mro(container)
            if c in indexer.class_scopes
        )

    def writes_through(record: Pass) -> bool:
        _, _, _, _, slot, bound = record
        for callee in callees(record):
            param = slot_param(callee, slot, bound)
            if param is not None and (callee, param) in writes:
                return True
        return False

    param_passes = [p for p in passes if p[1].startswith("param:")]
    changed = True
    while changed:
        changed = False
        for record in param_passes:
            key = (record[0], record[1][len("param:") :])
            if key not in writes and writes_through(record):
                writes.add(key)
                changed = True
    for record in passes:
        caller, what = record[0], record[1]
        if not what.startswith("var:"):
            continue
        variable = what[len("var:") :]
        symbol = symbols.get(variable)
        if symbol is None or caller in (variable, symbol.module):
            # A module's own top-level statements naming the variable are
            # part of its hash already.
            continue
        if writes_through(record):
            out.edges.add(Edge(variable, caller, REFERENCES, "mutated_by"))

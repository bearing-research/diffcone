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
* every object it hands to a call (``passes``): one of its parameters, a
  module-level variable (``REG``, ``cfg.REG``, ``REG["sub"]``,
  ``import_module("pkg.cfg").REG``), or an in-scope module (``pkg.cfg``, a
  function-local import of it, ``sys.modules["pkg.cfg"]``,
  ``import_module("pkg.cfg")``, a local bound once to one of those), as an
  argument or as the receiver of a method call, with the callee when it
  resolves and its name otherwise.

After pass 2 (``apply_writes``), a parameter handed to a callee's written
parameter is written too, until nothing changes; then every variable handed
to a written parameter gets a ``mutated_by`` edge to the caller (unless its
value cannot change in place: Indexer._value_writes), and so does
every variable of a module handed to one (and of its submodules: which of
its attributes the callee writes is not recorded). A callee
that does not resolve (``obj.fill(REG)``, ``registry.register(x)``) may be
any in-scope function or method of that name, unless the receiver is a
variable whose value is known: an instance of an in-scope class (only that
class's method), a dict, list or set display (only the container
mutators, which pass 2 already records as writes), or a third-party value
(a call into a module outside the roots, or a project factory whose every
``return`` is one, ``cast(T, ...)`` included: only the call is trusted), whose
methods are those of in-scope classes deriving from a third-party class
(``logging.setLoggerClass(StructuredLogger)``). A method called on an
instance attribute (``self._client.get(...)``) is one of what the attribute
holds when every write of it says (a construction of a plainly built
in-scope class, a third-party value: ``_attribute_values``), and any method
of that name otherwise.

Each parameter write also records how it writes (``param_flavors``):
``=<attr>`` binds or deletes an attribute of the parameter itself
(``p.a = v``, ``setattr(p, "a", v)``, ``=*`` for a computed name),
``+<attr>`` augments one with a value of a builtin type that cannot change
in place (``p.n += len(s)``), ``*`` is anything else; a parameter handed
whole to a written parameter (``whole_passes``) takes the callee's kinds,
one handed in part (``p.items``) is ``*``. What a constructor of an
in-scope class was handed is handed on as ``held:<class>|<what>``: it is
written when the instance is written in a way ``_HeldSafety`` cannot rule
out (audit round 6, T23), and an explicit construction hands the arguments
to the class's ``__init__`` as its receiver, so ``self.d = d`` in
``__init__`` then ``self.d[k] = v`` in ``__init__`` or a method called on
the instance writes what the caller handed.

Not modelled: an object kept and written through later under another name
(``self.d = d`` in ``__init__``, then ``self.d[k] = v`` on an instance not
made by an explicit call of its class; a parameter
returned to the caller; a module returned by a function other code calls),
and a third-party function that changes an argument in place, a module
included (a library handed a module of the project that writes into it).

Caches outside module variables (audit round 5, R22; ``SourceIndex.stores``,
evidence mode only): a function that stores into state outliving the call
that no ``mutated_by`` edge names, whatever the value (audit T19: what a
value derives from, statically, misses a call through a variable, a table
or ``super()``, a changed constant, the function's own literal, a branch
on the change's result). Recorded per store as (function, where):
``where`` is the attribute written through a parameter, ``self``/``cls``,
a function or a class (``self._cache[k] = v``: ``_cache``; ``get.cache[k]
= v``: ``cache``), ``@self`` for the receiver itself (``self.x = v``,
``self.__dict__[k] = v``, ``vars(self)[k] = v``, ``setattr(self, k, v)``,
a method call on it that writes it: ``self.put(k, v)``, and an explicit
``self.__init__()``, ``Base.__init__(self)`` or ``super().__init__()``),
``@param`` for a parameter container (``d[k] = v``) and ``@closure`` for a
variable of an enclosing function; a ``del`` of an item or attribute is a
store too. Stores through a method on an attribute or the receiver
(``self._cache.set(k, v)``) count when the method writes its receiver
(``computed_passes``, resolved in ``apply_writes``). ``handed``:
(caller, callee, attribute) for a parameter's attribute handed to a
callee, so a store into a parameter container is read through the
attribute the caller handed.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from typing import TYPE_CHECKING

from diffcone.indexer.definitions import _ATTRIBUTE_HOOKS
from diffcone.indexer.facts import apply_fact
from diffcone.indexer.scopes import ClassScope, External, ModuleNode, Resolved, Scope
from diffcone.indexer.syntax import BUILTIN_NAMES, _flatten_chain
from diffcone.indexer.uses import CONTAINER_MUTATORS
from diffcone.model import (
    FRESH_INITS,
    MUTATED_BY,
    STORE_CLOSURE,
    STORE_PARAM,
    STORE_SELF,
    Edge,
    EdgeKind,
    SymbolKind,
)

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
# A pass record's ``held:<class id>|<what>``: ``<what>`` handed on as what a
# fresh instance of the class holds of its constructor's arguments
# (references.HELD; audit round 6, T23).
HELD_WHAT = "held:"
# A pass record's callee ``@<class id>.<attribute>``: the method of that name
# of whatever ``self.<attribute>`` holds in a method of the class.
HELD_BY = "@"

# A pass record: (caller, what is handed on, callee or "", called name,
# slot, receiver bound). What is handed on is ``param:<name>``,
# ``var:<symbol id>``, ``module:<name>`` or ``instance:[.<attribute>]:<class
# id>`` (an instance of an in-scope class the caller made, or part of one,
# reached through that attribute: audit round 6, T22); the slot is a
# positional index, ``=<keyword>`` or RECEIVER.
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


def _module_of(collector: _ReferenceCollector, expr: ast.expr, depth: int = 0) -> str | None:
    """The in-scope module ``expr`` is: a name or chain that resolves to one
    (a function-local import included), a handle a literal names
    (``sys.modules["m"]``, ``import_module("m")``), or a local bound once to
    one of those."""
    scopes = collector.indexer.scopes
    literal = collector._literal_module(expr)
    if literal is not None:
        return literal if literal in scopes else None
    if (
        isinstance(expr, ast.Name)
        and expr.id in collector.scope.locals
        and expr.id not in collector.scope.local_imports
    ):
        if _own_param(collector, expr.id) is not None or depth > 3:
            return None
        source = collector._local_source(expr.id)
        return _module_of(collector, source, depth + 1) if source is not None else None
    parts = _flatten_chain(expr)
    if parts is None:
        return None
    node = collector.indexer.resolve_chain(parts, collector.scope)
    if isinstance(node, ModuleNode):
        return node.module if node.module in scopes else None
    if isinstance(node, Resolved) and not node.detail and node.symbol in scopes:
        return node.symbol
    return None


def _variable_of(collector: _ReferenceCollector, expr: ast.expr, depth: int = 0) -> str | None:
    """The module-level variable ``expr`` is or holds part of (``REG``,
    ``cfg.REG``, ``REG["sub"]``, a local bound once to one of those, an
    attribute of an in-scope module however obtained:
    ``import_module("pkg.cfg").REG``)."""
    while isinstance(expr, ast.Subscript):
        expr = expr.value
    if isinstance(expr, ast.Attribute):
        attrs: list[str] = []
        base: ast.expr = expr
        while isinstance(base, ast.Attribute):
            attrs.append(base.attr)
            base = base.value
        if not isinstance(base, ast.Name) or base.id in collector.scope.locals:
            module = _module_of(collector, base, depth)
            if module is not None:
                found = collector.indexer.resolve_dotted([*module.split("."), *reversed(attrs)])
                if isinstance(found, Resolved) and not found.detail:
                    symbol = collector.indexer.index.symbols.get(found.symbol)
                    if (
                        symbol is not None
                        and symbol.kind == SymbolKind.VARIABLE
                        and symbol.id != collector.source
                    ):
                        return symbol.id
                return None
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
    if symbol is None or symbol.kind != SymbolKind.VARIABLE or symbol.id == collector.source:
        return None
    return symbol.id


def _write(collector: _ReferenceCollector, expr: ast.expr, how: str = "*") -> None:
    """``expr`` (or what it holds) is changed in place. ``how`` (as
    ``param_flavors`` says) holds when ``expr`` is the parameter itself:
    through an item or an attribute of it, it is ``*``."""
    param = _param_of(collector, expr)
    if param is not None:
        out = collector.indexer.out
        out.param_writes.add((collector.source, param))
        flavor = how if _whole_param(collector, expr) else "*"
        out.param_flavors.add((collector.source, param, flavor))


def _instance_what(what: str) -> bool:
    """An instance of an in-scope class, or one a constructor was handed
    (``held:<class>|instance:...``), as ``_handed_state`` names it."""
    if what.startswith(HELD_WHAT):
        what = what.partition("|")[2]
    return what.startswith("instance:")


def _whole_param(collector: _ReferenceCollector, expr: ast.expr) -> bool:
    """``expr`` is a parameter of the function itself (or a local bound once
    to one), not an item or attribute of it."""
    if not isinstance(expr, ast.Name):
        return False
    if _own_param(collector, expr.id) is not None:
        return True
    if expr.id in collector.scope.locals:
        source = collector._local_source(expr.id)
        return (
            isinstance(source, ast.Name)
            and source.id != expr.id
            and _own_param(collector, source.id) is not None
        )
    return False


def _state_of(collector: _ReferenceCollector, expr: ast.expr) -> str | None:
    """Where ``expr``, the target of a store or the receiver of a call,
    lives when that outlives the call: the attribute written through a
    parameter (``self`` and ``cls`` included), a function or a class
    (``self._cache``, ``p.items``, ``get.cache``, ``Store._cache``),
    ``STORE_SELF`` for the receiver itself, ``STORE_PARAM`` for another
    parameter itself and ``STORE_CLOSURE`` for a variable of an enclosing
    function; None for a local, or a module variable (a ``mutated_by``
    edge covers it)."""
    attrs: list[str] = []
    base: ast.expr = expr
    while isinstance(base, (ast.Subscript, ast.Attribute)):
        if isinstance(base, ast.Attribute):
            attrs.append(base.attr)
        base = base.value
    if attrs and attrs[-1] == "__dict__":
        attrs.pop()  # ``self.__dict__[k] = v`` stores into ``self`` itself
    if (
        not attrs
        and isinstance(base, ast.Call)
        and isinstance(base.func, ast.Name)
        and base.func.id == "vars"
        and len(base.args) == 1
        and not collector._is_shadowed("vars")
    ):
        return _state_of(collector, base.args[0])  # ``vars(self)[k] = v``
    if not isinstance(base, ast.Name):
        return None
    attr = attrs[-1] if attrs else ""
    scope = collector.scope
    if _own_param(collector, base.id) is not None:
        if attr:
            return attr
        return STORE_SELF if base.id == scope.self_name else STORE_PARAM
    if base.id in scope.locals:
        if base.id not in scope.literal_bound and scope.literal_parent is not None:
            return STORE_CLOSURE  # bound by an enclosing function
        source = collector._local_source(base.id)
        if source is not None and source is not expr:
            inner = _root_name(source)
            if inner is not None and inner is not base and inner.id != base.id:
                found = _state_of(collector, source)
                return found if attr == "" or found is None else attr
        return None
    if not attr:
        return None
    node = collector.indexer.resolve_chain([base.id], scope)
    if isinstance(node, Resolved) and not node.detail:
        symbol = collector.indexer.index.symbols.get(node.symbol)
        if symbol is not None and symbol.kind in (
            SymbolKind.FUNCTION,
            SymbolKind.METHOD,
            SymbolKind.CLASS,
        ):
            return attr
    return None


def _store(collector: _ReferenceCollector, target: ast.expr) -> None:
    """``target`` (an item or attribute, or the receiver of a mutating call)
    is written: a store into state outliving the call, whatever the value
    (``SourceIndex.stores``)."""
    where = _state_of(collector, target)
    if where is not None:
        collector.indexer.out.stores.add((collector.source, where))


def observe_assign(collector: _ReferenceCollector, targets: list[ast.expr]) -> None:
    """An assignment or ``del``: an item or attribute of a parameter
    changes it; one of state outliving the call is a store."""
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, (ast.Subscript, ast.Attribute)) and not isinstance(
                sub.ctx, ast.Load
            ):
                how = f"={sub.attr}" if isinstance(sub, ast.Attribute) else "*"
                _write(collector, sub.value, how)
                _store(collector, sub.value)


def observe_augassign(collector: _ReferenceCollector, node: ast.AugAssign) -> None:
    """``p += [x]`` changes a list or dict parameter in place; so does an
    augmented item or attribute."""
    target = node.target
    receiver = target.value if isinstance(target, (ast.Subscript, ast.Attribute)) else target
    how = "*"
    if isinstance(target, ast.Attribute) and collector._immutable_typed(node.value, target):
        how = f"+{target.attr}"  # ``p.n += 1``: in place only if ``n`` may be mutable
    _write(collector, receiver, how)
    _store(collector, receiver)


def observe_call(collector: _ReferenceCollector, node: ast.Call, parts: list[str] | None) -> None:
    """A call that writes a parameter, or hands a parameter or a module
    variable on to a callee."""
    func = node.func
    indexer = collector.indexer
    if (
        parts is not None
        and node.args
        and any(
            canonical in ARGUMENT_MUTATORS
            and (
                len(parts) > 1
                or canonical != parts[0]  # imported: ``from heapq import heappush``
                or not collector._is_shadowed(parts[0])  # the builtin ``setattr``
            )
            for canonical in collector._canonical_names(parts)
        )
    ):
        first = node.args[0]
        how = "*"
        if parts[-1] in ("setattr", "delattr", "__setattr__", "__delattr__"):
            names = collector.scope.string_candidates(node.args[1]) if len(node.args) > 1 else None
            how = f"={names[0]}" if names is not None and len(names) == 1 else "=*"
        _write(collector, first, how)
        _dict_receiver(collector, first)
        if not isinstance(first, ast.Starred):
            collector._mutation_target(first)  # a module variable: ``heappush(QUEUE, x)``
            _store(collector, first)
    if isinstance(func, ast.Attribute):
        if func.attr in CONTAINER_MUTATORS or func.attr in ("__setattr__", "__delattr__"):
            how = "=*" if func.attr in ("__setattr__", "__delattr__") else "*"
            _write(collector, func.value, how)
            _dict_receiver(collector, func.value)
            _store(collector, func.value)
        else:
            _computed_pass(collector, node, parts)
        if func.attr in FRESH_INITS:
            _reinitialised(collector, node, func)
    if (
        isinstance(func, ast.Name)
        and func.id in BUILTIN_NAMES
        and indexer.resolve_chain([func.id], collector.scope) is None
    ):
        # The builtin itself (nothing of the project binds the name): it is
        # no function of ours, whatever methods of that name the project has
        # (``map(is_integer, header)`` is not ``Styler.map``).
        _builtin_call(collector, node, func.id)
        return
    passes: list[tuple[str, ast.expr]] = []  # (slot, argument)
    for i, arg in enumerate(node.args):
        if isinstance(arg, ast.Starred):
            break
        passes.append((str(i), arg))
    for kw in node.keywords:
        if kw.arg is not None:
            passes.append((f"={kw.arg}", kw.value))
    handed: list[tuple[str, str]] = []  # (slot, what)
    whole: set[tuple[str, str]] = set()  # a parameter itself handed on (T23)
    for slot, expr in passes:
        what = _handed(collector, expr)
        if what is not None:
            handed.append((slot, what))
            if what.startswith("param:") and _whole_param(collector, expr):
                whole.add((slot, what))
        else:  # through a name bound to part of it, a class (audit round 6, T6, T7)
            handed += [(slot, w) for w in collector._handed_state(expr)]
    if isinstance(func, ast.Attribute) and func.attr not in CONTAINER_MUTATORS:
        what = _handed(collector, func.value, modules=False)
        if what is not None:
            handed.append((RECEIVER, what))
            if what.startswith("param:") and _whole_param(collector, func.value):
                whole.add((RECEIVER, what))
        else:
            # Not an instance of an in-scope class: the method's own writes
            # of ``self`` reach the state its class holds (T21, T22), and
            # those of an instance a constructor was handed (T23).
            handed += [
                (RECEIVER, w)
                for w in collector._handed_state(func.value, True)
                if not _instance_what(w)
            ]
    if not handed:
        return  # (an instance's constructor is handed nothing its arguments are not)
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
            if symbol is not None and symbol.kind == SymbolKind.VARIABLE and len(parts) > 1:
                pass  # a method of the variable's value: by name (apply_writes)
            elif symbol is not None and symbol.kind == SymbolKind.CLASS:
                init = indexer.lookup_in_class(symbol.id, "__init__")
                if isinstance(init, Resolved) and not init.detail:
                    callees.append(init.symbol)
                bound = True
                if not callees:
                    return  # no in-scope constructor: nothing of ours runs
                # The new instance is the initialiser's ``self``: what it
                # holds of the arguments is written when the initialiser
                # changes ``self`` in place (``self.rules[k] = v``; T23).
                for w in collector._handed_state(node):
                    if w.startswith(HELD_WHAT) and not _instance_what(w):
                        handed.append((RECEIVER, w))
                        whole.add((RECEIVER, w))
            elif symbol is not None and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                callees.append(symbol.id)
                callees += [o for o, detail in target.overrides if not detail]
                if symbol.kind == SymbolKind.METHOD and len(parts) == 2:
                    base = indexer._lookup_base(parts[0], collector.scope)
                    bound = not (
                        isinstance(base, Resolved)
                        and not base.detail
                        and base.symbol in indexer.class_scopes
                        and parts[0] != collector.scope.self_name
                    )
            elif symbol is not None:
                return  # a variable or module called: not a function of ours
        if (
            not callees
            and len(parts) == 3
            and parts[0] == collector.scope.self_name
            and collector.scope.self_class is not None
        ):
            # ``self._client.get(...)``: a method of what the instance
            # attribute holds, typed after pass 2 when every binding of it
            # says (apply_writes, audit round 3, W21).
            callees.append(f"{HELD_BY}{collector.scope.self_class}.{parts[1]}")
    out = indexer.out.passes
    for slot, what in handed:
        for callee in callees or [""]:
            out.add((collector.source, what, callee, name, slot, bound))
            if (slot, what) in whole:
                indexer.out.whole_passes.add((collector.source, what, callee, name, slot, bound))
    for slot, expr in passes:
        held = _held_attr(collector, expr)
        if held:
            for callee in callees or [""]:
                indexer.out.held_passes.add((collector.source, callee, name, slot, held, bound))


def _held_attr(collector: _ReferenceCollector, expr: ast.expr) -> str:
    """The attribute of a parameter (``self`` included) an argument hands on
    (``self._cache`` of ``fill(self._cache, k)``), "" for anything else."""
    if _param_of(collector, expr) is None:
        return ""
    where = _state_of(collector, expr)
    return where if where and not where.startswith("@") else ""


def _computed_pass(collector: _ReferenceCollector, node: ast.Call, parts: list[str] | None) -> None:
    """``self._cache.set(k, v)``, ``self.put(k, v)``: a method called on
    state outliving the call. Whether the method writes its receiver is
    known after pass 2 (``apply_writes``: ``computed_passes`` -> ``stores``)."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return
    where = _state_of(collector, func.value)
    if where is None:
        return
    indexer = collector.indexer
    callee = ""
    if parts is not None:
        target = indexer.resolve_chain(parts, collector.scope)
        if isinstance(target, External):
            return  # a third-party method keeps what no record of ours shows
        if isinstance(target, Resolved) and not target.detail:
            symbol = indexer.index.symbols.get(target.symbol)
            if symbol is not None and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                callee = symbol.id
        if (
            not callee
            and len(parts) == 3
            and parts[0] == collector.scope.self_name
            and collector.scope.self_class is not None
        ):
            callee = f"{HELD_BY}{collector.scope.self_class}.{parts[1]}"
    indexer.out.computed_passes.add((collector.source, where, callee, func.attr))


def _reinitialised(collector: _ReferenceCollector, node: ast.Call, func: ast.Attribute) -> None:
    """``obj.__init__(...)``, ``Base.__init__(obj, ...)``,
    ``super().__init__(...)`` called by name: what the initialiser binds on
    its receiver lands on an object that may already be in use (evidence
    planning drops an initialiser's own receiver stores, a fresh object's:
    ``FRESH_INITS``), so the call stores into that object."""
    receiver = func.value
    scope = collector.scope
    targets: list[ast.expr] = []
    if (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "super"
    ):
        if scope.self_name is not None and not scope.self_is_class:
            targets.append(ast.Name(id=scope.self_name, ctx=ast.Load()))
    else:
        targets.append(receiver)
        if node.args and not isinstance(node.args[0], ast.Starred):
            targets.append(node.args[0])  # ``Base.__init__(self)``
    for target in targets:
        _store(collector, target)


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


def _builtin_call(collector: _ReferenceCollector, node: ast.Call, name: str) -> None:
    """A builtin runs no function of ours on what it is handed, except the
    higher-order ones, which call a function they are handed on the items:
    ``map(f, xs, ys)`` calls ``f(x, y)``, ``filter(f, xs)`` ``f(x)``,
    ``sorted(xs, key=f)``, ``min``/``max(..., key=f)`` ``f(x)``. That call
    is recorded as one of ``f`` with the iterables (an item written is what
    the iterable holds, changed)."""
    if name in ("map", "filter"):
        if not node.args or any(isinstance(a, ast.Starred) for a in node.args):
            return
        function, items = node.args[0], list(node.args[1:])
    elif name in ("sorted", "min", "max"):
        key = next((kw.value for kw in node.keywords if kw.arg == "key"), None)
        if key is None or any(isinstance(a, ast.Starred) for a in node.args):
            return
        function, items = key, list(node.args)
    else:
        return
    if name in ("min", "max") and len(items) > 1:
        calls = [[item] for item in items]  # ``min(a, b, key=f)``: f(a), f(b)
    elif name == "map":
        calls = [items]
    else:
        calls = [[item] for item in items[:1]]
    for args in calls:
        call = ast.Call(func=function, args=args, keywords=[])
        ast.copy_location(call, node)
        observe_call(collector, call, _flatten_chain(function))


def _handed(collector: _ReferenceCollector, expr: ast.expr, *, modules: bool = True) -> str | None:
    """What a call is handed in ``expr``: one of the function's parameters,
    a module-level variable (or part of one), or (``modules``: as an
    argument) an in-scope module."""
    if isinstance(expr, ast.Call):
        # ``vars(p)``/``p.__dict__``-style views hand on the object itself.
        if isinstance(expr.func, ast.Name) and expr.func.id == "vars" and expr.args:
            expr = expr.args[0]
        else:
            module = _module_of(collector, expr) if modules else None
            return f"module:{module}" if module is not None else None
    param = _param_of(collector, expr)
    if param is not None:
        return f"param:{param}"
    variable = _variable_of(collector, expr)
    if variable is not None:
        return f"var:{variable}"
    module = _module_of(collector, expr) if modules else None
    if module is not None:
        return f"module:{module}"
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
        _apply_held_binds(indexer, _HeldSafety(indexer))
        return
    symbols = indexer.index.symbols
    by_name: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols.values():
        if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and symbol.id in params:
            by_name[symbol.name].append(symbol.id)
    for ids in by_name.values():
        ids.sort()
    passes = sorted(out.passes)
    edges_from: dict[str, list[Edge]] = defaultdict(list)
    for edge in indexer.index.edges:
        if edge.kind == EdgeKind.REFERENCES:
            edges_from[edge.source].append(edge)
    for state, writer in sorted(out.value_writes):  # edges once every module is known
        edges_from[state].append(Edge(state, writer, EdgeKind.REFERENCES, MUTATED_BY))
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
            and target.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS)
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
            if target.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                return None
            if target.kind == SymbolKind.CLASS:
                classes.append(target.id)
        return sorted(classes) or None

    def callees(record: Pass) -> list[str]:
        _, what, callee, name, slot, _ = record
        if callee.startswith(HELD_BY):
            cls, _, attr = callee[len(HELD_BY) :].rpartition(".")
            kinds = attribute_values(cls, attr)
            if kinds is None:
                return by_name.get(name, [])
            found: list[str] = []
            for kind in kinds:
                if kind == "external":
                    found += [m for m in by_name.get(name, []) if derives_from_third_party(m)]
                    continue
                hit = indexer.lookup_in_class(kind, name)
                if isinstance(hit, Resolved) and not hit.detail:
                    found.append(hit.symbol)
                elif indexer.class_scopes[kind].opaque:
                    # Not found in scope: a third-party base's method.
                    found += [m for m in by_name.get(name, []) if derives_from_third_party(m)]
            return sorted(set(found))
        if callee:
            return [callee]
        if slot == RECEIVER and what.startswith("attr:"):
            # A literal table nothing changes: its methods and its items'
            # are builtins' (T24).
            attr, _, cls = what[len("attr:") :].partition(":")
            if literal_table(cls, attr):
                return []
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

    # What may change a class attribute in place: a write of it pass 2
    # records, its module's top-level code, and (whatever the callee does)
    # any call it is handed to other than as the receiver of a method, or
    # as what a constructor was handed.
    tables_written = {(c, a) for c, a, _ in out.attr_value_writes} | out.own_class_writes
    for record in passes:
        what = record[1]
        if what.startswith(HELD_WHAT):
            what = what.partition("|")[2]
        elif record[4] == RECEIVER:
            continue
        if what.startswith("attr:"):
            attr, _, cls = what[len("attr:") :].partition(":")
            tables_written.add((cls, attr))
    for _, _, _, kind, first, second in out.held_binds:
        if kind == "attribute":
            tables_written.add((first, second))
    classes_written = {e.source for e in indexer.index.edges if e.detail == MUTATED_BY}
    classes_written |= {state for state, _ in out.value_writes}
    classes_written |= {state for state, _ in out.receiver_writes}
    # What is decided only below or after this function counts as written
    # however it ends: a class (or an instance of it, which reads the table
    # through the class) handed to a call other than as a method's receiver,
    # or held by an instance its constructor was handed, an instance of it
    # written in place, and a class, an instance or a class attribute some
    # function returns and some code writes into (Indexer._result_writes).
    classes_written |= {cls for cls, _, _ in out.instance_writes}
    for record in passes:
        what, held = record[1], record[1].startswith(HELD_WHAT)
        if held:
            what = what.partition("|")[2]
        elif record[4] == RECEIVER:
            continue
        if what.startswith("var:"):
            classes_written.add(what[len("var:") :])
        elif what.startswith("instance:"):
            classes_written.add(what.rpartition(":")[2])
    returned: dict[str, set[str]] = defaultdict(set)
    for function, root in out.state_returns:
        returned[function].add(root)
    stack = sorted({callee for _, callee, _ in out.result_writes})
    stack += [first for *_, kind, first, _ in out.held_binds if kind == "result"]
    seen_results: set[str] = set()
    written_returns: set[str] = set()
    while stack:  # what a call whose result is written in place returns
        function = stack.pop()
        if function in seen_results:
            continue
        seen_results.add(function)
        for root in returned.get(function, ()):
            if root.startswith("<result>:"):
                stack.append(root[len("<result>:") :])
            else:
                written_returns.add(root)
    for root in sorted(written_returns):
        if root.startswith("<attr>:"):
            attr, _, cls = root[len("<attr>:") :].partition(":")
            tables_written.add((cls, attr))
        elif root.startswith(("<fresh>:", "<instance>")):
            classes_written.add(root.rpartition(":")[2])
        else:
            classes_written.add(root)
    table_cache: dict[tuple[str, str], bool] = {}

    def literal_table(cls: str, attr: str) -> bool:
        """Whether class ``cls``'s attribute ``attr`` is a literal table
        (``ClassScope.tables``) nothing may change or bind anew: every class
        of its MRO kept and in scope, no class of its family written as a
        whole, storing the attribute or changing it in place, or customising
        attribute reads."""
        key = (cls, attr)
        if key not in table_cache:
            cscope = indexer.class_scopes.get(cls)
            found = cscope is not None and attr in cscope.tables
            if found:
                for c in indexer._mro(cls):
                    base = indexer.class_scopes.get(c)
                    if base is None or not base.kept or base.external_base:
                        found = False
            if found:
                family = {
                    c for d in (cls, *indexer._descendants.get(cls, ())) for c in indexer._mro(d)
                }
                for c in sorted(family):
                    member = indexer.class_scopes[c]
                    if (
                        c in classes_written
                        or (c, attr) in tables_written
                        or (c, "*") in tables_written
                        or "__getattribute__" in member.members.keys() | member.bindings
                    ):
                        found = False
            table_cache[key] = found
        return table_cache[key]

    attribute_cache: dict[tuple[str, str], list[str] | None] = {}

    def attribute_values(cls: str, attr: str) -> list[str] | None:
        """What ``self.<attr>`` may hold in a method of ``cls``, as the
        in-scope classes of the instances and ``external`` for a third-party
        value; None when it is not known (audit round 3, W21). Known when
        every write of ``self.<attr>``, in any method of any class (so a
        subclass the index cannot place counts too), binds a construction
        of a plain in-scope class with no in-scope ``__new__`` in its MRO, a
        call into a module outside the source roots, or a call of a project
        factory whose every return is such a call; and nothing writes the
        attribute otherwise (through another receiver, ``setattr``,
        ``__dict__``), binds it at class level, or customises attribute
        access in the class's family."""
        key = (cls, attr)
        if key not in attribute_cache:
            attribute_cache[key] = _attribute_values(indexer, cls, attr)
        return attribute_cache[key]

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

    # How each written parameter is written (``param_flavors``): a parameter
    # handed itself on is written as the callee writes its own, one handed
    # on in part (an item, an attribute) in place (``*``; T23).
    flavors: dict[tuple[str, str], set[str]] = defaultdict(set)
    for function, param, how in out.param_flavors:
        flavors[(function, param)].add(how)
    whole = out.whole_passes

    def written_flavors(record: Pass) -> set[str] | None:
        """How the callees of ``record`` write what it hands them; None when
        none writes it."""
        _, _, _, _, slot, bound = record
        found: set[str] | None = None
        for callee in callees(record):
            param = slot_param(callee, slot, bound)
            if param is not None and (callee, param) in writes:
                found = (found or set()) | (flavors.get((callee, param)) or {"*"})
        return found

    param_passes = [p for p in passes if p[1].startswith("param:")]
    changed = True
    while changed:
        changed = False
        for record in param_passes:
            key = (record[0], record[1][len("param:") :])
            if key in writes and "*" in flavors[key]:
                continue  # written every way already
            how = written_flavors(record)
            if how is None:
                continue
            if key not in writes:
                writes.add(key)
                changed = True
            if record not in whole:
                how = {"*"}
            if not how <= flavors[key]:
                flavors[key] |= how
                changed = True

    def resolved_callees(record: Pass) -> list[str]:
        """``callees`` without the name-only fallback: a method of a
        receiver the index cannot type is not taken for every method of
        that name (which on a real project makes every write a store).
        A value of a known class, or a third-party value's project subclass
        methods, resolve as in ``callees``."""
        _, _, callee, name, _, _ = record
        if callee.startswith(HELD_BY):
            cls, _, attr = callee[len(HELD_BY) :].rpartition(".")
            return [] if attribute_values(cls, attr) is None else callees(record)
        return [callee] if callee else []

    # A method called on state stores into it when it writes its receiver
    # (``self._cache.set(k, v)``: the ``set`` of what ``_cache`` is known to
    # hold, or ``self.put(...)`` resolved through the class).
    for caller, where, callee, name in sorted(out.computed_passes):
        record = (caller, "param:self", callee, name, RECEIVER, True)
        for c in resolved_callees(record):
            param = slot_param(c, RECEIVER, True)
            if param is not None and (c, param) in writes:
                out.stores.add((caller, where))
                break
    # A parameter's attribute handed to a callee that writes the parameter
    # it lands in: a store into that container is read through the
    # attribute (SourceIndex.handed).
    for caller, callee, name, slot, held, bound in sorted(out.held_passes):
        record = (caller, "param:self", callee, name, slot, bound)
        for c in resolved_callees(record):
            param = slot_param(c, slot, bound)
            if param is not None and (c, param) in writes:
                out.handed.add((caller, c, held))
    module_variables: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols.values():
        if symbol.kind == SymbolKind.VARIABLE:
            module_variables[symbol.module].append(symbol.id)
    safe = _HeldSafety(indexer)

    def handed_written(caller: str, what: str, receiver: bool) -> None:
        """What a written parameter was handed is changed: an instance of an
        in-scope class (or part of one), what a class attribute holds, a
        module variable or class."""
        if what.startswith("instance:"):
            # An instance of an in-scope class (or part of one) handed to a
            # written parameter: a write of the state its class holds, if
            # any (Indexer._value_writes; audit round 6, T22).
            how, _, cls = what[len("instance:") :].partition(":")
            symbol = symbols.get(cls)
            if symbol is not None and caller not in (cls, symbol.module):
                out.instance_writes.add((cls, caller, how))
            return
        if what.startswith("attr:"):
            # What a class attribute holds (Indexer._class_attribute_writes; T24).
            attr, _, cls = what[len("attr:") :].partition(":")
            symbol = symbols.get(cls)
            if symbol is not None and caller not in (cls, symbol.module):
                out.attr_value_writes.add((cls, attr, caller))
            return
        if not what.startswith("var:"):
            return
        variable = what[len("var:") :]
        symbol = symbols.get(variable)
        if symbol is None or caller in (variable, symbol.module):
            # A module's own top-level statements naming the variable are
            # part of its hash already (a class among them may have an
            # attribute bound anew: Indexer._class_attribute_writes).
            if symbol is not None and symbol.kind == SymbolKind.CLASS and caller != variable:
                out.own_class_writes.add((variable, "*"))
            return
        # What the variable holds is written: no edge if it cannot be
        # (Indexer._value_writes). As the receiver of a method that writes
        # ``self``, what the method writes of state its class holds is an
        # edge of the class already (audit round 6, T21).
        if receiver:
            out.receiver_writes.add((variable, caller))
        else:
            out.value_writes.add((variable, caller))

    for record in passes:
        caller, what = record[0], record[1]
        if what.startswith(HELD_WHAT):
            # What a fresh instance holds of its constructor's arguments:
            # written unless the callee only binds attributes of the instance
            # (that run no code of its class) or augments immutable ones (T23).
            cls, _, inner = what[len(HELD_WHAT) :].partition("|")
            how = written_flavors(record)
            if how is not None and safe.writes(cls, how):
                handed_written(caller, inner, False)
            continue
        if what.startswith("module:"):
            # A module handed to a written parameter: any of its variables
            # (and its submodules') may be what the callee writes.
            module = what[len("module:") :]
            if writes_through(record):
                for m in sorted(module_variables):
                    if m == module or m.startswith(module + "."):
                        for variable in module_variables[m]:
                            if caller not in (variable, m):
                                out.edges.add(
                                    Edge(variable, caller, EdgeKind.REFERENCES, MUTATED_BY)
                                )
            continue
        if writes_through(record):
            handed_written(caller, what, record[4] == RECEIVER)
    _apply_held_binds(indexer, safe)


def _apply_held_binds(indexer: Resolver, safe: _HeldSafety) -> None:
    """An attribute bound on a fresh instance: what its constructor was
    handed changes only when binding runs code of the class (T23). Decided
    whether or not any function writes a parameter."""
    out = indexer.out
    for cls, attr, writer, kind, first, second in sorted(out.held_binds):
        if safe.writes(cls, {f"={attr}"}):
            apply_fact(out, writer, (kind, first, second))


class _HeldSafety:
    """Whether writes into a fresh instance of an in-scope class change
    nothing its constructor was handed (audit round 6, T23): the instance
    is a new object (no class of its family defines ``__new__``, has a
    decorator other than an inert one, a metaclass other than
    ``abc.ABCMeta`` (``ClassScope.kept``) or a base outside the source roots
    that may give back something else), and every write only binds an attribute of
    the instance itself (``=<attr>``), other than a special one
    (``__dict__``, ``__class__``), that runs no code of the class (no
    ``__setattr__``/``__delattr__``, no class-level member or binding of
    that name, which may be a descriptor, other than an immutable value),
    or augments one (``+<attr>``) whose every value is of a builtin type
    that cannot change in place (no store of the attribute, on any
    receiver of unknown type or an instance of the family, binds anything
    else, ``attr_values``). A change in place of the instance (``*``) may
    run its methods, which may change anything it holds."""

    def __init__(self, indexer: Resolver) -> None:
        self.indexer = indexer
        self.values: dict[str, set[tuple[str, bool]]] = defaultdict(set)
        for owner, attr, ok in indexer.out.attr_values:
            self.values[attr].add((owner, ok))
        self._family: dict[str, set[str] | None] = {}

    def family(self, cls: str) -> set[str] | None:
        """The family of ``cls`` (its MRO and its in-scope subclasses'), or
        None when one of them may make or customise the instance."""
        if cls not in self._family:
            indexer = self.indexer
            found: set[str] | None = set()
            for c in (cls, *indexer._descendants.get(cls, ())):
                found.update(indexer._mro(c))
            for c in sorted(found):
                cscope = indexer.class_scopes.get(c)
                names = cscope.members.keys() | cscope.bindings if cscope is not None else set()
                if (
                    cscope is None
                    or not cscope.kept
                    or cscope.external_base
                    or names & _INSTANCE_HOOKS
                ):
                    found = None
                    break
            self._family[cls] = found
        return self._family[cls]

    def writes(self, cls: str, how: set[str]) -> bool:
        """Whether writes ``how`` (``param_flavors``) into a fresh instance
        of ``cls`` may change what its constructor was handed."""
        if "*" in how:
            return True
        family = self.family(cls)
        if family is None:
            return True
        class_scopes = self.indexer.class_scopes
        for flavor in sorted(how):
            attr = flavor[1:]
            if attr == "*" or (attr.startswith("__") and attr.endswith("__")):
                # A computed name, or a special attribute: ``__dict__`` bound
                # to what it was handed makes every later binding a write
                # into it, ``__class__`` may give it any class's hooks.
                return True
            for c in family:
                cscope = class_scopes[c]
                if attr in cscope.members or attr in _class_values(cscope):
                    return True
            if flavor.startswith("+"):
                # What the attribute may hold: a store of it, or under any
                # name, binding a value that may change in place, or a
                # class-level value (a descriptor's ``__get__`` may store into
                # the instance's ``__dict__``).
                owners = family | {""}
                for name in (attr, "*"):
                    if any(not ok and owner in owners for owner, ok in self.values.get(name, ())):
                        return True
                if any(_class_values(class_scopes[c]) for c in family):
                    return True
        return False


def _class_values(cscope: ClassScope) -> set[str]:
    """The names a class body binds to a value that may be a descriptor or
    change in place: not a function, an only annotated name or an immutable
    value (``_HeldSafety``)."""
    return cscope.bindings - cscope.declared - (cscope.immutables or {}).keys()


# Special methods that make an instance or run on an attribute bound on it,
# or read one (``__getattribute__``, ``__getattr__``: an augmented attribute).
_INSTANCE_HOOKS = frozenset(
    {"__new__", "__setattr__", "__delattr__", "__getattribute__", "__getattr__"}
)


def _attribute_values(indexer: Resolver, cls: str, attr: str) -> list[str] | None:
    """See ``apply_writes.attribute_values``. The instance may be one of an
    in-scope subclass, or of a class whose base the index cannot resolve
    (which may be a subclass): what their methods write counts too."""
    out = indexer.out
    family: set[str] = set()
    for cid in (cls, *indexer._descendants.get(cls, ())):
        family.update(indexer._mro(cid))
    holders = family | {c for c, scope in indexer.class_scopes.items() if not scope.complete}
    for owner, written in out.attr_unbound:
        if written in (attr, "*") and (owner == "" or owner in holders):
            return None
    for cid in holders:
        cscope = indexer.class_scopes.get(cid)
        if cscope is None or _ATTRIBUTE_HOOKS & cscope.members.keys():
            return None
        if attr in cscope.members or attr in cscope.bindings:
            return None
    found: set[str] = set()
    writes = [w for w in out.attr_writes if w.attr == attr and w.cls in holders]
    if not writes:
        return None
    for w in writes:
        binding = w.binding
        if not binding:
            return None
        if binding[0] == "external":
            found.add("external")
        elif binding[0] == "factory" and binding[1] in out.external_returns:
            found.add("external")
        elif binding[0] == "instance" and _plainly_built(indexer, binding[1]):
            found.add(binding[1])
        else:
            return None
    return sorted(found)


def _plainly_built(indexer: Resolver, cls: str) -> bool:
    """Whether calling in-scope class ``cls`` makes an instance of it: no
    decorator or metaclass in its MRO, and no in-scope ``__new__``."""
    if cls not in indexer.class_scopes:
        return False
    for cid in indexer._mro(cls):
        cscope = indexer.class_scopes.get(cid)
        if cscope is None or not cscope.plain:
            return False
        if "__new__" in cscope.members:
            return False
    return True

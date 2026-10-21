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
to a written parameter gets a ``mutated_by`` edge to the caller and to each
callee writing it (``_writers``: the callee is project code even when only
a test calls it, which the isolation assumption drops as a writer; audit
round 7, U4), unless its value cannot change in place
(Indexer._value_writes), and so does
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

After pass 2, ``ClassRecord`` gathers every attribute each class is given
anywhere (its body, its module's top-level code, ``class_binds``, other
modules, decorators and metaclasses as ``*``, a computed ``setattr``,
method binds and shares) and is what every class-state rule reads (audit
round 7, U3); ``variable_classes`` says which classes a module variable may
hold an instance of or be (U2); and a method a library gives a class,
called on the class itself, writes it (``library_calls``,
``_apply_library_calls``, U12).

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
= v``: ``cache``; ``getattr(self, "_cache")[k] = v`` too), ``@self`` for
the receiver itself (an item stored into it, a container mutator called
on it, a method call on it that writes it: ``self.put(k, v)``), ``@self.x``
for an attribute bound or deleted on it (``self.x = v``,
``setattr(self, "x", v)``, ``self.__dict__["x"] = v``, ``vars(self)["x"] =
v``; ``@self.*`` for a name known only at run time, and for an item or
attribute stored into what a method of the receiver returns, when one of
the methods it may be may return the receiver's state: ``self._table()[k]
= v`` with ``_table`` returning ``self._data``, closed after pass 2 over
``result_stores`` and ``receiver_returns``; audit round 7, U4, U15),
``@init`` for an explicit ``self.__init__()``, ``Base.__init__(self)`` or
``super().__init__()``, ``@param`` for a parameter container (``d[k] =
v``) and ``@closure`` for a variable of an enclosing function; a ``del`` of
an item or attribute is a store too. Stores through a method on an attribute or the receiver
(``self._cache.set(k, v)``) count when the method writes its receiver
(``computed_passes``, resolved in ``apply_writes``). ``handed``:
(caller, callee, attribute) for a parameter's attribute handed to a
callee, so a store into a parameter container is read through the
attribute the caller handed.

Receivers of a known class (evidence mode only; audit round 7, U5, U18): a
method called on a receiver pass 2 cannot type, or an attribute bound or
deleted on one, is recorded with what the receiver may be
(``receiver_calls``: the instance-binding origins of
``_ReferenceCollector._held_origins``), and resolved after pass 2 through
the classes the whole index knows for it (Indexer._typed_calls).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from typing import TYPE_CHECKING

from diffcone.indexer.definitions import _ATTRIBUTE_HOOKS
from diffcone.indexer.facts import _Output, apply_fact
from diffcone.indexer.scopes import ClassScope, External, ModuleNode, Resolved, Scope
from diffcone.indexer.syntax import BUILTIN_NAMES, _flatten_chain
from diffcone.indexer.uses import CONTAINER_MUTATORS
from diffcone.model import (
    FRESH_INITS,
    MUTATED_BY,
    STORE_ANY_ATTRIBUTE,
    STORE_CLOSURE,
    STORE_PARAM,
    STORE_REINIT,
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
        "type.__setattr__",  # a class's own attribute (U2)
        "type.__delattr__",
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
# A pass record's ``anyclass:<attribute>``: the class of an object of unknown
# type, or what it holds under that attribute (references.ANY_CLASS; audit
# round 6, T25).
ANY_CLASS_WHAT = "anyclass:"

# A pass record: (caller, what is handed on, callee or "", called name,
# slot, receiver bound). What is handed on is ``param:<name>``,
# ``var:<symbol id>``, ``module:<name>`` or ``instance:[.<attribute>]:<class
# id>`` (an instance of an in-scope class the caller made, or part of one,
# reached through that attribute: audit round 6, T22), ``attr:...``,
# ``held:...`` or ``anyclass:[<attribute>]`` (ANY_CLASS_WHAT); the slot is a
# positional index, ``=<keyword>`` or RECEIVER.
Pass = tuple[str, str, str, str, str, bool]

# A store into some attribute of the receiver not known where it is made.
ANY_SELF_ATTRIBUTE = f"{STORE_SELF}.{STORE_ANY_ATTRIBUTE}"
# Methods whose result, on an object of unknown type, is a new object (a
# copy, the parts of a string) or a view nothing stores into: a store into
# it is none into the object (``_state_of``).
FRESH_RESULTS = frozenset(
    {
        "copy",
        "__copy__",
        "__deepcopy__",
        "split",
        "rsplit",
        "splitlines",
        "partition",
        "rpartition",
        "keys",
        "values",
        "items",
    }
)


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


def _calls_builtin(
    collector: _ReferenceCollector, expr: ast.expr, name: str, args: int
) -> ast.Call | None:
    """``expr`` when it calls the builtin ``name`` (nothing of the project
    binds it) with ``args`` positional arguments (or more, for ``getattr``'s
    default) and no keywords."""
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == name
        and (len(expr.args) == args or (name == "getattr" and len(expr.args) == 3))
        and not any(isinstance(a, ast.Starred) for a in expr.args)
        and not expr.keywords
        and not collector._is_shadowed(name)
    ):
        return expr
    return None


def _literal_getattr(collector: _ReferenceCollector, expr: ast.expr) -> str | None:
    """The attribute ``getattr(obj, "name")`` (or with a default) reads, when
    one literal names it."""
    call = _calls_builtin(collector, expr, "getattr", 2)
    if call is None:
        return None
    names = collector.scope.string_candidates(call.args[1])
    return names[0] if names is not None and len(names) == 1 else None


def _call_receiver(collector: _ReferenceCollector, expr: ast.Call) -> ast.expr | None:
    """The object a call is made on, whose state what it returns may be
    part of: ``self`` of ``self._table()`` or of ``getattr(self, name)``."""
    if isinstance(expr.func, ast.Attribute):
        return expr.func.value
    call = _calls_builtin(collector, expr, "getattr", 2)
    return call.args[0] if call is not None else None


def _chain_root(collector: _ReferenceCollector, expr: ast.expr) -> ast.Name | None:
    """The name an item, attribute or method-result chain starts from (``p``
    of ``p.items[0]``, of ``p._table()[k]``, of ``getattr(p, "x")``)."""
    for _ in range(64):
        if isinstance(expr, (ast.Subscript, ast.Attribute)):
            expr = expr.value
        elif isinstance(expr, ast.Call) and (inner := _call_receiver(collector, expr)) is not None:
            expr = inner
        elif (call := _calls_builtin(collector, expr, "type", 1)) is not None or (
            call := _calls_builtin(collector, expr, "vars", 1)
        ) is not None:
            expr = call.args[0]
        else:
            break
    return expr if isinstance(expr, ast.Name) else None


def _state_of(
    collector: _ReferenceCollector, expr: ast.expr, calls: list[str] | None = None
) -> str | None:
    """Where ``expr``, the target of a store or the receiver of a call,
    lives when that outlives the call: the attribute written through a
    parameter (``self`` and ``cls`` included), a function or a class
    (``self._cache``, ``p.items``, ``get.cache``, ``Store._cache``,
    ``getattr(self, "_cache")``), ``STORE_SELF`` for the receiver itself,
    ``STORE_PARAM`` for another parameter itself and ``STORE_CLOSURE`` for a
    variable of an enclosing function; None for a local, or a module
    variable (a ``mutated_by`` edge covers it). What a call made on one of
    those returns (``self._table()``, ``getattr(self, name)``, a local bound
    to it) may be part of it: for the receiver, ``STORE_SELF`` + ``.*``
    (under an attribute not known here; audit round 7, U15). A method
    called on the receiver itself (``self._table()``) is added to
    ``calls``: whether what it returns may be the receiver's state is known
    after pass 2 (``receiver_returns``); without ``calls``, such a result
    is taken for a fresh object."""
    attrs: list[str] = []
    base: ast.expr = expr
    while True:
        if isinstance(base, (ast.Subscript, ast.Attribute)):
            if isinstance(base, ast.Attribute):
                attrs.append(base.attr)
            base = base.value
        elif (call := _calls_builtin(collector, base, "type", 1)) is not None:
            attrs.append("__class__")  # ``type(p).x = v`` as ``p.__class__.x = v``
            base = call.args[0]
        elif (named := _literal_getattr(collector, base)) is not None:
            assert isinstance(base, ast.Call)
            attrs.append(named)  # ``getattr(self, "_data")`` as ``self._data``
            base = base.args[0]
        else:
            break
    if attrs and attrs[-1] == "__dict__":
        attrs.pop()  # ``self.__dict__[k] = v`` stores into ``self`` itself
    if not attrs and (call := _calls_builtin(collector, base, "vars", 1)) is not None:
        return _state_of(collector, call.args[0], calls)  # ``vars(self)[k] = v``
    if isinstance(base, ast.Call):
        # What a call on state outliving the call returns may be part of
        # that state (``self._table()[k] = v`` where ``_table`` returns
        # ``self._data``), under some attribute of it.
        receiver = _call_receiver(collector, base)
        if receiver is None:
            return None
        if (
            isinstance(base.func, ast.Attribute)
            and isinstance(receiver, ast.Name)
            and receiver.id == collector.scope.self_name
            and _own_param(collector, receiver.id) is not None
        ):
            if calls is None:
                return None
            calls.append(base.func.attr)
            return ANY_SELF_ATTRIBUTE
        if isinstance(base.func, ast.Attribute) and base.func.attr in FRESH_RESULTS:
            return None  # ``self._d.copy()[k] = v`` stores into a new object
        found = _state_of(collector, receiver, calls)
        return ANY_SELF_ATTRIBUTE if found == STORE_SELF else found
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
            inner = _chain_root(collector, source)
            if inner is not None and inner is not base and inner.id != base.id:
                found = _state_of(collector, source, calls)
                if found is None or attr == "":
                    return found
                return ANY_SELF_ATTRIBUTE if found == ANY_SELF_ATTRIBUTE else attr
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


def _store(
    collector: _ReferenceCollector,
    target: ast.expr,
    attr: str | None = None,
    *,
    reinit: bool = False,
) -> None:
    """``target`` (an item or attribute, or the receiver of a mutating call)
    is written: a store into state outliving the call, whatever the value
    (``SourceIndex.stores``). ``attr``: the attribute bound or deleted on
    ``target`` itself (``self.x = v``: ``x``, ``*`` for a name known only at
    run time), kept for a store into the receiver (``STORE_SELF`` +
    ``.x``); ``reinit``: an explicit re-initialisation (``STORE_REINIT``).
    A store through what a method of the receiver returns waits for pass 2
    to end (``result_stores``: whether it may return the receiver's state)."""
    calls: list[str] = []
    where = _state_of(collector, target, calls)
    if where is None:
        return
    if where == STORE_SELF:
        if reinit:
            where = STORE_REINIT
        elif attr is not None:
            where = f"{STORE_SELF}.{attr}"
    out = collector.indexer.out
    if calls:
        out.result_stores.add((collector.source, where, ",".join(calls)))
    else:
        out.stores.add((collector.source, where))


def _bound_attr(collector: _ReferenceCollector, target: ast.expr) -> str | None:
    """The attribute an assignment target binds on the object it is taken
    from: ``x`` of ``o.x``, of ``o.__dict__["x"]`` and of ``vars(o)["x"]``
    (``*`` for a key known only at run time); None for an item."""
    if isinstance(target, ast.Attribute):
        return target.attr
    if not isinstance(target, ast.Subscript):
        return None
    view = target.value
    if not (
        (isinstance(view, ast.Attribute) and view.attr == "__dict__")
        or _calls_builtin(collector, view, "vars", 1) is not None
    ):
        return None
    names = collector.scope.string_candidates(target.slice)
    return names[0] if names is not None and len(names) == 1 else STORE_ANY_ATTRIBUTE


def observe_receiver(
    collector: _ReferenceCollector, receiver: ast.expr, attr: str, how: str
) -> None:
    """``receiver.attr(...)`` called (``how`` ``call``), bound (``set``) or
    deleted (``del``) where the receiver may be an instance of an in-scope
    class the index knows (``ENGINE.run(k)`` with ``ENGINE = Engine()``,
    ``Engine().run(k)``, a local bound to either): the method, property
    accessor or attribute hook that runs is resolved through those classes
    after pass 2 (``receiver_calls`` -> ``SourceIndex.typed_calls``;
    evidence planning only, audit round 7, U5, U18), and an attribute of the
    receiver holding one (``self.store.put(k, v)``)."""
    origins = collector._held_origins(receiver, 0)
    scope = collector.scope
    if (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == scope.self_name
        and not scope.self_is_class
        and scope.self_class is not None
        and _own_param(collector, receiver.value.id) is not None
    ):
        # ``self.store.put(k, v)``: what the attribute holds when every
        # write of it says (``_attribute_values``, W21).
        origins.add(f"{HELD_BY}{scope.self_class}.{receiver.attr}")
    for origin in sorted(origins):
        collector.indexer.out.receiver_calls.add((collector.source, origin, attr, how))


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
                _store(collector, sub.value, _bound_attr(collector, sub))
                if isinstance(sub, ast.Attribute):
                    kind = "del" if isinstance(sub.ctx, ast.Del) else "set"
                    observe_receiver(collector, sub.value, sub.attr, kind)


def observe_return(collector: _ReferenceCollector, value: ast.expr) -> None:
    """A method returning its receiver, or what an item, attribute or
    method-result chain from it reaches (``return self._data``, ``return
    self``, ``return self._get().items``): what a caller stores into the
    result may be the receiver's state (``receiver_returns``, with the
    methods of the receiver the chain calls, which must each return such
    state too; ``result_stores``)."""
    scope = collector.scope
    if scope.self_name is None or scope.literal_parent is not None:
        return
    calls: list[str] = []
    if _state_of(collector, value, calls) is None:
        return
    node: ast.expr | None = value
    for _ in range(16):  # through locals bound once to such a chain
        root = _chain_root(collector, node) if node is not None else None
        if root is None:
            return
        if root.id == scope.self_name and _own_param(collector, root.id) is not None:
            collector.indexer.out.receiver_returns.add((collector.source, ",".join(calls)))
            return
        if root.id not in scope.locals or _own_param(collector, root.id) is not None:
            return
        node = collector._local_source(root.id)


def observe_augassign(collector: _ReferenceCollector, node: ast.AugAssign) -> None:
    """``p += [x]`` changes a list or dict parameter in place; so does an
    augmented item or attribute."""
    target = node.target
    receiver = target.value if isinstance(target, (ast.Subscript, ast.Attribute)) else target
    how = "*"
    if isinstance(target, ast.Attribute) and collector._immutable_typed(node.value, target):
        how = f"+{target.attr}"  # ``p.n += 1``: in place only if ``n`` may be mutable
    _write(collector, receiver, how)
    _store(collector, receiver, _bound_attr(collector, target))
    if isinstance(target, ast.Attribute):
        observe_receiver(collector, target.value, target.attr, "set")


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
        named: str | None = None
        if parts[-1] in ("setattr", "delattr", "__setattr__", "__delattr__"):
            names = collector.scope.string_candidates(node.args[1]) if len(node.args) > 1 else None
            named = names[0] if names is not None and len(names) == 1 else STORE_ANY_ATTRIBUTE
            how = f"={named}"
        _write(collector, first, how)
        _dict_receiver(collector, first)
        if not isinstance(first, ast.Starred):
            collector._mutation_target(first)  # a module variable: ``heappush(QUEUE, x)``
            _store(collector, first, named)
            if named is not None and named != STORE_ANY_ATTRIBUTE:
                kind = "del" if "del" in parts[-1] else "set"
                observe_receiver(collector, first, named, kind)
    if isinstance(func, ast.Attribute):
        if func.attr in CONTAINER_MUTATORS or func.attr in ("__setattr__", "__delattr__"):
            how = "=*" if func.attr in ("__setattr__", "__delattr__") else "*"
            _write(collector, func.value, how)
            _dict_receiver(collector, func.value)
            view = func.value
            whole = func.attr in ("__setattr__", "__delattr__") or (
                (isinstance(view, ast.Attribute) and view.attr == "__dict__")
                or _calls_builtin(collector, view, "vars", 1) is not None
            )  # ``self.__dict__.update(d)`` binds attributes on ``self``
            _store(collector, view, STORE_ANY_ATTRIBUTE if whole else None)
        else:
            _computed_pass(collector, node, parts)
        if func.attr in FRESH_INITS:
            _reinitialised(collector, node, func)
        if not _resolves_to_code(collector, parts):
            observe_receiver(collector, func.value, func.attr, "call")
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
        _store(collector, target, reinit=True)


def _resolves_to_code(collector: _ReferenceCollector, parts: list[str] | None) -> bool:
    """Whether a called chain resolves to a function, method or class of
    the index (or to something outside the source roots), which the
    reference edges already name."""
    if parts is None:
        return False
    node = collector.indexer.resolve_chain(parts, collector.scope)
    if isinstance(node, External):
        return True
    if not isinstance(node, Resolved) or node.detail:
        return False
    symbol = collector.indexer.index.symbols.get(node.symbol)
    return symbol is not None and symbol.kind in (
        SymbolKind.FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.CLASS,
    )


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


def apply_writes(indexer: Resolver) -> ClassRecord:
    """Close parameter writes over the calls that hand parameters on, then
    give each variable handed to a written parameter a ``mutated_by`` edge
    to the caller (see the module docstring); and keep each store through
    what a method of the receiver returns that may be the receiver's state
    (``_resolve_result_stores``). Gives back the record of what each class
    is given (ClassRecord), which the decisions after it read."""
    record = _apply_writes(indexer)
    _resolve_result_stores(indexer)
    return record


def _resolve_result_stores(indexer: Resolver) -> None:
    """``result_stores`` -> ``stores``: a store through what methods of the
    receiver return counts when, for each method the chain calls, one of
    the methods it may be (the hit in the caller's class's MRO, and the
    overrides in its subclasses) may return the receiver's state, or none
    is in scope (a method of a base outside the source roots, or one the
    index cannot find, may). A method may return it when one of its
    ``receiver_returns`` does, whose own chain's methods do in turn, until
    nothing changes (audit round 7, U15: ``self._table()[k] = v`` where
    ``_table`` returns ``self._data``; ``out = self._fresh(); out[k] = v``
    is no store when ``_fresh`` returns a new list)."""
    out = indexer.out
    if not out.result_stores:
        return
    symbols = indexer.index.symbols
    dispatched: dict[tuple[str, str], tuple[str, ...] | None] = {}

    def methods(caller: str, name: str) -> tuple[str, ...] | None:
        """What ``self.<name>`` may be in ``caller``; None when not known."""
        symbol = symbols.get(caller)
        cls = symbol.container if symbol is not None else None
        if cls is None or cls not in indexer.class_scopes:
            return None
        key = (cls, name)
        if key not in dispatched:
            hit = indexer.lookup_in_class(cls, name, dispatch=True)
            found: tuple[str, ...] | None = None
            if isinstance(hit, Resolved) and not hit.detail and not hit.uncertain_attr:
                found = tuple(
                    sorted({hit.symbol, *(o for o, detail in hit.overrides if not detail)})
                )
            dispatched[key] = found
        return dispatched[key]

    returning: set[str] = set()
    pending = sorted(out.receiver_returns)

    def holds(caller: str, chain: str) -> bool:
        for name in chain.split(",") if chain else ():
            found = methods(caller, name)
            if found is not None and not any(m in returning for m in found):
                return False
        return True

    changed = True
    while changed:
        changed = False
        for method, chain in pending:
            if method not in returning and holds(method, chain):
                returning.add(method)
                changed = True
    for caller, where, chain in sorted(out.result_stores):
        if holds(caller, chain):
            out.stores.add((caller, where))


def _apply_writes(indexer: Resolver) -> ClassRecord:
    out = indexer.out
    params = out.func_params
    _apply_library_calls(indexer)
    class_record = ClassRecord(indexer)
    # Writes onto a class of unknown type are writes of the classes that may
    # own the attribute, before anything asks which classes are written (T25).
    owners = class_record.owners
    _record_any_class_writes(out, owners)
    writes: set[tuple[str, str]] = set(out.param_writes)
    if not writes:
        _apply_held_binds(indexer, _HeldSafety(indexer))
        _record_any_class_writes(out, owners)
        return class_record
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
    tables_written = {(c, a) for c, a, _ in out.attr_value_writes}
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
    classes_written = class_record.written()
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
            classes_written |= class_record.classes(what[len("var:") :])
        elif what.startswith("instance:"):
            classes_written.add(what.rpartition(":")[2])
        elif what.startswith(ANY_CLASS_WHAT):
            for name in owners.names(what, None):
                classes_written.update(owners.owners(name))
    returned: dict[str, set[str]] = defaultdict(set)
    for function, root in out.state_returns:
        returned[function].add(root)
    stack = sorted({callee for _, callee, _ in out.result_writes | out.direct_result_writes})
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
            classes_written |= class_record.instances.get(root.rpartition(":")[2], set())
        elif root.startswith("<anyclass>:"):
            classes_written.update(owners.owners(root[len("<anyclass>:") :] or "*"))
        else:
            classes_written.add(root)
            classes_written |= class_record.classes(root)
    table_cache: dict[tuple[str, str], bool] = {}

    def literal_table(cls: str, attr: str) -> bool:
        """Whether class ``cls``'s attribute ``attr`` is a literal table
        (``ClassScope.tables``) nothing may change or bind anew: every class
        of its MRO kept and in scope, no class of its family written as a
        whole, given the attribute outside its body (ClassRecord), changing
        it in place, or customising attribute reads."""
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
                        or attr in class_record.given(c)
                        or "*" in class_record.given(c)
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

    def writing_callees(record: Pass) -> list[str]:
        """The callees of ``record`` that write what it hands them."""
        _, _, _, _, slot, bound = record
        found: list[str] = []
        for callee in callees(record):
            param = slot_param(callee, slot, bound)
            if param is not None and (callee, param) in writes:
                found.append(callee)
        return found

    def writes_through(record: Pass) -> bool:
        return bool(writing_callees(record))

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

    def handed_written(
        caller: str, what: str, receiver: bool, flavors: set[str] | None = None
    ) -> None:
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
        if what.startswith(ANY_CLASS_WHAT):
            # The class of an object of unknown type (``type(o)``) handed to
            # a written parameter: a write of each class that may own what
            # the callee binds on it (T25).
            for name in owners.names(what, flavors):
                owners.record(out, name, caller, "")
            return
        if not what.startswith("var:"):
            return
        variable = what[len("var:") :]
        symbol = symbols.get(variable)
        if symbol is None or caller in (variable, symbol.module):
            # A module's own top-level statements naming the variable are
            # part of its hash already (a class among them may have an
            # attribute bound anew: Indexer._class_attribute_writes).
            # A class among them, or held by one, may be given an attribute
            # (Indexer._class_attribute_writes, ClassRecord).
            if symbol is not None and symbol.kind in (SymbolKind.CLASS, SymbolKind.VARIABLE):
                if caller != variable:
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
        if not receiver:
            # A class the variable holds, or the class itself, is given what
            # the callee binds on it (``configure(KINDS["c"])``; U2, U3).
            if symbol.kind == SymbolKind.VARIABLE:
                out.typed_writes.add((variable, caller))
            for name in owners.names(ANY_CLASS_WHAT, flavors):
                out.class_binds.add((variable, name))

    for record in passes:
        caller, what = record[0], record[1]
        if what.startswith(HELD_WHAT):
            # What a fresh instance holds of its constructor's arguments:
            # written unless the callee only binds attributes of the instance
            # (that run no code of its class) or augments immutable ones (T23).
            cls, _, inner = what[len(HELD_WHAT) :].partition("|")
            how = written_flavors(record)
            if how is not None and safe.writes(cls, how):
                for writer in _writers(caller, writing_callees(record)):
                    handed_written(writer, inner, False, how)
            continue
        if what.startswith("module:"):
            # A module handed to a written parameter: any of its variables
            # (and its submodules') may be what the callee writes.
            module = what[len("module:") :]
            for writer in _writers(caller, writing_callees(record)):
                for m in sorted(module_variables):
                    if m == module or m.startswith(module + "."):
                        for variable in module_variables[m]:
                            if writer not in (variable, m):
                                out.edges.add(
                                    Edge(variable, writer, EdgeKind.REFERENCES, MUTATED_BY)
                                )
            continue
        if what.startswith(ANY_CLASS_WHAT):
            how = written_flavors(record)
            if how is not None:
                for writer in _writers(caller, writing_callees(record)):
                    handed_written(writer, what, record[4] == RECEIVER, how)
            continue
        how = written_flavors(record)
        for writer in _writers(caller, writing_callees(record)):
            handed_written(writer, what, record[4] == RECEIVER, how)
    _apply_held_binds(indexer, safe)
    _record_any_class_writes(out, owners)
    return class_record


def _apply_library_calls(indexer: Resolver) -> None:
    """A method a library gives a class, called on the class itself
    (``library_calls``): a write of each in-scope class of its MRO when one
    of them has a base outside the index, a decorator or a metaclass,
    which may define it (audit round 7, U12)."""
    out, scopes = indexer.out, indexer.class_scopes
    decided: dict[str, bool] = {}
    for cls, writer, kind, first, second in sorted(out.library_calls):
        if cls not in decided:
            mro = [c for c in indexer._mro(cls) if c in scopes]
            decided[cls] = any(scopes[c].opaque or not scopes[c].plain for c in mro)
        if decided[cls]:
            apply_fact(out, writer, (kind, first, second))


def variable_classes(indexer: Resolver) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Module variable -> the in-scope classes it may hold an instance of,
    and -> those it may hold themselves (``Alias = Conf``, ``KINDS = {"c":
    Conf}``, ``ALL = (Conf,)``, what a call handed a class gives back; audit
    round 7, U2): what its bindings hold (``instance_bindings``: ``class:``
    an instance, its id a class's or a variable's called as one; ``type:``
    a class itself), through what the functions called return
    (``returns``, ``state_returns``), other variables, and what such an
    instance holds under an attribute a method binds to an instance or a
    factory's result (``attr_writes``). Computed over the whole index on
    every build from facts a cold and a warm build share (audit round 6,
    T21)."""
    out = indexer.out
    bindings: dict[str, set[str]] = defaultdict(set)
    for var, origin in out.instance_bindings:
        bindings[var].add(origin)
    if not bindings:
        return {}, {}
    returned: dict[str, set[str]] = defaultdict(set)
    for function, root in out.state_returns:
        returned[function].add(root)
    returns, symbols, scopes = out.returns, indexer.index.symbols, indexer.class_scopes
    held: dict[str, set[str]] = defaultdict(set)
    for w in out.attr_writes:
        if w.binding is not None and w.binding[0] == "instance":
            held[w.cls].add(f"class:{w.binding[1]}")
        elif w.binding is not None and w.binding[0] == "factory":
            held[w.cls].add(f"call:{w.binding[1]}")
    instances: dict[str, set[str]] = {}
    types: dict[str, set[str]] = {}
    for start in sorted(bindings):
        made: set[str] = set()
        kinds: set[str] = set()
        seen: set[tuple[str, bool]] = set()
        # (origin, whether what it gives is called: an instance of it)
        stack = [(f"var:{start}", False)]
        while stack:
            origin, called = stack.pop()
            if (origin, called) in seen:
                continue
            seen.add((origin, called))
            kind, _, ident = origin.partition(":")
            symbol = symbols.get(ident)
            if kind in ("class", "type") and symbol is not None:
                instance = called or kind == "class"
                if symbol.kind == SymbolKind.VARIABLE:
                    stack.append((f"var:{ident}", instance))
                elif ident in scopes and not instance:
                    kinds.add(ident)
                elif ident in scopes and ident not in made:
                    made.add(ident)
                    # What its instances hold under an attribute
                    # (``self.store = Store()``) is reached through them.
                    for c in indexer._mro(ident):
                        stack.extend((h, False) for h in held.get(c, ()))
            elif kind == "var":
                stack.extend((o, called) for o in bindings.get(ident, ()))
            elif kind == "call":
                made.update(c for c in returns.get(ident, ()) if c in scopes)
                for root in returned.get(ident, ()):
                    if root.startswith("<attr>:"):
                        # What a class attribute holds: any of the class's.
                        stack.append((f"class:{root.rpartition(':')[2]}", called))
                    elif root.startswith("<result>:"):
                        stack.append(("call:" + root[len("<result>:") :], called))
                    elif (returned_symbol := symbols.get(root)) is not None:
                        if returned_symbol.kind == SymbolKind.CLASS:
                            stack.append((f"type:{root}", called))
                        elif returned_symbol.kind == SymbolKind.VARIABLE:
                            stack.append((f"var:{root}", called))
        if made:
            instances[start] = made
        if kinds:
            types[start] = kinds
    return instances, types


class ClassRecord:
    """Every attribute each in-scope class is given anywhere, computed once
    and read by every rule that asks whether a class holds state, owns an
    attribute or may be an unknown object's class written under a name
    (Indexer._classes_holding_state, ``_own_attribute``,
    ``_allocated_fresh``, ``_class_attribute_writes``, ``literal_table``,
    AnyClassOwners; audit round 7, U3). Its sources: the class body
    (``members``, ``bindings``); its own module's top-level code
    (``own_class_writes``) and any other code (``class_binds``), on the
    class or on a module variable holding it (``variable_classes``); a
    decorator or metaclass that may give it names nothing bounds
    (``ClassScope.unbounded``: ``*``, any); ``setattr`` with a computed name
    (``*``); and what methods bind on ``self`` or ``cls`` (``attr_writes``,
    ``methods``) and bind there to an object that may be shared
    (``attr_shares``, ``shares``). Read live: what is decided after pass 2
    adds to it."""

    def __init__(self, indexer: Resolver) -> None:
        self._indexer = indexer
        out = indexer.out
        scopes = indexer.class_scopes
        self.instances, self.types = variable_classes(indexer)
        self.methods: dict[str, set[str]] = defaultdict(set)
        for w in out.attr_writes:
            if w.cls in scopes:
                self.methods[w.cls].add(w.attr)
        self.shares: dict[str, dict[str, set[tuple[str, str, str]]]] = {}
        for cls, attr, kind, first, second in out.attr_shares:
            if cls in scopes:
                self.shares.setdefault(cls, {}).setdefault(attr, set()).add((kind, first, second))
        self._unbounded = {cid for cid, cscope in scopes.items() if cscope.unbounded}
        # The classes nested in each (``class Outer: class Meta: ...``).
        self.nested: dict[str, list[str]] = defaultdict(list)
        for cid, cscope in sorted(scopes.items()):
            if cscope.enclosing is not None:
                self.nested[cscope.enclosing.id].append(cid)
        self._given: dict[str, set[str]] = {}
        self._sizes: tuple[int, int] | None = None
        self.owners = AnyClassOwners(indexer, self)

    def classes(self, ident: str) -> set[str]:
        """The in-scope classes ``ident`` is, or may hold itself (a module
        variable bound to a class)."""
        if ident in self._indexer.class_scopes:
            return {ident}
        return self.types.get(ident, set())

    def given(self, cls: str) -> set[str]:
        """The names given to ``cls`` outside its body (``*``: any)."""
        out = self._indexer.out
        sizes = (len(out.own_class_writes), len(out.class_binds))
        if sizes != self._sizes:
            self._sizes = sizes
            given: dict[str, set[str]] = defaultdict(set)
            for ident, name in out.own_class_writes | out.class_binds:
                for c in self.classes(ident):
                    given[c].add(name)
            for c in self._unbounded:
                given[c].add("*")
            self._given = given
        return self._given.get(cls, set())

    def class_level(self, cls: str, attr: str) -> bool:
        """Whether ``cls`` may have ``attr`` on the class itself: its body
        binds or defines it, or something gives it."""
        cscope = self._indexer.class_scopes[cls]
        given = self.given(cls)
        return attr in cscope.members or attr in cscope.bindings or attr in given or "*" in given

    def written(self) -> set[str]:
        """What some code writes as a whole: the sources of ``mutated_by``
        edges and of ``value_writes`` and ``receiver_writes``, and the
        classes a written variable may hold (``typed_writes``)."""
        indexer = self._indexer
        out = indexer.out
        found = {e.source for e in indexer.index.edges if e.detail == MUTATED_BY}
        found |= {state for state, _ in out.value_writes}
        found |= {state for state, _ in out.receiver_writes}
        for var, _ in out.typed_writes:
            found |= self.types.get(var, set())
        return found


def _writers(caller: str, callees: list[str]) -> list[str]:
    """Who writes what a call hands a callee that writes it: the caller,
    which decides what is handed and with what, and each such callee
    itself, which does the writing whoever calls it. A test calling
    ``SETTINGS.configure(debug=1)`` is code the isolation assumption leaves
    out (planner.isolated_test_code), but ``configure`` is not: a change to
    it reaches the readers of ``SETTINGS`` through its own edge (audit round
    7, U4). Where the caller is other code, the callee adds nothing new: the
    caller depends on it already."""
    if not callees:
        return []
    return list(dict.fromkeys([caller, *callees]))


class AnyClassOwners:
    """The in-scope classes a write onto the class of an object of unknown
    type may change (``type(o).x = v``, ``o.__class__.items.append(v)``,
    ``setattr(type(o), "x", v)``, ``type(o)`` handed to a callee binding
    ``c.x``, or returned to a caller writing it; audit round 6, T25). The
    object may be an instance of any class, but binding ``x`` on it, or
    changing what it holds under ``x``, matters to the readers of a class
    that may have ``x``: one whose body binds or defines it, whose
    methods bind it on ``self`` or ``cls`` (an instance's own attribute may
    be a class's too, since which one the object is is not known), or that
    is given it elsewhere (ClassRecord: ``*`` gives it every name; U3), one
    with a base outside the index (which may define it), and every in-scope
    subclass of those. A special name (``__doc__``, ``__repr__``,
    ``__dict__``) every class has, an attribute no class binds or defines
    (which only code like this gives a class, for readers such as
    ``getattr(Conf, "flag", None)``), and an attribute named only at run
    time (``*``), are any class's."""

    def __init__(self, indexer: Resolver, record: ClassRecord) -> None:
        scopes = indexer.class_scopes
        self._indexer = indexer
        self._record = record
        self._body: dict[str, set[str]] = defaultdict(set)
        for cid, cscope in scopes.items():
            for name in cscope.members.keys() | cscope.bindings:
                self._body[name].add(cid)
        for cid, names in record.methods.items():
            for name in names:
                self._body[name].add(cid)
        self._opaque = {cid for cid, cscope in scopes.items() if cscope.opaque}
        self._found: dict[str, list[str]] = {}
        self._sizes: tuple[int, int] | None = None

    def owners(self, name: str) -> list[str]:
        out = self._indexer.out
        sizes = (len(out.own_class_writes), len(out.class_binds))
        if sizes != self._sizes:
            # What is given to classes grows as writes are decided.
            self._sizes, self._found = sizes, {}
        found = self._found.get(name)
        if found is None:
            indexer = self._indexer
            named = set(self._body.get(name, ()))
            for cid in indexer.class_scopes:
                given = self._record.given(cid)
                if name in given or "*" in given:
                    named.add(cid)
            if name == "*" or (name.startswith("__") and name.endswith("__")) or not named:
                found = sorted(indexer.class_scopes)
            else:
                family: set[str] = set()
                for cid in named | self._opaque:
                    family.add(cid)
                    family.update(indexer._descendants.get(cid, ()))
                found = sorted(family)
            self._found[name] = found
        return found

    def record(self, out: _Output, name: str, writer: str, module: str) -> None:
        """A write of ``name`` by ``writer`` on a class of unknown type: a
        ``mutated_by`` edge of each owner (``value_writes``, so that the
        class counts as written: it holds state, T21, and its attributes
        may be bound anew, T24), or for top-level code of the owner's own
        module (``module``), which its hash covers, a binding of the
        attribute by that module (``own_class_writes``)."""
        symbols = self._indexer.index.symbols
        for cid in self.owners(name):
            if cid == writer:
                continue
            if module and symbols[cid].module == module:
                out.own_class_writes.add((cid, name))
            else:
                out.value_writes.add((cid, writer))

    def names(self, what: str, flavors: set[str] | None) -> list[str]:
        """The attributes an ``anyclass:`` pass names: the one it was reached
        through, or what the callee binds or augments on its parameter
        (``param_flavors``: ``=x``, ``+x``), any for a change in place."""
        member = what[len(ANY_CLASS_WHAT) :]
        if member:
            return ["*" if member == "__dict__" else member]
        found = {how[1:] if how[:1] in "=+" and how[1:] else "*" for how in flavors or {"*"}}
        return ["*"] if "*" in found else sorted(found)


def _record_any_class_writes(out: _Output, owners: AnyClassOwners) -> None:
    for name, writer, module in sorted(out.any_class_writes):
        owners.record(out, name, writer, module)


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

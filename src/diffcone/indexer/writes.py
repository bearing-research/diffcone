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
  bound once to a parameter (``d = p``) is that parameter; ``*args`` and
  ``**kwargs`` are parameters written in part by an item's write (``objs[0]
  .x = v``, ``for o in objs: o.x = v``; audit round 8, V8), and so is any
  parameter by an element of it handed to a call that writes it (``for o
  in objs: o.reset()``; V7). A fresh
  instance holding a parameter (``r = Box(p)``, a library logger's record:
  ``RECORD``) written in place (``r.args.update(...)``, through ``h =
  r.args`` too) is recorded as ``held_param_writes``, decided after pass 2
  by ``_HeldSafety`` (audit round 8, V1, V7);
* every object it hands to a call (``passes``): one of its parameters, a
  module-level variable (``REG``, ``cfg.REG``, ``REG["sub"]``,
  ``import_module("pkg.cfg").REG``), or an in-scope module (``pkg.cfg``, a
  function-local import of it, ``sys.modules["pkg.cfg"]``,
  ``import_module("pkg.cfg")``, a local bound once to one of those), as an
  argument or as the receiver of a method call, with the callee when it
  resolves and its name otherwise. A starred argument (``f(*ITEMS)``) may
  land in any parameter from its position on (slot ``*<i>``), a
  double-starred one (``f(**PARTS)``) in any keyword (``**``), each in part
  (``_call_slots``, ``slot_params``; V8);
* what the logging library may call back (``log_calls``, ``log_contents``,
  ``log_registered``; ``_observe_logging``, ``observe_log_store``): log
  calls with what they log, and the objects and functions handed to the
  library or stored on a class under a callback's name (``SLog.handle =
  f``: ``STORED_ROLE``), decided after pass 2 by ``_LoggingModel`` (audit
  round 8, V1, V9); such a stored function is that method when project
  code calls it by the name too (``self.handle(record)``, ``LOG.emit(r)``:
  ``_LoggingModel.stored_methods``, ``_stored_method_edges``; V20);
* what a call of project code returns, as a pseudo-parameter of the caller
  (``RESULT_PARAM``) written or handed on like any parameter, with what
  the call hands each callee (``result_calls``), and what each function's
  ``return`` hands back of its parameters (``param_returns``): closed after
  pass 2 (``_returning_calls``), a write of the result writes what the
  callee may return of what it was handed (``r = wrap(p); r.x.append(1)``,
  ``b.tag().add(1)``; V19).

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
mutators, which pass 2 already records as writes; so is a local of the
function bound once to a display or to ``dict()``/``list()``/``set()``:
``fields = {}; fields.update(kw)``, ``_display_local``), or a third-party value
(a call into a module outside the roots, or a project factory whose every
``return`` is one, ``cast(T, ...)`` included: only the call is trusted), whose
methods are those of in-scope classes deriving from a third-party class
(``logging.setLoggerClass(StructuredLogger)``). So is a method a
``logging.Logger`` subclass calls on ``self`` (``self.handle(record)``,
``LIBRARY_METHOD`` + class): the library's own, which calls nothing of the
project directly, unless the project may define or give it to the class's
family (``_LoggingModel.library_method``); the library's callbacks are
followed from each log site instead (``_LoggingModel.passes``). A method called on an
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
made by an explicit call of its class; a module returned by a function
other code calls; a function bound onto a class under another name and
called on ``self``, but one stored on a logging-derived class under a
callback's name), what a third-party call returns (but ``typing.cast``),
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
from collections.abc import Callable
from typing import TYPE_CHECKING

from diffcone.indexer.aliases import STORED, VALUE
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
    UnresolvedKind,
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
# Slots of a starred argument: ``*<i>``, any positional slot from ``i`` on
# or the ``*args``; ``**``, any keyword or the ``**kwargs`` (V8).
ANY_POSITION = "*"
ANY_KEYWORD = "**"
# A pass record's ``held:<class id>|<what>``: ``<what>`` handed on as what a
# fresh instance of the class holds of its constructor's arguments
# (references.HELD; audit round 6, T23).
HELD_WHAT = "held:"
# A pass record's callee ``@<class id>.<attribute>``: the method of that name
# of whatever ``self.<attribute>`` holds in a method of the class.
HELD_BY = "@"
# A pass record's callee ``~``: a library method of that name, or one of an
# in-scope class deriving from a library class (``self.handle(record)`` in a
# ``logging.Logger`` subclass defining no ``handle``).
LIBRARY_METHOD = "~"
# The library's logger classes a project logger class may derive from.
LOGGER_CLASSES = frozenset({"logging.Logger", "logging.RootLogger"})

# The logging library (audit round 8, V1, V9): a log call makes a record and
# hands it to the filters and handlers of the logger and its parents, which
# format it; project objects handed to the library are called back
# (_LoggingModel). Logger methods that make a record:
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)
# ... that hand on a record made already:
RECORD_METHODS = frozenset({"handle", "callHandlers"})
# ... and what a ``logging.Logger`` subclass may call on ``self`` to log.
LOGGER_SELF_METHODS = LOG_METHODS | RECORD_METHODS | {"_log", "makeRecord"}
# Calls handing the library an object it calls back, and a library object's
# attributes holding such objects.
REGISTER_METHODS = frozenset({"addHandler", "addFilter", "setFormatter"})
REGISTER_ATTRIBUTES = frozenset({"handlers", "filters", "formatter"})
# Library callables whose arguments it calls back: a handler class's stream,
# queue, target or handlers (``StreamHandler(w)``, ``QueueListener(q, *hs)``).
LOG_HANDLER_SUFFIXES = ("Handler", "Listener")
# Configurations that may name any class or function to make and call.
LOG_CONFIGS = frozenset(
    {"logging.config.dictConfig", "logging.config.fileConfig", "logging.config.listen"}
)
# What the library calls on an object handed to it: a handler's, a filter's,
# a formatter's, a stream's or queue's methods, a record's.
OBJECT_CALLBACKS = frozenset(
    {
        "handle",
        "emit",
        "filter",
        "format",
        "formatTime",
        "formatException",
        "formatStack",
        "formatMessage",
        "usesTime",
        "flush",
        "handleError",
        "acquire",
        "release",
        "write",
        "put",
        "put_nowait",
        "getMessage",
    }
)
# What a logger's library methods call on the logger itself (a project
# subclass's override).
LOGGER_CALLBACKS = frozenset(
    {
        "makeRecord",
        "handle",
        "callHandlers",
        "_log",
        "findCaller",
        "isEnabledFor",
        "getEffectiveLevel",
        "filter",
        "hasHandlers",
        "process",
    }
)
# The library classes a logger class derives from.
LOGGER_FAMILY = frozenset({"logging.Logger", "logging.RootLogger", "logging.LoggerAdapter"})
# Callbacks handed what a record is made of, not a record.
DIRECT_CALLBACKS = frozenset({"makeRecord", "_log", "process"})
# ``log_calls`` kinds, ``log_contents`` sites and ``log_registered`` roles.
LIBRARY, VARIABLE_LOGGER, SELF_LOGGER, ANY_LOGGER = "lib", "var", "self", "any"
# An instance of an in-scope class (``self`` in its methods, a local bound
# once to a constructor call): a log site only when the class or a
# subclass derives from a ``logging`` class (``console.log(...)`` is not).
INSTANCE_LOGGER = "inst"
ARGS_SITE, RECORD_SITE = "args", "record"
HANDLER, FILTER, FORMATTER, STREAM = "handler", "filter", "formatter", "stream"
FUNCTION, FACTORY, ANY_REGISTERED = "function", "factory", "*"
# What the library calls on an object by the role it was handed in (a class
# deriving from a ``logging`` class has every ``OBJECT_CALLBACKS`` method
# called): a handler's ``handle``, a filter's ``filter``, a formatter's
# ``format``, a handler's stream's, queue's or target's methods; after a
# configuration naming anything, the first three by name.
ROLE_CALLBACKS = {
    HANDLER: frozenset({"handle"}),
    FILTER: frozenset({"filter"}),
    FORMATTER: frozenset({"format"}),
    STREAM: frozenset({"write", "flush", "put", "put_nowait", "handle"}),
    ANY_REGISTERED: frozenset({"handle", "filter", "format"}),
}
REGISTER_ROLES = {
    "addHandler": HANDLER,
    "addFilter": FILTER,
    "setFormatter": FORMATTER,
    "handlers": HANDLER,
    "filters": FILTER,
    "formatter": FORMATTER,
}
FUNCTION_WHAT = "func:"  # a function of the project handed to the library
# A function stored on an in-scope class under a callback's name
# (``SLog.handle = _recording_handle``): ``stored:<name>``, the holder the class.
STORED_ROLE = "stored:"
UNKNOWN_REGISTERED = "?"  # an object nothing says the class of
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
    """``name`` when it is a parameter of the function being walked, its
    ``*args`` and ``**kwargs`` included (a nested scope that binds the name
    hides it)."""
    scope = collector.scope
    while scope is not None:
        if name in scope.params or name in scope.var_params:
            return name
        if name in scope.literal_bound:
            return None
        scope = scope.literal_parent
    return None


def _var_param(collector: _ReferenceCollector, name: str) -> bool:
    """Whether ``name`` is the function's ``*args`` or ``**kwargs``: a new
    tuple or dict each call, holding what the caller handed."""
    scope = collector.scope
    while scope is not None:
        if name in scope.params:
            return False
        if name in scope.var_params:
            return True
        if name in scope.literal_bound:
            return False
        scope = scope.literal_parent
    return False


def _root_name(expr: ast.expr) -> ast.Name | None:
    """The name an item or attribute chain starts from (``p`` of
    ``p.items[0].x``)."""
    while isinstance(expr, (ast.Subscript, ast.Attribute)):
        expr = expr.value
    return expr if isinstance(expr, ast.Name) else None


# What a parameter root is (``_param_roots``): the parameter itself or part
# of it, a new container of its items (``*args``, ``list(p)``), or held by a
# new object (``held:<class>``: a fresh instance of an in-scope class whose
# constructor was handed it, or ``RECORD`` + class for the record a library
# logger's ``makeRecord`` makes; audit round 8, V1, V7).
PARAM, PARAM_COPY = "", "copy"
# The record ``makeRecord`` makes, called on ``self`` in a method of a
# ``logging.Logger`` subclass (``%record:<class id>``; ``%record:`` for what
# any logger's library methods make): a new object holding what it was
# handed, unless the class's ``makeRecord`` may be the project's or a project
# record factory makes records (decided after pass 2: ``_LoggingModel``).
RECORD = "%record:"
# What a call of project code returns, as a pseudo-parameter of the caller
# (``%result:<line>.<column>`` of the call): written as a parameter is, it
# writes what the callee may return of what the call hands it
# (``result_calls``, ``param_returns``; decided after pass 2: audit round 8,
# V19).
RESULT_PARAM = "%result:"
# Builtins whose result is a new container of the items of what they are
# handed (``for i, x in enumerate(p)``: ``x`` is part of ``p``).
_ITEM_CONTAINERS = frozenset(
    {"list", "tuple", "set", "frozenset", "sorted", "reversed", "iter", "enumerate", "zip", "dict"}
)
# Methods whose result is part of the object they are called on.
_PART_METHODS = frozenset({"items", "values", "get", "setdefault", "pop", "popitem"})

ParamRoots = dict[str, tuple[str, bool]]


def _merge_param_roots(*found: ParamRoots) -> ParamRoots:
    """The union of ``_param_roots``: a parameter reached itself on one way
    and held or copied on another is reached itself."""
    out: ParamRoots = {}
    for roots in found:
        for param, (kind, whole) in roots.items():
            if param not in out:
                out[param] = (kind, whole)
                continue
            old_kind, old_whole = out[param]
            if PARAM in (kind, old_kind):
                merged = PARAM
            elif old_kind.startswith(HELD_WHAT) or kind.startswith(HELD_WHAT):
                merged = old_kind if old_kind.startswith(HELD_WHAT) else kind
            else:
                merged = PARAM_COPY
            out[param] = (merged, whole and old_whole)
    return out


def _param_parts(roots: ParamRoots) -> ParamRoots:
    """What is part of the objects of ``roots`` (an item, an attribute): part
    of the parameter, whatever holds or copied it."""
    return {param: (PARAM, False) for param in roots}


def _param_roots(
    collector: _ReferenceCollector,
    expr: ast.expr,
    seen: frozenset[str] = frozenset(),
) -> ParamRoots:
    """The function's parameters ``expr`` may be or hold part of: parameter
    -> (PARAM, PARAM_COPY or ``held:<class>``, whether ``expr`` is that
    object itself rather than part of it). ``p``, ``p.x``, ``p[k]``,
    ``p.get(k)``; a local any binding of which is one of those (``d = p``,
    ``for o in p``, ``with p as d``, ``x = p or {}``); ``*args`` and
    ``**kwargs``, and a builtin's new container of a parameter's items
    (``list(p)``, ``enumerate(p)``), whose items are the caller's objects;
    and what a fresh instance of an in-scope class (``Box(p)``) or a
    library logger's record (``self.makeRecord(..., msg, args)``) was
    handed, changed when the instance is changed in place or binding one of
    its attributes runs code (``_HeldSafety``; audit round 8, V7, V8). What
    a call that may run project code returns is the call's pseudo-parameter
    (``RESULT_PARAM``): ``r = wrap(p)``, ``b.tag()``, written or handed on
    as a parameter is, writes what the callee may return of what it was
    handed (``param_returns``, after pass 2; V19). The same holds for what
    a call is handed (``_handed_all``): an element handed on (``for o in
    objs: o.reset()``) is part of the parameter."""
    if len(seen) > 8:
        return {}
    if isinstance(expr, (ast.Starred, ast.NamedExpr, ast.Await)):
        return _param_roots(collector, expr.value, seen)
    if isinstance(expr, ast.IfExp):
        return _merge_param_roots(
            _param_roots(collector, expr.body, seen),
            _param_roots(collector, expr.orelse, seen),
        )
    if isinstance(expr, ast.BoolOp):
        return _merge_param_roots(*(_param_roots(collector, v, seen) for v in expr.values))
    if isinstance(expr, (ast.Subscript, ast.Attribute)):
        return _param_parts(_param_roots(collector, expr.value, seen))
    if isinstance(expr, ast.Name):
        param = _own_param(collector, expr.id)
        if param is not None:
            return {param: (PARAM_COPY if _var_param(collector, param) else PARAM, True)}
        if not collector._is_local(expr.id) or expr.id in seen:
            return {}
        owner = collector._binding_scope(expr.id)
        if owner is None:
            return {}
        inner = seen | {expr.id}
        found: ParamRoots = {}
        for value, how in collector._sources_in(owner).get(expr.id, ()):
            roots = _param_roots(collector, value, inner)
            if how == STORED:
                # Stored into a parameter or part of one, it is part of what
                # the caller handed; into a new object or container itself
                # (``setattr(record, "fields", fields)``, ``kwargs["k"] =
                # d``), of nothing the caller handed (the store is a write of
                # the holder, recorded as such).
                roots = {p: r for p, r in roots.items() if r[0] == PARAM}
            if how != VALUE:  # an element, what entering gives, a holder
                roots = _param_parts(roots)
            found = _merge_param_roots(found, roots)
        return found
    if isinstance(expr, ast.Call):
        return _call_param_roots(collector, expr, seen)
    return {}


def _call_param_roots(
    collector: _ReferenceCollector, node: ast.Call, seen: frozenset[str]
) -> ParamRoots:
    """``_param_roots`` of a call: what a fresh object it makes holds of the
    parameters it is handed, part of its receiver, the items of a builtin's
    new container, what ``typing.cast`` is handed, or the call's own
    pseudo-parameter for what project code it may run returns
    (``_result_root``)."""
    func = node.func
    args = [*node.args, *(k.value for k in node.keywords)]
    if isinstance(func, ast.Attribute):
        logger = collector._logger_class(func.value) if func.attr == "makeRecord" else None
        if logger is not None:
            found = _merge_param_roots(*(_param_roots(collector, a, seen) for a in args))
            return {param: (f"{HELD_WHAT}{RECORD}{logger}", True) for param in found}
        if func.attr in _PART_METHODS:
            return _merge_param_roots(
                _param_parts(_param_roots(collector, func.value, seen)),
                _result_root(collector, node),
            )
    if (
        isinstance(func, ast.Name)
        and func.id in _ITEM_CONTAINERS
        and not collector._is_shadowed(func.id)
    ):
        found = _merge_param_roots(*(_param_roots(collector, a, seen) for a in args))
        return {param: (PARAM_COPY, True) for param in found}
    made = collector._made_instance(node)
    if made is not None and made[0] == "call":
        found = _merge_param_roots(*(_param_roots(collector, a, seen) for a in args))
        return {param: (f"{HELD_WHAT}{made[1]}", True) for param in found}
    parts = _flatten_chain(func)
    if parts is not None and not collector._is_local(parts[0]):
        target = collector.indexer.resolve_chain(parts, collector.scope)
        if isinstance(target, External):
            if "typing.cast" in collector._canonical_names(parts) and len(node.args) == 2:
                return _param_roots(collector, node.args[1], seen)  # the value itself
            return {}  # a third-party function's result (not followed)
        if isinstance(target, Resolved) and not target.detail:
            symbol = collector.indexer.index.symbols.get(target.symbol)
            if symbol is not None and symbol.kind == SymbolKind.CLASS:
                return {}  # a new instance (``made`` above when it holds anything)
    if (
        isinstance(func, ast.Name)
        and func.id in BUILTIN_NAMES
        and collector.indexer.resolve_chain([func.id], collector.scope) is None
    ):
        return {}
    return _result_root(collector, node)


def _result_root(collector: _ReferenceCollector, node: ast.Call) -> ParamRoots:
    """The pseudo-parameter of what ``node`` returns (``RESULT_PARAM``):
    ``observe_call`` records what the call hands which callee
    (``result_calls``), and a callee's ``return`` of its parameters
    (``param_returns``) says after pass 2 what a write of it changes."""
    return {_result_key(node): (PARAM, True)}


def _result_key(node: ast.Call) -> str:
    """The pseudo-parameter of ``node``'s result: the call's span, which a
    call it is made of does not share (``b.tag().add(1)``)."""
    return f"{RESULT_PARAM}{node.lineno}.{node.col_offset}.{node.end_lineno}.{node.end_col_offset}"


def _param_of(collector: _ReferenceCollector, expr: ast.expr) -> str | None:
    """A parameter of the function ``expr`` is or holds part of, held by no
    new object (the first, when it may be several; ``_param_roots``; no
    call's result)."""
    for param, (kind, _) in sorted(_param_roots(collector, expr).items()):
        if not kind.startswith(HELD_WHAT) and not param.startswith(RESULT_PARAM):
            return param
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
    through an item or an attribute of it, it is ``*``. A new container of a
    parameter's items changed itself (``kw["k"] = v`` on ``**kw``) changes
    none of them; a new object holding a parameter (``Box(p)``, a library
    logger's record) changes it as ``_HeldSafety`` decides after pass 2
    (``held_param_writes``; audit round 8, V7, V8)."""
    out = collector.indexer.out
    for param, (kind, whole) in sorted(_param_roots(collector, expr).items()):
        if whole and kind == PARAM_COPY:
            continue
        if whole and kind.startswith(HELD_WHAT):
            out.held_param_writes.add((collector.source, param, kind[len(HELD_WHAT) :], how))
            continue
        out.param_writes.add((collector.source, param))
        out.param_flavors.add((collector.source, param, how if whole else "*"))


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
    state too; ``result_stores``). And each parameter (or call result:
    ``RESULT_PARAM``) the value may be or hold part of (``param_returns``:
    ``return p``, ``return p.items``, ``return self``, ``return wrap(p)``;
    audit round 8, V19)."""
    scope = collector.scope
    if scope.literal_parent is None:
        for param, (kind, whole) in _param_roots(collector, value).items():
            collector.indexer.out.param_returns.add(
                (collector.source, param, whole and kind == PARAM)
            )
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


def _observe_logging(
    collector: _ReferenceCollector, node: ast.Call, parts: list[str] | None
) -> None:
    """A call that may make the logging library call project code back: a
    log call (``log_calls``, with what the record is made of or the record
    itself: ``log_contents``), or a call handing it an object or function it
    calls later (``log_registered``; audit round 8, V1, V9)."""
    func = node.func
    out = collector.indexer.out
    source = collector.source
    if (
        parts == ["setattr"]
        and len(node.args) == 3
        and not node.keywords
        and not collector._is_shadowed("setattr")
    ):
        for name in collector.scope.string_candidates(node.args[1]) or ():
            _stored_callback(collector, node.args[0], name, node.args[2])
    if parts is not None and not collector._is_local(parts[0]):
        target = collector.indexer.resolve_chain(parts, collector.scope)
        if isinstance(target, External):
            names = collector._canonical_names(parts)
            if names & LOG_CONFIGS:
                # A configuration may name any class or function to make.
                out.log_registered.add((source, ANY_REGISTERED, ANY_REGISTERED, ""))
            if "logging.setLogRecordFactory" in names and node.args:
                for what in _registered(collector, node.args[0]):
                    out.log_registered.add((source, FACTORY, what, ""))
            if "logging.basicConfig" in names:
                for kw in node.keywords:
                    if kw.arg in ("handlers", None):
                        for what in _registered(collector, kw.value):
                            out.log_registered.add((source, HANDLER, what, ""))
            if any(n.startswith("logging.") and n.endswith(LOG_HANDLER_SUFFIXES) for n in names):
                # A library handler's stream, queue, target or handlers.
                for _, expr, _ in _call_slots(node):
                    for what in _registered(collector, expr):
                        out.log_registered.add((source, STREAM, what, ""))
            if any(n.startswith("logging.") and n.rpartition(".")[2] in LOG_METHODS for n in names):
                _log_site(collector, node, LIBRARY, "", parts[-1])
    if not isinstance(func, ast.Attribute):
        return
    name = func.attr
    if name in REGISTER_METHODS and node.args:
        holder = _holder(collector, func.value)
        for what in _registered(collector, node.args[0]):
            out.log_registered.add((source, REGISTER_ROLES[name], what, holder))
    if (
        name in ("append", "extend", "insert")
        and isinstance(func.value, ast.Attribute)
        and func.value.attr in REGISTER_ATTRIBUTES
        and _library_holder(collector, func.value.value)
    ):
        holder = _holder(collector, func.value.value)
        for arg in node.args:
            for what in _registered(collector, arg):
                out.log_registered.add((source, REGISTER_ROLES[func.value.attr], what, holder))
    if name in LOGGER_SELF_METHODS:
        kind, ident = _log_receiver(collector, func.value, name)
        if kind:
            _log_site(collector, node, kind, ident, name)


def observe_log_store(
    collector: _ReferenceCollector, targets: list[ast.expr], value: ast.expr
) -> None:
    """An assignment the logging library calls back through: a library
    object's ``handlers``, ``filters`` or ``formatter`` bound, or a function
    stored on a library class (``logging.Logger.handle = f``), which every
    logger then runs (``log_registered``; audit round 8, V1)."""
    out = collector.indexer.out
    for target in targets:
        if not isinstance(target, ast.Attribute):
            continue
        _stored_callback(collector, target.value, target.attr, value)
        if target.attr in REGISTER_ATTRIBUTES and _library_holder(collector, target.value):
            holder = _holder(collector, target.value)
            role = REGISTER_ROLES[target.attr]
            for what in _registered(collector, value):
                out.log_registered.add((collector.source, role, what, holder))
        parts = _flatten_chain(target)
        if parts is None or len(parts) < 3 or collector._is_local(parts[0]):
            continue
        if not isinstance(collector.indexer.resolve_chain(parts[:-1], collector.scope), External):
            continue
        if any(n.startswith("logging.") for n in collector._canonical_names(parts)):
            for what in _registered(collector, value):
                out.log_registered.add((collector.source, FUNCTION, what, ""))


def _stored_callback(
    collector: _ReferenceCollector, holder: ast.expr, name: str, value: ast.expr
) -> None:
    """A function stored on an in-scope class under a name the logging
    library calls back (``SLog.handle = _recording_handle``, ``setattr(SLog,
    "handle", f)``): it runs as that method of the class's instances, decided
    after pass 2 when the class derives from ``logging`` (audit round 8, V1)."""
    if name not in OBJECT_CALLBACKS and name not in LOGGER_CALLBACKS:
        return
    cls = collector._class_named(holder)
    if cls is None:
        return
    for what in _registered(collector, value):
        if what.startswith(FUNCTION_WHAT):
            collector.indexer.out.log_registered.add(
                (collector.source, f"{STORED_ROLE}{name}", what, cls)
            )


def _log_receiver(collector: _ReferenceCollector, receiver: ast.expr, name: str) -> tuple[str, str]:
    """What a call of method ``name`` on ``receiver`` may log through
    (``log_calls``' kinds): ``self`` in a ``logging.Logger`` subclass, a
    library value, a module variable, a receiver of unknown type (only for
    the methods that log, not ``handle``: every project ``handle`` would be
    one); ("", "") for none (a class, function or module of the project)."""
    logger = collector._logger_class(receiver)
    if logger is not None:
        return SELF_LOGGER, logger
    if name not in LOG_METHODS and name not in RECORD_METHODS:
        return "", ""
    indexer = collector.indexer
    if isinstance(receiver, ast.Call):
        inner = _flatten_chain(receiver.func)
        if (
            inner is not None
            and not collector._is_local(inner[0])
            and isinstance(indexer.resolve_chain(inner, collector.scope), External)
        ):
            return LIBRARY, ""  # ``logging.getLogger(name).info(...)``
    chain = _flatten_chain(receiver)
    if (
        chain is not None
        and not collector._is_local(chain[0])
        and chain[0] != collector.scope.self_name
    ):
        target = indexer.resolve_chain(chain, collector.scope)
        if isinstance(target, External):
            return LIBRARY, ""
        if isinstance(target, ModuleNode):
            return "", ""
        if isinstance(target, Resolved) and not target.detail:
            symbol = indexer.index.symbols.get(target.symbol)
            if symbol is not None and symbol.kind == SymbolKind.VARIABLE:
                return VARIABLE_LOGGER, symbol.id
            if symbol is not None:
                return "", ""
    if name not in LOG_METHODS:
        return "", ""
    cls = _instance_class(collector, receiver)
    return (INSTANCE_LOGGER, cls) if cls is not None else (ANY_LOGGER, "")


def _instance_class(collector: _ReferenceCollector, receiver: ast.expr) -> str | None:
    """The in-scope class ``receiver`` is an instance of, or of a subclass
    of: ``self`` in one of its methods, a constructor call, or a local of
    the function (no parameter) bound once to one."""
    scope = collector.scope
    if isinstance(receiver, ast.Name):
        if receiver.id == scope.self_name:
            return scope.self_class if not scope.self_is_class else None
        if not collector._is_local(receiver.id) or _own_param(collector, receiver.id) is not None:
            return None
        value = collector._local_source(receiver.id)
        if not isinstance(value, ast.Call):
            return None
        receiver = value
    if isinstance(receiver, ast.Call):
        made = collector._made_instance(receiver)
        if made is not None and made[0] == "call":
            return made[1]
    return None


def _log_site(
    collector: _ReferenceCollector, node: ast.Call, kind: str, ident: str, name: str
) -> None:
    """A log call: recorded with what it hands the library (a record for
    ``handle``/``callHandlers`` on a logger's own ``self``, what one is made
    of otherwise)."""
    out = collector.indexer.out
    out.log_calls.add((collector.source, kind, ident, name))
    site = RECORD_SITE if kind == SELF_LOGGER and name in RECORD_METHODS else ARGS_SITE
    for _, expr, _ in _call_slots(node):
        for what in _content(collector, expr):
            out.log_contents.add((collector.source, site, what))


def _content(collector: _ReferenceCollector, expr: ast.expr) -> list[str]:
    """What a call hands on in ``expr`` as pass records name it: the
    parameters it may be or hold (``_handed_all``) and the module state it
    may be part of (``_handed_state``)."""
    found = [what for what, _ in _handed_all(collector, expr)]
    root = _root_name(expr)
    if not (found and root is not None and _own_param(collector, root.id) is not None):
        found += collector._handed_state(expr)
    return list(dict.fromkeys(found))


def _registered(collector: _ReferenceCollector, expr: ast.expr) -> list[str]:
    """What an object or function handed to the logging library to call
    back is, as pass records name it (``func:<id>`` for a function of the
    project), none for a library value (``logging.StreamHandler()``, a
    local bound only to one), and ``?`` when nothing says."""
    if isinstance(expr, ast.Starred):
        return _registered(collector, expr.value)
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return list(dict.fromkeys(w for e in expr.elts for w in _registered(collector, e)))
    indexer = collector.indexer
    chain = _flatten_chain(expr)
    if chain is not None and not collector._is_local(chain[0]):
        target = indexer.resolve_chain(chain, collector.scope)
        if isinstance(target, External):
            return []
        if isinstance(target, Resolved) and not target.detail:
            symbol = indexer.index.symbols.get(target.symbol)
            if symbol is not None and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                return [f"{FUNCTION_WHAT}{symbol.id}"]
    found = _content(collector, expr)
    if found:
        return found
    if _library_value(collector, expr, 0):
        return []
    return [UNKNOWN_REGISTERED]


def _library_value(collector: _ReferenceCollector, expr: ast.expr, depth: int) -> bool:
    """``expr`` is what a call into a module outside the source roots made,
    or a local (no parameter) every binding of which is one (storing it
    into something else, ``log.handlers = [h]``, binds nothing)."""
    if isinstance(expr, ast.Call):
        func = _flatten_chain(expr.func)
        return (
            func is not None
            and not collector._is_local(func[0])
            and isinstance(collector.indexer.resolve_chain(func, collector.scope), External)
        )
    if (
        not isinstance(expr, ast.Name)
        or not collector._is_local(expr.id)
        or _own_param(collector, expr.id) is not None
        or depth > 3
    ):
        return False
    owner = collector._binding_scope(expr.id)
    sources = collector._sources_in(owner).get(expr.id, ()) if owner is not None else ()
    values = [(value, how) for value, how in sources if how != STORED]
    return bool(values) and all(
        how == VALUE and _library_value(collector, value, depth + 1) for value, how in values
    )


def _library_holder(collector: _ReferenceCollector, expr: ast.expr) -> bool:
    """Whether ``expr``, whose ``handlers``/``filters``/``formatter`` is
    bound or added to, may be a library logger or handler: not ``self`` in a
    class that is no ``logging.Logger`` subclass (a project event bus's
    ``self.handlers``)."""
    root = _root_name(expr)
    if root is None or root.id != collector.scope.self_name:
        return True
    return expr is root and collector._logger_class(expr) is not None


def _holder(collector: _ReferenceCollector, expr: ast.expr) -> str:
    """``var:<id>`` when ``expr`` is a module variable (a logger handlers
    are added to), else ""."""
    chain = _flatten_chain(expr)
    if chain is None or collector._is_local(chain[0]) or chain[0] == collector.scope.self_name:
        return ""
    target = collector.indexer.resolve_chain(chain, collector.scope)
    if isinstance(target, Resolved) and not target.detail:
        symbol = collector.indexer.index.symbols.get(target.symbol)
        if symbol is not None and symbol.kind == SymbolKind.VARIABLE:
            return f"var:{symbol.id}"
    return ""


def observe_call(collector: _ReferenceCollector, node: ast.Call, parts: list[str] | None) -> None:
    """A call that writes a parameter, or hands a parameter or a module
    variable on to a callee."""
    func = node.func
    indexer = collector.indexer
    _observe_logging(collector, node, parts)
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
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _DISPLAY_METHODS
        and _display_local(collector, func.value)
    ):
        # ``fields = {}; fields.update(kwargs)``: the builtin container's own
        # method, no method of ours of that name (the store into it is
        # recorded as a binding of what it holds: ``name_sources``).
        return
    passes = _call_slots(node)
    handed: list[tuple[str, str]] = []  # (slot, what)
    whole: set[tuple[str, str]] = set()  # a parameter itself handed on (T23)
    for slot, expr, part in passes:
        found = _handed_all(collector, expr)
        for what, itself in found:
            handed.append((slot, what))
            if itself and not part:
                whole.add((slot, what))
        if not _plainly_handed(collector, expr):
            # Through a name bound to part of it, a class (audit round 6, T6, T7).
            handed += [(slot, w) for w in collector._handed_state(expr)]
    if isinstance(func, ast.Attribute) and func.attr not in CONTAINER_MUTATORS:
        found = _handed_all(collector, func.value, modules=False)
        for what, itself in found:
            handed.append((RECEIVER, what))
            if itself:
                whole.add((RECEIVER, what))
        if not _plainly_handed(collector, func.value, modules=False):
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
        logger = (
            collector._logger_class(func.value)
            if not callees and isinstance(func, ast.Attribute)
            else None
        )
        if logger is not None:
            # ``self.handle(record)`` in a ``logging.Logger`` subclass: the
            # library's when nothing in scope may define or give the method
            # (``_LoggingModel.library_method``), whose calls back into the
            # project are its log sites' (``log_calls``); every method of
            # that name otherwise.
            callees.append(f"{LIBRARY_METHOD}{logger}")
    out = indexer.out.passes
    for slot, what in handed:
        for callee in callees or [""]:
            out.add((collector.source, what, callee, name, slot, bound))
            if (slot, what) in whole:
                indexer.out.whole_passes.add((collector.source, what, callee, name, slot, bound))
    if not isinstance(collector._parent(node), ast.Expr) and not (
        callees and callees[0].endswith(".__init__")
    ):
        # The result is used: a write of it (``RESULT_PARAM``) writes what
        # the callee may return of what it is handed (V19).
        key = _result_key(node)
        results = indexer.out.result_calls
        for slot, what in handed:
            for callee in callees or [""]:
                itself = (slot, what) in whole
                results.add((collector.source, what, callee, name, slot, bound, key, itself))
    for slot, expr, _ in passes:
        held = _held_attr(collector, expr)
        if held:
            for callee in callees or [""]:
                indexer.out.held_passes.add((collector.source, callee, name, slot, held, bound))


def _call_slots(node: ast.Call) -> list[tuple[str, ast.expr, bool]]:
    """Each argument of a call with the slot it lands in and whether only
    its items are handed: a positional index, ``*<i>`` for any position
    from ``i`` on or the ``*args`` (a starred argument, and every positional
    one after it), ``=<keyword>``, or ``**`` for any keyword or the
    ``**kwargs`` (``**D``; audit round 8, V8)."""
    slots: list[tuple[str, ast.expr, bool]] = []
    starred = False
    for i, arg in enumerate(node.args):
        if isinstance(arg, ast.Starred):
            starred = True
            slots.append((f"{ANY_POSITION}{i}", arg.value, True))
        else:
            slots.append((f"{ANY_POSITION}{i}" if starred else str(i), arg, False))
    for kw in node.keywords:
        if kw.arg is not None:
            slots.append((f"={kw.arg}", kw.value, False))
        else:
            slots.append((ANY_KEYWORD, kw.value, True))
    return slots


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


# Methods of a builtin list, dict or set that call nothing they are handed
# (``sort``'s ``key`` is called).
_DISPLAY_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "reverse",
        "popitem",
        "update",
        "setdefault",
        "add",
        "discard",
        "get",
        "keys",
        "values",
        "items",
        "copy",
        "index",
        "count",
    }
)
_DISPLAY_TYPES = ("dict", "list", "set")


def _display_local(collector: _ReferenceCollector, expr: ast.expr) -> bool:
    """Whether ``expr`` is a local of the function bound once, to a list,
    dict or set display or comprehension or to a call of the builtin
    ``dict``/``list``/``set`` (an object whose methods are the builtin's)."""
    if not isinstance(expr, ast.Name) or not collector._is_local(expr.id):
        return False
    if _own_param(collector, expr.id) is not None:
        return False
    value = collector._local_source(expr.id)
    if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.ListComp, ast.SetComp, ast.DictComp)):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _DISPLAY_TYPES
        and collector.indexer.resolve_chain([value.func.id], collector.scope) is None
    )


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
    argument) an in-scope module (the first of ``_handed_all``)."""
    found = _handed_all(collector, expr, modules=modules)
    return found[0][0] if found else None


def _handed_all(
    collector: _ReferenceCollector, expr: ast.expr, *, modules: bool = True
) -> list[tuple[str, bool]]:
    """What a call is handed in ``expr``, with whether it is a parameter
    itself (not an item, an attribute, a copy of its items or what a new
    object holds of it): each parameter it may be or hold (``param:<name>``,
    ``held:<class>|param:<name>`` for one a fresh object holds;
    ``_param_roots``), else a module-level variable (or part of one), else
    (``modules``: as an argument) an in-scope module. A call's result
    (``param:`` + ``RESULT_PARAM``) comes with what else it may be."""
    if isinstance(expr, ast.Call):
        # ``vars(p)``/``p.__dict__``-style views hand on the object itself.
        if isinstance(expr.func, ast.Name) and expr.func.id == "vars" and expr.args:
            expr = expr.args[0]
        else:
            found = _param_whats(_param_roots(collector, expr))
            if _real_params(found):
                return found
            module = _module_of(collector, expr) if modules else None
            return found + ([(f"module:{module}", False)] if module is not None else [])
    found = _param_whats(_param_roots(collector, expr))
    if _real_params(found):
        return found
    what = _handed_other(collector, expr, modules=modules)
    return found + ([(what, False)] if what is not None else [])


def _real_params(found: list[tuple[str, bool]]) -> bool:
    """Whether ``_param_whats`` names a parameter, not only a call's result."""
    return any(RESULT_PARAM not in what for what, _ in found)


def _plainly_handed(
    collector: _ReferenceCollector, expr: ast.expr, *, modules: bool = True
) -> bool:
    """Whether ``expr`` is plainly a parameter (``p``, ``p.x``, ``p[k]``, a
    local bound once to one of those; not ``*args``, not what a new object
    or container holds of one), a module variable or a module: what a call
    is handed is then that alone, and otherwise also the state
    ``_handed_state`` finds (the rule before ``_handed_all`` followed more
    of what holds a parameter, kept so that following more adds only)."""
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Name) and expr.func.id == "vars" and expr.args:
            expr = expr.args[0]
        else:
            return modules and _module_of(collector, expr) is not None
    root = _root_name(expr)
    if root is not None:
        param = _own_param(collector, root.id)
        if param is not None and not _var_param(collector, param):
            return True
        if param is None and root.id in collector.scope.locals:
            source = collector._local_source(root.id)
            if source is not None and source is not expr:
                inner = _root_name(source)
                if inner is not None and inner is not root:
                    param = _own_param(collector, inner.id)
                    if param is not None and not _var_param(collector, param):
                        return True
    return _handed_other(collector, expr, modules=modules) is not None


def _param_whats(roots: ParamRoots) -> list[tuple[str, bool]]:
    """Pass records' ``what`` for ``_param_roots``, with whether each is
    the parameter itself."""
    found: list[tuple[str, bool]] = []
    for param, (kind, whole) in sorted(roots.items()):
        if kind.startswith(HELD_WHAT):
            found.append((f"{kind}|param:{param}", False))
        else:
            found.append((f"param:{param}", whole and kind == PARAM))
    return found


def _handed_other(collector: _ReferenceCollector, expr: ast.expr, *, modules: bool) -> str | None:
    """``_handed_all`` of what is no parameter: a module variable or module."""
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


def apply_writes(indexer: Resolver) -> tuple[ClassRecord, dict[str, set[str]]]:
    """Close parameter writes over the calls that hand parameters on, then
    give each variable handed to a written parameter a ``mutated_by`` edge
    to the caller (see the module docstring); and keep each store through
    what a method of the receiver returns that may be the receiver's state
    (``_resolve_result_stores``). Gives back the record of what each class
    is given (ClassRecord), which the decisions after it read, and each log
    site's callbacks (``_LoggingModel``: what they write themselves is the
    log site's write too, Indexer._log_callback_writes)."""
    record, links = _apply_writes(indexer)
    _resolve_result_stores(indexer)
    return record, links


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


def _apply_writes(indexer: Resolver) -> tuple[ClassRecord, dict[str, set[str]]]:
    out = indexer.out
    params = out.func_params
    _apply_library_calls(indexer)
    class_record = ClassRecord(indexer)
    # Writes onto a class of unknown type are writes of the classes that may
    # own the attribute, before anything asks which classes are written (T25).
    owners = class_record.owners
    _record_any_class_writes(out, owners)
    writes: set[tuple[str, str]] = set(out.param_writes)
    if not writes and not out.held_param_writes and not out.log_calls:
        model = _LoggingModel(indexer, class_record)
        _stored_method_edges(out, model.stored_methods())
        _apply_held_binds(indexer, _HeldSafety(indexer, model))
        _record_any_class_writes(out, owners)
        return class_record, {}
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
    # (function, name) -> the classes a function stored on a logging-derived
    # class under a callback's name is a method of (``SLog.handle = f``):
    # called by that name on one of them, it runs bound (V1, V20).
    as_method: dict[tuple[str, str], set[str]] = {}

    def slot_params(callee: str, slot: str, bound: bool, name: str = "") -> list[str]:
        """The parameters of ``callee`` an argument in ``slot`` may land in:
        a positional one, the ``*args`` past them, a keyword, the
        ``**kwargs`` for one no parameter is named; any from a position on
        for a starred argument, any keyword for ``**D`` (audit round 8, V8).
        A function stored as a method (``as_method``) called by that
        ``name`` is bound."""
        info = params.get(callee)
        if info is None:
            return []
        method = info.bound or (callee, name) in as_method
        if slot == RECEIVER:
            return [info.positional[0]] if method and info.positional else []
        varargs = [info.varargs] if info.varargs else []
        varkw = [info.varkw] if info.varkw else []
        shift = 1 if method and bound else 0
        if slot == ANY_KEYWORD:
            return [*info.positional[shift:], *info.keyword_only, *varkw]
        if slot.startswith("="):
            name = slot[1:]
            if varkw and name not in info.positional and name not in info.keyword_only:
                return varkw
            return [name]
        if slot.startswith(ANY_POSITION):
            return [*info.positional[int(slot[1:]) + shift :], *varargs]
        index = int(slot) + shift
        return [info.positional[index]] if index < len(info.positional) else varargs

    def writes_param(callee: str, slot: str, bound: bool, name: str = "") -> list[str]:
        """``slot_params`` that ``callee`` writes."""
        return [p for p in slot_params(callee, slot, bound, name) if (callee, p) in writes]

    def stored_methods(cls: str, name: str) -> list[str]:
        """The functions stored as method ``name`` of class ``cls``."""
        return sorted(f for (f, n), classes in as_method.items() if n == name and cls in classes)

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

    model = _LoggingModel(indexer, class_record, value_classes, third_party, by_name)
    safe = _HeldSafety(indexer, model)
    # A function stored on a logging-derived class under a callback's name
    # is that method when project code calls it too (``self.handle(record)``
    # in a method of the class, ``LOG.handle(r)``): any method of that name
    # (``by_name``) includes it, and an attribute of that name nothing
    # resolves (``self.handle``, ``LOG.handle``) may be it, as the planner
    # matches it to every symbol of the name (V20).
    as_method.update(model.stored_methods())
    for function, name in sorted(as_method):
        if function not in by_name[name]:
            by_name[name] = sorted([*by_name[name], function])
    _stored_method_edges(out, as_method)

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
                    found += third_party_methods(name)
                    continue
                hit = indexer.lookup_in_class(kind, name)
                found += stored_methods(kind, name)
                if isinstance(hit, Resolved) and not hit.detail:
                    found.append(hit.symbol)
                elif indexer.class_scopes[kind].opaque:
                    # Not found in scope: a third-party base's method.
                    found += third_party_methods(name)
            return sorted(set(found))
        if callee.startswith(LIBRARY_METHOD):
            # The library's own method calls nothing of the project but its
            # log sites' callbacks; any method of that name if the project
            # may define or give one (V1).
            if model.library_method(callee[len(LIBRARY_METHOD) :], name):
                return []
            return by_name.get(name, [])
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
                return third_party_methods(name)
            classes = value_classes(what[4:])
            if classes is not None:
                found: list[str] = []
                for cls in classes:
                    hit = indexer.lookup_in_class(cls, name)
                    if isinstance(hit, Resolved) and not hit.detail:
                        found.append(hit.symbol)
                    found += stored_methods(cls, name)
                return sorted(set(found))
        return by_name.get(name, [])

    def third_party_methods(name: str) -> list[str]:
        """The methods ``name`` of in-scope classes deriving from a
        third-party class, a function stored as one included (V20)."""
        found = [m for m in by_name.get(name, []) if derives_from_third_party(m)]
        found += [f for f, n in as_method if n == name and f not in found]
        return found

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
        _, _, _, name, slot, bound = record
        found: list[str] = []
        for callee in callees(record):
            if writes_param(callee, slot, bound, name):
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
        _, _, _, name, slot, bound = record
        found: set[str] | None = None
        for callee in callees(record):
            for param in writes_param(callee, slot, bound, name):
                found = (found or set()) | (flavors.get((callee, param)) or {"*"})
        return found

    # A fresh object holding a parameter, written in a way that may change
    # what it holds (``Box(p).items.append(x)``, a library logger's record
    # whose ``args`` is changed: V1, V7).
    for function, param, cls, how in sorted(out.held_param_writes):
        if safe.writes(cls, {how}):
            writes.add((function, param))
            flavors[(function, param)].add("*")
    # What the logging library calls back with what a log call hands it.
    passes += model.passes(passes, callees, slot_params)
    links = model.links
    param_passes = [p for p in passes if p[1].startswith("param:")]
    held_param_passes = [
        p
        for p in passes
        if p[1].startswith(HELD_WHAT) and p[1].partition("|")[2].startswith("param:")
    ]
    returning = _returning_calls(out, callees, slot_params)
    changed = True
    while changed:
        changed = False
        for caller, key, record, itself, hits in returning:
            # A parameter handed to a call whose result is written, which
            # the callee may return (``r = wrap(p); r.x.append(1)``; V19).
            if (caller, key) not in writes:
                continue
            what = record[1]
            how = flavors[(caller, key)] or {"*"}
            if not itself or not all(w for _, w in hits):
                how = {"*"}
            if what.startswith(HELD_WHAT):
                cls, _, what = what[len(HELD_WHAT) :].partition("|")
                if not safe.writes(cls, how):
                    continue
                how = {"*"}
            if not what.startswith("param:"):
                continue
            target = (caller, what[len("param:") :])
            if target not in writes:
                writes.add(target)
                changed = True
            if not how <= flavors[target]:
                flavors[target] |= how
                changed = True
        for record in held_param_passes:
            # A parameter a fresh object holds, handed on in it: written
            # when the callee writes the object in a way that may change it.
            cls, _, inner = record[1][len(HELD_WHAT) :].partition("|")
            key = (record[0], inner[len("param:") :])
            if key in writes and "*" in flavors[key]:
                continue
            how = written_flavors(record)
            if how is None or not safe.writes(cls, how):
                continue
            if key not in writes:
                writes.add(key)
                changed = True
            if "*" not in flavors[key]:
                flavors[key].add("*")
                changed = True
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
        if callee.startswith(LIBRARY_METHOD):
            return callees(record)
        return [callee] if callee else []

    # A method called on state stores into it when it writes its receiver
    # (``self._cache.set(k, v)``: the ``set`` of what ``_cache`` is known to
    # hold, or ``self.put(...)`` resolved through the class).
    for caller, where, callee, name in sorted(out.computed_passes):
        record = (caller, "param:self", callee, name, RECEIVER, True)
        for c in resolved_callees(record):
            if writes_param(c, RECEIVER, True, name):
                out.stores.add((caller, where))
                break
    # A parameter's attribute handed to a callee that writes the parameter
    # it lands in: a store into that container is read through the
    # attribute (SourceIndex.handed).
    for caller, callee, name, slot, held, bound in sorted(out.held_passes):
        record = (caller, "param:self", callee, name, slot, bound)
        for c in resolved_callees(record):
            if writes_param(c, slot, bound, name):
                out.handed.add((caller, c, held))
    module_variables: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols.values():
        if symbol.kind == SymbolKind.VARIABLE:
            module_variables[symbol.module].append(symbol.id)

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
    # What a call is handed that is no parameter (a module variable, an
    # instance, a module), when the call's result is written and the callee
    # may return it: written by the caller, by each callee that may return
    # it and by each that writes the result for the caller (V19).
    result_writers: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record in param_passes:
        if record[1].startswith(f"param:{RESULT_PARAM}"):
            key = (record[0], record[1][len("param:") :])
            result_writers[key] += writing_callees(record)
    for caller, key, record, itself, hits in returning:
        what = record[1]
        if (caller, key) not in writes or what.startswith("param:"):
            continue
        how = flavors[(caller, key)] or {"*"}
        if not itself or not all(w for _, w in hits):
            how = {"*"}
        if what.startswith(HELD_WHAT):
            cls, _, what = what[len(HELD_WHAT) :].partition("|")
            if what.startswith("param:") or not safe.writes(cls, how):
                continue
            how = {"*"}
        doers = [c for c, _ in hits] + result_writers.get((caller, key), [])
        for writer in _writers(caller, doers):
            if what.startswith("module:"):
                module = what[len("module:") :]
                for m in sorted(module_variables):
                    if m == module or m.startswith(module + "."):
                        for variable in module_variables[m]:
                            if writer not in (variable, m):
                                out.edges.add(
                                    Edge(variable, writer, EdgeKind.REFERENCES, MUTATED_BY)
                                )
            else:
                handed_written(writer, what, False, how)
    _apply_held_binds(indexer, safe)
    _record_any_class_writes(out, owners)
    return class_record, links


def _stored_method_edges(out: _Output, as_method: dict[tuple[str, str], set[str]]) -> None:
    """An attribute nothing resolves (``self.handle``, ``LOG.handle``) may be
    a function stored as a method of that name (``as_method``): an edge to
    it, as the planner matches the name to every symbol of it (V20)."""
    stored: dict[str, list[str]] = defaultdict(list)
    for function, name in sorted(as_method):
        stored[name].append(function)
    if not stored:
        return
    for ref in sorted(out.unresolved):
        if ref.kind == UnresolvedKind.ATTRIBUTE:
            for function in stored.get(ref.name, ()):
                if ref.symbol != function:
                    out.edges.add(
                        Edge(ref.symbol, function, EdgeKind.UNRESOLVED_NAME_MATCH, ref.detail)
                    )


def _returning_calls(
    out: _Output,
    callees: Callable[[Pass], list[str]],
    slot_params: Callable[[str, str, bool, str], list[str]],
) -> list[tuple[str, str, Pass, bool, list[tuple[str, bool]]]]:
    """Each call whose result the caller uses and what it hands a callee
    that may return it (``result_calls``): (caller, the result's
    pseudo-parameter, the pass record, whether it is handed itself, and
    (callee, whether it returns the object itself) for each callee that
    may). What a function returns of its parameters (``param_returns``) is
    closed first over the calls it returns the result of (``return
    wrap(q)``: ``q`` when ``wrap`` returns its parameter; audit round 8,
    V19)."""
    returned: dict[str, dict[str, bool]] = defaultdict(dict)
    for function, param, itself in sorted(out.param_returns):
        old = returned[function].get(param)
        returned[function][param] = itself if old is None else (old and itself)
    if not returned:
        return []
    records: list[tuple[str, str, Pass, bool]] = []
    for caller, what, callee, name, slot, bound, key, itself in sorted(out.result_calls):
        records.append((caller, key, (caller, what, callee, name, slot, bound), itself))

    def hits_of(record: Pass) -> list[tuple[str, bool]]:
        _, _, _, name, slot, bound = record
        found: list[tuple[str, bool]] = []
        for callee in callees(record):
            got = returned.get(callee)
            if got:
                for param in slot_params(callee, slot, bound, name):
                    if param in got:
                        found.append((callee, got[param]))
        return found

    hits = {record: hits_of(record) for _, _, record, _ in records}
    changed = True
    while changed:  # what a function returns of what a call it returns is handed
        changed = False
        for function, key, record, itself in records:
            mine = returned.get(function)
            if mine is None or key not in mine or not hits[record]:
                continue
            what = record[1]
            part = what.startswith(HELD_WHAT)
            if part:
                what = what.partition("|")[2]
            if not what.startswith("param:"):
                continue
            param = what[len("param:") :]
            new = mine[key] and itself and not part and all(w for _, w in hits[record])
            old = mine.get(param)
            if old is None or (old and not new):
                mine[param] = new
                changed = True
        if changed:
            hits = {record: hits_of(record) for _, _, record, _ in records}
    return [
        (caller, key, record, itself, hits[record])
        for caller, key, record, itself in records
        if hits[record]
    ]


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


class _LoggingModel:
    """What the logging library calls back (audit round 8, V1, V9). A log
    call (``log_calls``: ``log.info(...)`` on a library logger, a module
    variable that may hold one or a receiver of unknown type, ``logging.
    warning(...)``, a ``logging.Logger`` subclass's ``self.handle(record)``)
    makes a record of what it is handed (``log_contents``) and hands it to
    the filters and handlers of the logger and of its parents, which format
    it. Each may be a project object, so each log site calls, with the
    record (a record callback) or with what it is made of (``makeRecord``,
    ``_log``, a record factory: ``DIRECT_CALLBACKS``):

    * the ``OBJECT_CALLBACKS`` methods of each object handed to the library
      (``log_registered``: ``addHandler``, ``addFilter``, ``setFormatter``,
      ``basicConfig(handlers=...)``, a library handler's stream, queue or
      target, a library object's ``handlers``/``filters``/``formatter``),
      through the parameters it is handed to (``def attach(h):
      root.addHandler(h)``), with the object as their receiver, and the
      module variable it was added to (a logger whose ``handlers`` holds
      it); every method of those names for one whose class is not known;
    * a function handed to the library (a filter, a record factory, one
      stored on a library class: ``logging.Logger.handle = f``);
    * the ``OBJECT_CALLBACKS`` methods of each class deriving from a
      ``logging`` class other than a logger (a handler, a formatter), on any
      instance of the class, and the ``LOGGER_CALLBACKS`` overrides of a
      logger class (``logging.setLoggerClass``) on the logger logged
      through (any instance of the class when the site names none);
    * for a configuration that may name any class (``dictConfig``), every
      method named as a handler's, filter's or formatter's
      (``ROLE_CALLBACKS``).

    An object handed in a role has that role's methods called
    (``ROLE_CALLBACKS``: a handler's ``handle``, a filter's ``filter``, a
    formatter's ``format``, a stream's ``write``); a library value (a
    ``logging.StreamHandler()``, a local bound only to one) none.

    Each callback is a pass record (the record held as a fresh ``LogRecord``
    holds what it is made of: ``RECORD``), and what a callback writes itself
    (``MADE.append(...)`` in a ``makeRecord`` override) is a write of the log
    site's too (``links``; Indexer._log_callback_writes). Not followed: a
    library's own logging (``urllib3`` logging into a project handler), and
    an object reached through the library (``logging.getLogger(n).handlers
    [0].records`` in a test).

    Whether a method a ``logging.Logger`` subclass calls on ``self``
    (``self.handle``, ``self.makeRecord``) is the library's
    (``library_method``), and whether a record is a fresh ``LogRecord``
    holding what it was made of (``fresh_record``), are decided here, after
    pass 2, over every class of the family's MRO (a mixin of a subclass
    included) and the one record of what each class is given anywhere
    (ClassRecord: a class attribute bound here or in another module, by a
    decorator or metaclass, an instance attribute bound in a method), a
    class written as a whole (``type()`` naming it as a base), a store of
    that name onto a ``logging`` class, and a project record factory
    (``logging.setLogRecordFactory``). As for any class, a store of the
    name onto an object of unknown type (``active.handle = h``) is not taken
    for an instance attribute shadowing the method."""

    def __init__(
        self,
        indexer: Resolver,
        record: ClassRecord,
        value_classes: Callable[[str], list[str] | None] | None = None,
        third_party: Callable[[str], bool] | None = None,
        by_name: dict[str, list[str]] | None = None,
    ) -> None:
        self.indexer = indexer
        self.record = record
        self._value_classes = value_classes
        self._third_party = third_party
        self._by_name = by_name or {}
        out = indexer.out
        # Log site -> the callbacks it runs (Indexer._log_callback_writes).
        self.links: dict[str, set[str]] = {}
        self.factory = any(role == FACTORY for _, role, _, _ in out.log_registered)
        # Names stored onto a ``logging`` class (``*``: any).
        self.patched: set[str] = set()
        for _, what in out.process_writes:
            head, paren, rest = what.partition("(")
            if paren:
                if head in ("setattr", "delattr") and rest.startswith("logging"):
                    self.patched.add("*")
            elif what.startswith("logging.") and what.count(".") >= 2:
                self.patched.add(what.rpartition(".")[2])
        self._library: dict[tuple[str, str], bool] = {}
        self._derived: dict[str, set[str] | None] = {}
        self._written: set[str] | None = None
        self._stored: dict[tuple[str, str], set[str]] | None = None

    def stored_methods(self) -> dict[tuple[str, str], set[str]]:
        """(function, name) -> the classes a function stored on an in-scope
        class under a callback's name (``SLog.handle = f``,
        ``setattr(SLog, "emit", f)``: ``STORED_ROLE``) is that method of:
        the class and its subclasses deriving from ``logging``, a handler's,
        filter's or formatter's callback on one that is no logger, a
        logger's on a logger class. The library calls it back (``passes``),
        and so may the project's own code (``self.handle(record)``; audit
        round 8, V1, V20)."""
        if self._stored is not None:
            return self._stored
        indexer = self.indexer
        out = indexer.out
        found: dict[tuple[str, str], set[str]] = defaultdict(set)
        for _, role, what, holder in sorted(out.log_registered):
            if not role.startswith(STORED_ROLE) or not what.startswith(FUNCTION_WHAT):
                continue
            function, name = what[len(FUNCTION_WHAT) :], role[len(STORED_ROLE) :]
            if function not in indexer.index.symbols or function not in out.func_params:
                continue
            for c in sorted({holder, *indexer._descendants.get(holder, ())}):
                bases = self._logging_bases(c)
                if bases is not None and not bases:
                    continue
                if (bases is None or bases - LOGGER_FAMILY) and name in OBJECT_CALLBACKS:
                    found[(function, name)].add(c)
                if (bases is None or bases & LOGGER_FAMILY) and name in LOGGER_CALLBACKS:
                    found[(function, name)].add(c)
        self._stored = dict(found)
        return self._stored

    def library_method(self, cls: str, name: str) -> bool:
        """Whether ``self.<name>()`` in a method of ``cls`` (a plain
        ``logging.Logger`` subclass: references._logger_class) runs the
        library's method: no class of the MRO of ``cls`` or of a subclass
        is decorated, written as a whole, has a base other than an in-scope
        class or ``logging.Logger``, defines or binds ``name`` in its body,
        is given it anywhere (ClassRecord) or binds it on an instance, and
        nothing stores ``name`` onto an object of unknown type or onto a
        ``logging`` class."""
        key = (cls, name)
        found = self._library.get(key)
        if found is None:
            found = self._library[key] = self._is_library(cls, name)
        return found

    def _is_library(self, cls: str, name: str) -> bool:
        if name in self.patched or "*" in self.patched:
            return False
        indexer = self.indexer
        scopes = indexer.class_scopes
        if cls not in scopes:
            return False
        family: set[str] = set()
        for c in (cls, *indexer._descendants.get(cls, ())):
            family.update(indexer._mro(c))
        if self._written is None:
            self._written = self.record.written()
        for c in sorted(family):
            cscope = scopes.get(c)
            if cscope is None or not cscope.plain or c in self._written:
                return False
            if not _logger_bases(cscope):
                return False
            given = self.record.given(c)
            if (
                name in cscope.members
                or name in cscope.bindings
                or name in given
                or "*" in given
                or name in self.record.methods.get(c, ())
            ):
                return False
        return True

    def fresh_record(self, cls: str) -> bool:
        """Whether the record ``makeRecord`` makes in a method of ``cls``
        (any library logger's, for "") is a fresh ``LogRecord`` holding what
        it was handed: no project record factory, and the library's
        ``makeRecord`` (for any logger: no class deriving from a ``logging``
        class may define or be given one)."""
        if self.factory:
            return False
        if cls:
            return self.library_method(cls, "makeRecord")
        name = "makeRecord"
        if name in self.patched or "*" in self.patched:
            return False
        for c, cscope in sorted(self.indexer.class_scopes.items()):
            if not self.logging_derived(c):
                continue
            given = self.record.given(c)
            if (
                name in cscope.members
                or name in cscope.bindings
                or name in given
                or "*" in given
                or name in self.record.methods.get(c, ())
            ):
                return False
        return True

    def logging_derived(self, cls: str) -> bool:
        """Whether a class of the in-scope MRO of ``cls`` names a base from
        ``logging`` through its module's imports, or one the index cannot
        tell (a dynamic base, a star import)."""
        bases = self._logging_bases(cls)
        return bases is None or bool(bases)

    def _logging_bases(self, cls: str) -> set[str] | None:
        """The ``logging`` bases the in-scope MRO of ``cls`` names through
        its modules' imports (``logging.Handler``), None when a base cannot
        be told (a dynamic base, a star import)."""
        if cls in self._derived:
            return self._derived[cls]
        found: set[str] | None = set()
        scopes = self.indexer.class_scopes
        for c in self.indexer._mro(cls):
            cscope = scopes.get(c)
            if cscope is None or found is None:
                continue
            module = cscope.module
            if not cscope.complete and module.star_imports:
                found = None
                continue
            names = cscope.base_names or [None] * len(cscope.base_chains)
            for given, named in zip(cscope.base_chains, names, strict=False):
                chain = given if given is not None else named
                if chain is None:
                    # A base no name says (``make_base()``): unknown when the
                    # module imports ``logging`` at all.
                    if not cscope.complete and any(
                        b.dotted == "logging" or b.dotted.startswith("logging.")
                        for b in module.imports.values()
                    ):
                        found = None
                        break
                    continue
                bindings = [module.imports.get(chain[0]), *module.alt_imports.get(chain[0], ())]
                for binding in bindings:
                    if binding is not None and (
                        binding.dotted == "logging" or binding.dotted.startswith("logging.")
                    ):
                        found.add(".".join([binding.dotted, *chain[1:]]))
        self._derived[cls] = found
        return found

    def _classes_of(self, what: str) -> list[str] | None:
        """The in-scope classes a registered object may be an instance of
        ([] for a library value), None when nothing says."""
        if what.startswith("var:"):
            variable = what[len("var:") :]
            classes = set(self._value_classes(variable) or ()) if self._value_classes else set()
            classes |= self.record.instances.get(variable, set())
            if classes:
                return sorted(classes)
            if self._third_party is not None and self._third_party(variable):
                return []
            return None
        if what.startswith(HELD_WHAT):
            return [what[len(HELD_WHAT) :].partition("|")[0]]
        if what.startswith("instance:"):
            return [what.rpartition(":")[2]]
        return None

    def _is_site(self, kind: str, ident: str) -> bool:
        """A log call through a module variable logs through the library
        unless its initialiser makes an instance of project classes none
        of which derives from a ``logging`` class; through an instance of an
        in-scope class (``INSTANCE_LOGGER``), only when the class or a
        subclass derives from one."""
        if kind == INSTANCE_LOGGER:
            family = [ident, *self.indexer._descendants.get(ident, ())]
            return any(self.logging_derived(c) for c in family)
        if kind != VARIABLE_LOGGER:
            return True
        classes = self._value_classes(ident) if self._value_classes is not None else None
        return classes is None or any(self.logging_derived(c) for c in classes)

    def passes(
        self,
        passes: list[Pass],
        callees: Callable[[Pass], list[str]],
        slot_params: Callable[[str, str, bool, str], list[str]],
    ) -> list[Pass]:
        """The pass records of every log site's callbacks (see the class
        docstring), and ``links``."""
        out = self.indexer.out
        if not out.log_calls:
            return []
        indexer = self.indexer
        symbols = indexer.index.symbols
        params = out.func_params
        scopes = indexer.class_scopes
        registered: set[tuple[str, str, str]] = set()
        through: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for caller, role, what, holder in sorted(out.log_registered):
            if what.startswith("param:"):
                through[(caller, what[len("param:") :])].add((role, holder))
            else:
                registered.add((role, what, holder))
        changed = bool(through)
        while changed:  # what is handed to a parameter registered in turn
            changed = False
            for record in passes:
                caller, what, _, name, slot, bound = record
                for callee in callees(record):
                    for param in slot_params(callee, slot, bound, name):
                        for role, holder in sorted(through.get((callee, param), ())):
                            if what.startswith("param:"):
                                key = (caller, what[len("param:") :])
                                if (role, holder) not in through[key]:
                                    through[key].add((role, holder))
                                    changed = True
                            else:
                                registered.add((role, what, holder))

        def instance_of(method: str) -> set[str]:
            container = symbols[method].container if method in symbols else None
            return {f"instance::{container}"} if container in scopes else set()

        targets: dict[str, set[str]] = defaultdict(set)  # callback -> receivers
        functions: set[tuple[str, str]] = set()
        by_name = self._by_name
        # Callback -> the method name it is stored under on a class (it runs
        # bound, its first parameter the instance).
        stored: dict[str, str] = {}
        for role, what, holder in sorted(registered):
            if role.startswith(STORED_ROLE):
                continue  # ``stored_methods``
            if what.startswith(FUNCTION_WHAT):
                function = what[len(FUNCTION_WHAT) :]
                if function in symbols:
                    functions.add((function, role))
                continue
            names = ROLE_CALLBACKS.get(role)
            if names is None:
                continue
            if what in (UNKNOWN_REGISTERED, ANY_REGISTERED):
                for name in sorted(names):
                    for method in by_name.get(name, ()):
                        targets[method] |= instance_of(method)
                continue
            receivers = {what, holder} - {""}
            classes = self._classes_of(what)
            if classes is None:
                for name in sorted(names):
                    for method in by_name.get(name, ()):
                        targets[method] |= receivers
                continue
            for cls in classes:
                if cls not in scopes:
                    continue
                for name in sorted(names):
                    hit = indexer.lookup_in_class(cls, name)
                    if isinstance(hit, Resolved) and not hit.detail and hit.symbol in params:
                        targets[hit.symbol] |= receivers
        # Callback -> the logger classes whose loggers it runs on.
        loggers: dict[str, set[str]] = defaultdict(set)
        for cls in sorted(scopes):
            bases = self._logging_bases(cls)
            if bases is not None and not bases:
                continue
            names: set[str] = set()
            if bases is None or bases - LOGGER_FAMILY:
                # A handler, filter or formatter: any instance of the class.
                for name in sorted(OBJECT_CALLBACKS):
                    hit = indexer.lookup_in_class(cls, name)
                    if isinstance(hit, Resolved) and not hit.detail and hit.symbol in params:
                        targets[hit.symbol].add(f"instance::{cls}")
            if bases is None or bases & LOGGER_FAMILY:
                names = set(LOGGER_CALLBACKS)
            for name in sorted(names):
                hit = indexer.lookup_in_class(cls, name)
                if isinstance(hit, Resolved) and not hit.detail and hit.symbol in params:
                    loggers[hit.symbol].add(cls)
        for (function, name), classes in sorted(self.stored_methods().items()):
            stored[function] = name
            for c in sorted(classes):
                bases = self._logging_bases(c)
                if (bases is None or bases - LOGGER_FAMILY) and name in OBJECT_CALLBACKS:
                    targets[function].add(f"instance::{c}")
                if (bases is None or bases & LOGGER_FAMILY) and name in LOGGER_CALLBACKS:
                    loggers[function].add(c)
        if not targets and not functions and not loggers:
            return []
        contents: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for caller, site, what in out.log_contents:
            contents[caller].add((site, what))
        sites: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for caller, kind, ident, _ in out.log_calls:
            if self._is_site(kind, ident):
                sites[caller].add((kind, ident))
        found: list[Pass] = []
        any_position = f"{ANY_POSITION}0"
        for caller in sorted(sites):
            kinds = sites[caller]
            markers = sorted(
                {f"{RECORD}{ident}" if kind == SELF_LOGGER else RECORD for kind, ident in kinds}
            )
            records: set[str] = set()  # what the record holds, as record callbacks get it
            direct: set[str] = set()  # what it is made of
            for site, what in contents.get(caller, ()):
                if site == RECORD_SITE or what.startswith(HELD_WHAT):
                    records.add(what)
                    if site == ARGS_SITE:
                        direct.add(what)
                else:
                    records.update(f"{HELD_WHAT}{marker}|{what}" for marker in markers)
                    direct.add(what)
            own = {f"var:{ident}" for kind, ident in kinds if kind == VARIABLE_LOGGER}
            info = params.get(caller)
            if info is not None and info.bound and info.positional:
                if any(kind == SELF_LOGGER for kind, _ in kinds):
                    own.add(f"param:{info.positional[0]}")
            linked: set[str] = set()
            # A logger class's callbacks run on the logger logged through:
            # the variable or ``self`` when the site names it, any instance
            # of the class when it does not (``logging.getLogger(n).info``).
            unnamed = not own or any(
                kind in (LIBRARY, ANY_LOGGER, INSTANCE_LOGGER) for kind, _ in kinds
            )
            on: dict[str, set[str]] = defaultdict(set)
            for method, receivers in targets.items():
                on[method] |= receivers
            for method, classes in loggers.items():
                on[method] |= own
                if unnamed:
                    on[method] |= {f"instance::{cls}" for cls in classes}
            for method, receivers in sorted(on.items()):
                name = stored.get(method) or symbols[method].name
                bound = method in stored or params[method].bound
                linked.add(method)
                if name in DIRECT_CALLBACKS:
                    for what in sorted(direct):
                        found.append((caller, what, method, name, any_position, bound))
                        found.append((caller, what, method, name, ANY_KEYWORD, bound))
                else:
                    for what in sorted(records):
                        found.append((caller, what, method, name, any_position, bound))
                for what in sorted(receivers):
                    found.append((caller, what, method, name, RECEIVER, True))
            for function, role in sorted(functions):
                name = symbols[function].name
                linked.add(function)
                if role == FUNCTION:
                    whats = records | direct
                elif role == FACTORY:
                    whats = direct
                else:
                    whats = records
                for what in sorted(whats):
                    found.append((caller, what, function, name, any_position, False))
                    found.append((caller, what, function, name, ANY_KEYWORD, False))
                if role == FUNCTION:  # a method of a library class: its ``self``
                    for what in sorted(own):
                        found.append((caller, what, function, name, "0", False))
            self.links[caller] = linked
        return found


def _logger_bases(cscope: ClassScope) -> bool:
    """Whether every base of a class is an in-scope class or names
    ``logging.Logger`` (``LOGGER_CLASSES``) through its module's imports."""
    module = cscope.module
    external = 0
    for chain in cscope.base_chains:
        if chain is None:
            return False
        binding = module.imports.get(chain[0])
        if (
            binding is not None
            and not module.alt_imports.get(chain[0])
            and ".".join([binding.dotted, *chain[1:]]) in LOGGER_CLASSES
        ):
            continue
        external += 1
    return external <= len(cscope.bases)


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
    run its methods, which may change anything it holds. A library logger's
    record (``RECORD``) is a ``LogRecord``, which runs no code binding an
    attribute, when ``_LoggingModel.fresh_record`` says it is one (audit
    round 8, V1)."""

    def __init__(self, indexer: Resolver, logging: _LoggingModel) -> None:
        self.indexer = indexer
        self.logging = logging
        self.values: dict[str, set[tuple[str, bool]]] = defaultdict(set)
        for owner, attr, ok in indexer.out.attr_values:
            self.values[attr].add((owner, ok))
        self._family: dict[str, set[str] | None] = {}

    def family(self, cls: str) -> set[str] | None:
        """The family of ``cls`` (its MRO and its in-scope subclasses'), or
        None when one of them may make or customise the instance, or ``cls``
        is a variable called as a class (``Alias = Box``: U2), whose classes
        are not known here (audit round 7, U33)."""
        if cls not in self._family:
            indexer = self.indexer
            if cls not in indexer.class_scopes:
                self._family[cls] = None
                return None
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
        if cls.startswith(RECORD):
            if not self.logging.fresh_record(cls[len(RECORD) :]):
                return True
            return any(
                flavor[:1] != "="
                or flavor[1:] == "*"
                or (flavor[1:].startswith("__") and flavor[1:].endswith("__"))
                for flavor in how
            )
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

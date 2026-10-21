"""Per-module facts the first pass records and the second pass reads, and
their JSON codec for the module cache."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from diffcone.indexer.scopes import ClassScope, ImportBinding, ModuleScope, Scope
from diffcone.indexer.syntax import _digest
from diffcone.model import (
    Edge,
    EdgeKind,
    ExternalReference,
    Symbol,
    SymbolKind,
    UnresolvedKind,
    UnresolvedReference,
)


@dataclass
class _FuncParams:
    positional: list[str]  # including self/cls for bound methods
    bound: bool
    defaults: dict[str, tuple[str, ...] | None]


_NOTHING: Any = object()  # a call site passes nothing for the parameter


@dataclass
class _CallSite:
    positional: list[tuple[str, ...] | None]  # candidates per positional argument
    keywords: dict[str, tuple[str, ...] | None]
    unbounded: bool  # *args / **kwargs at the call site
    receiver_bound: bool  # ``obj.m(...)`` / ``self.m(...)``: self is implicit
    # The symbol the call sits in: a by-name import is attributed to the
    # caller that named the module, not to the helper that imports it.
    caller: str
    # Per argument, the caller's own parameter it is, when it is exactly
    # that: ``skip_if_no(name)`` passes its parameter to the importer, so the
    # answer is one level further out.
    positional_params: list[str | None] = field(default_factory=list)
    keyword_params: dict[str, str | None] = field(default_factory=dict)
    # The class each argument is an instance of, where the argument says so
    # (``C()`` or ``C``); None when it does not. Used to bound what a
    # ``getattr`` on a parameter may read.
    positional_classes: list[str | None] = field(default_factory=list)
    keyword_classes: dict[str, str | None] = field(default_factory=dict)
    # The function an argument came out of (``make()``, or a local assigned
    # once from it): its return class answers once every module is indexed.
    positional_sources: list[str | None] = field(default_factory=list)
    keyword_sources: dict[str, str | None] = field(default_factory=dict)

    def _argument(self, param: str, info: _FuncParams, positional: list, keywords: dict) -> Any:
        """The entry of one per-argument table (``positional``/``keywords``)
        for the argument this site passes for ``param``: by keyword, else by
        position, shifted by the implicit receiver of a bound method.
        _NOTHING when the site passes nothing for it."""
        if param in keywords:
            return keywords[param]
        if param in info.positional:
            index = info.positional.index(param)
            if info.bound and self.receiver_bound:
                index -= 1
            if 0 <= index < len(positional):
                return positional[index]
        return _NOTHING

    def param_for(self, param: str, info: _FuncParams) -> str | None:
        """The caller's parameter passed for ``param`` here, if it is one."""
        if self.unbounded:
            return None
        found = self._argument(param, info, self.positional_params, self.keyword_params)
        return None if found is _NOTHING else found

    def class_for(self, param: str, info: _FuncParams) -> str | None:
        """The class of the argument passed for ``param`` here, if it says."""
        if self.unbounded:
            return None
        found = self._argument(param, info, self.positional_classes, self.keyword_classes)
        return None if found is _NOTHING else found

    def value_for(self, param: str, info: _FuncParams) -> tuple[str, ...] | None:
        """The strings passed for ``param`` here (its default when nothing
        is), or None when unbounded."""
        if self.unbounded:
            return None
        found = self._argument(param, info, self.positional, self.keywords)
        return info.defaults.get(param) if found is _NOTHING else found


@dataclass
class _ParamDynamic:
    function: str
    param: str  # a parameter name, or an instance attribute name with self_class
    # "getattr" | "import" | "script" | "code" (text run as code) | "format"
    # (a str.format template) | "runner" (a pytester session's arguments) |
    # "template" (code text with one field the parameter fills; ``base``
    # holds the text, scripts.HOLE_NAME standing for the field)
    kind: str
    base: list[str] | None  # receiver chain for getattr, when it is a name chain
    scope: Scope
    detail: str
    self_class: str = ""  # set when the name is ``self.<param>`` of this class


@dataclass
class _AttrWrite:
    """A write of ``self.<attr>`` in a method of ``cls``. ``binding`` is what
    an ``__init__`` assignment binds (``["param", name]``, ``["strings",
    [...]]``, ``["symbol", id]``), or what an assignment in any method makes
    (``["instance", class id]``, ``["external"]``, ``["factory", function
    id]``: writes.py); None for any other write."""

    cls: str
    attr: str
    method: str
    binding: list | None


@dataclass
class _AttrRef:
    """``self.<attr>[.rest]`` read in ``source`` where no class in the MRO
    defines ``attr``: resolved to the bound symbols when there are some."""

    source: str
    cls: str
    attr: str
    rest: list[str]
    chain: str


@dataclass
class _Output:
    """Everything a resolution pass writes. Pass 2 runs per module against a
    fresh instance so a module's contribution can be cached and merged; the
    rest of the indexer writes to the global one backed by the index."""

    edges: set[Edge] = field(default_factory=set)
    unresolved: set[UnresolvedReference] = field(default_factory=set)
    external: set[ExternalReference] = field(default_factory=set)
    call_sites: dict[str, list[_CallSite]] = field(default_factory=lambda: defaultdict(list))
    escapes: set[str] = field(default_factory=set)
    func_params: dict[str, _FuncParams] = field(default_factory=dict)
    # Per function: the classes its ``return`` statements yield, when every
    # one of them yields a class. A factory is how an object reaches code
    # that reads attributes off it by a name nothing resolves.
    returns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Functions whose every ``return`` yields what a third-party call returns
    # (``return logging.getLogger(name)``; diffcone.indexer.writes).
    external_returns: set[str] = field(default_factory=set)
    param_dynamics: list[_ParamDynamic] = field(default_factory=list)
    attr_writes: list[_AttrWrite] = field(default_factory=list)
    # (class id, attribute) pairs whose value cannot be bounded; the class is
    # "" for a write through a receiver of unknown type, the attribute "*"
    # for every attribute.
    attr_unbound: set[tuple[str, str]] = field(default_factory=set)
    attr_refs: list[_AttrRef] = field(default_factory=list)
    reflection: set[tuple[str, str]] = field(default_factory=set)
    class_attributes: dict[str, dict[str, str]] = field(default_factory=dict)
    class_bases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    open_classes: set[str] = field(default_factory=set)
    doc_decorated: set[str] = field(default_factory=set)
    # (decorated symbol, decorator symbol or "", receiver symbol or ""): who
    # may hold the decorated function afterwards (see Indexer._registrations).
    decorations: set[tuple[str, str, str]] = field(default_factory=set)
    # External modules in-scope code writes attributes onto (``logging.x =
    # ...``, ``setattr(logging, ...)``, ``patch("logging.x", ...).start()``;
    # not a store undone after the test, indexer/patches.py), as (module,
    # the symbol writing), and lookups by a name nothing
    # bounds on an external module: (symbol, module, "dynamic" or
    # "reflection", detail). Such a lookup can reach in-scope code only
    # through a write onto that module, which only the whole tree can tell
    # (Indexer._external_lookups).
    external_writes: set[tuple[str, str]] = field(default_factory=set)
    external_lookups: set[tuple[str, str, str, str]] = field(default_factory=set)
    # (symbol, dotted name): an attribute chain of a module outside the
    # roots the symbol reads, by each canonical name it may stand for
    # (``json.mode``, ``os.environ.get``): matched after pass 2 against the
    # process writes (Indexer._external_state_edges; audit round 7, U13).
    external_reads: set[tuple[str, str]] = field(default_factory=set)
    # Dynamic imports whose name this module's own literal tables bound:
    # (symbol, detail, the names they give). Which code may change those
    # tables only the whole tree tells (SourceIndex.table_imports).
    table_imports: set[tuple[str, str, tuple[str, ...]]] = field(default_factory=set)
    # Writes through a parameter or a receiver (diffcone.indexer.writes):
    # (function, parameter) written in place, and what calls are handed.
    param_writes: set[tuple[str, str]] = field(default_factory=set)
    passes: set[tuple[str, str, str, str, str, bool]] = field(default_factory=set)
    # Caches outside module variables (diffcone.indexer.writes, audit round
    # 5, R22): stores into state outliving the call (SourceIndex.stores),
    # the method calls on such state, resolved after pass 2
    # (``computed_passes``: (caller, where, callee, name)), a parameter's
    # attribute handed to a callee (``held_passes``: (caller, callee, name,
    # slot, attribute, bound); resolved into SourceIndex.handed), and the
    # functions whose decorator may keep a result (SourceIndex.memoized).
    stores: set[tuple[str, str]] = field(default_factory=set)
    computed_passes: set[tuple[str, str, str, str]] = field(default_factory=set)
    held_passes: set[tuple[str, str, str, str, str, bool]] = field(default_factory=set)
    handed: set[tuple[str, str, str]] = field(default_factory=set)
    memoized: set[str] = field(default_factory=set)
    # Functions and methods a decorator not known to call them afresh each
    # time wraps (``functools.cache``, ``lru_cache``, a project decorator
    # returning a wrapper, one from outside the index): what a call returns
    # may be what an earlier call returned, so a write into it is a write of
    # the function's state (Indexer._result_writes; audit round 7, U7).
    kept_results: set[str] = field(default_factory=set)
    # (symbol, path suffix) of each ``.py`` file a string names
    # (diffcone.indexer.scripts), matched against the files after pass 2.
    script_paths: set[tuple[str, str]] = field(default_factory=set)
    # (symbol, path suffix or directory, runner) of each test file a test
    # runner is given (``scripts.test_file``): run whole after pass 2.
    test_runs: set[tuple[str, str, str]] = field(default_factory=set)
    # (kind, id): a class whose instance or a function whose result is
    # handed to code the analysis does not follow (``references.py``,
    # ``_handed_on``); the classes join ``SourceIndex.escaped_classes``.
    handed_instances: set[tuple[str, str]] = field(default_factory=set)
    # State reached through what a function returns (references.py,
    # ``_rooted_write``; audit round 6, T7), joined after pass 2
    # (Indexer._result_writes): (function, module variable, class, function
    # or ``<result>:`` callee whose state it may return), and (writer,
    # callee, the writer's module for top-level code or "") for a write in
    # place into what a call of an in-scope function returns.
    state_returns: set[tuple[str, str]] = field(default_factory=set)
    result_writes: set[tuple[str, str, str]] = field(default_factory=set)
    # The (function, root) of ``state_returns`` some ``return`` hands back as
    # the object itself, not only in a new container of it (``return
    # dict(_cached())``); and the result writes into the returned object
    # itself (a local bound to the call, the call's own item), which change
    # nothing a copy was made from (Indexer._result_writes; audit round 7,
    # U7).
    whole_returns: set[tuple[str, str]] = field(default_factory=set)
    direct_result_writes: set[tuple[str, str, str]] = field(default_factory=set)
    # (state, writer): a ``mutated_by`` edge of a change in place of what a
    # module variable (or class, or function) holds, kept apart until every
    # module is known: a variable whose value cannot change in place gets
    # none (audit round 6, T20; Indexer._value_writes). Rebinding a variable
    # (``mod.X = v``, ``global X; X = v``) and writing a module object are
    # edges of their own.
    value_writes: set[tuple[str, str]] = field(default_factory=set)
    # (module variable, what a binding of it holds): ``class:<id>`` an
    # instance of an in-scope class (or the class), ``call:<id>`` what an
    # in-scope function returns, ``var:<id>`` another module variable's
    # value; joined after pass 2 (Indexer._instance_classes; audit round 6,
    # T21).
    instance_bindings: set[tuple[str, str]] = field(default_factory=set)
    # (state, writer): ``value_writes`` made only by handing the variable to
    # a method writing its receiver (writes.apply_writes, after pass 2): an
    # edge of the variable, but none of the class it holds an instance of,
    # whose state the method's own writes of ``self`` reach (T21).
    receiver_writes: set[tuple[str, str]] = field(default_factory=set)
    # (class, writer, how): a write into an instance of an in-scope class
    # the code made (``s = Store(); s._items.append(x)``, ``new =
    # cls.__new__(cls)``), or into part of one: a ``mutated_by`` edge of the
    # class and its in-scope bases when one of them holds state. ``how`` is
    # "" for a write in place, ``.`` and the attribute for one reached
    # through that attribute of the instance (none when the attribute is the
    # instance's own), or how the instance was made (references.FRESH) for an
    # attribute bound on it, which changes the instance alone when nothing
    # but ``object.__new__`` made it (Indexer._instance_writes; audit round
    # 6, T22).
    instance_writes: set[tuple[str, str, str]] = field(default_factory=set)
    # (class, attribute, writer): a change in place of what a class
    # attribute holds (``Field.DAYS``, ``self._SECS_MAP``): a ``mutated_by``
    # edge of the class unless the attribute is bound once to a value that
    # cannot change in place and nothing binds it anew (references.ATTR,
    # Indexer._class_attribute_writes; audit round 6, T24).
    attr_value_writes: set[tuple[str, str, str]] = field(default_factory=set)
    # (class, attribute or ``*``): classes their own module's top-level code
    # writes (an edge the module's hash covers, but a class attribute may be
    # bound anew: ``Arrow.min = ...`` binds ``min``; T24).
    own_class_writes: set[tuple[str, str]] = field(default_factory=set)
    # (attribute or ``*``, writer, the writer's module for code its hash
    # covers or ""): a write onto the class of an object of unknown type
    # (``type(o).x = v``, ``o.__class__.items.append(x)``,
    # ``setattr(type(o), name, v)``): a write of every in-scope class that
    # may own the attribute (writes.AnyClassOwners; audit round 6, T25).
    any_class_writes: set[tuple[str, str, str]] = field(default_factory=set)
    # (class, attribute or ``*``, writer, kind, first, second): an attribute
    # bound on a fresh instance of the class whose constructor was handed
    # what the fact (apply_fact) names; the fact holds when
    # binding the attribute may run code of the class (writes._HeldSafety;
    # audit round 6, T23).
    held_binds: set[tuple[str, str, str, str, str, str]] = field(default_factory=set)
    # (class of a ``self``/``cls`` receiver or "", attribute or ``*``,
    # whether the value bound is of a builtin type that cannot change in
    # place): every attribute store, ``setattr``, ``__dict__`` write (T23).
    attr_values: set[tuple[str, str, bool]] = field(default_factory=set)
    # (function, parameter, how): how the function writes a parameter
    # (``param_writes``): ``=<attribute>`` binds or deletes one of its
    # attributes (``=*``: a name known at run time), ``+<attribute>``
    # augments one with a value of a builtin type that cannot change in
    # place, ``*`` anything else (T23).
    param_flavors: set[tuple[str, str, str]] = field(default_factory=set)
    # The ``passes`` handing a parameter itself on (not an item or an
    # attribute of it): the callee's writes are the caller's (T23).
    whole_passes: set[tuple[str, str, str, str, str, bool]] = field(default_factory=set)
    # (symbol, what): writes of process-global state (indexer/process.py).
    process_writes: set[tuple[str, str]] = field(default_factory=set)
    # Module variables some code binds anew (``global X; X = ...`` in a
    # function, ``mod.X = ...`` elsewhere): what they hold may be any kind of
    # object (Indexer._write_only_references).
    rebound: set[str] = field(default_factory=set)
    # (module, kind, target): what a module's own code does only under
    # ``if __name__ == "__main__":`` (SourceIndex.main_guarded).
    main_guarded: set[tuple[str, str, str]] = field(default_factory=set)

    def merge(self, other: _Output) -> None:
        self.edges |= other.edges
        self.unresolved |= other.unresolved
        self.external |= other.external
        for function, sites in other.call_sites.items():
            self.call_sites[function].extend(sites)
        self.escapes |= other.escapes
        self.func_params.update(other.func_params)
        self.returns.update(other.returns)
        self.external_returns |= other.external_returns
        self.param_dynamics.extend(other.param_dynamics)
        self.attr_writes.extend(other.attr_writes)
        self.attr_unbound |= other.attr_unbound
        self.attr_refs.extend(other.attr_refs)
        self.reflection |= other.reflection
        self.class_attributes.update(other.class_attributes)
        self.class_bases.update(other.class_bases)
        self.open_classes |= other.open_classes
        self.doc_decorated |= other.doc_decorated
        self.decorations |= other.decorations
        self.external_writes |= other.external_writes
        self.external_reads |= other.external_reads
        self.external_lookups |= other.external_lookups
        self.table_imports |= other.table_imports
        self.param_writes |= other.param_writes
        self.passes |= other.passes
        self.stores |= other.stores
        self.computed_passes |= other.computed_passes
        self.held_passes |= other.held_passes
        self.handed |= other.handed
        self.memoized |= other.memoized
        self.kept_results |= other.kept_results
        self.script_paths |= other.script_paths
        self.test_runs |= other.test_runs
        self.handed_instances |= other.handed_instances
        self.state_returns |= other.state_returns
        self.result_writes |= other.result_writes
        self.whole_returns |= other.whole_returns
        self.direct_result_writes |= other.direct_result_writes
        self.value_writes |= other.value_writes
        self.instance_bindings |= other.instance_bindings
        self.receiver_writes |= other.receiver_writes
        self.instance_writes |= other.instance_writes
        self.attr_value_writes |= other.attr_value_writes
        self.own_class_writes |= other.own_class_writes
        self.any_class_writes |= other.any_class_writes
        self.held_binds |= other.held_binds
        self.attr_values |= other.attr_values
        self.param_flavors |= other.param_flavors
        self.whole_passes |= other.whole_passes
        self.process_writes |= other.process_writes
        self.rebound |= other.rebound
        self.main_guarded |= other.main_guarded


def _tuples(value: list | None) -> tuple[str, ...] | None:
    return None if value is None else tuple(value)


def _scope_to_dict(scope: Scope) -> dict:
    """The part of a function scope that deferred parameter-dynamic expansion
    resolves names against (module, imports, locals, self, aliases): every
    field ``Resolver._lookup_base`` and ``DynamicBounds._receiver_classes``
    read. The rest (literal bindings, parameters, ``method``) is read only
    while the module is resolved, never after (tests/test_cache.py holds
    the list)."""
    return {
        "module": scope.module.name,
        "local_imports": {k: [b.module, b.attr] for k, b in scope.local_imports.items()},
        "local_alt_imports": {
            k: [[b.module, b.attr] for b in v] for k, v in sorted(scope.local_alt_imports.items())
        },
        "locals": sorted(scope.locals),
        "self_name": scope.self_name,
        "self_class": scope.self_class,
        # ``cls`` rather than ``self`` (DynamicBounds._receiver_classes;
        # audit round 7, U21).
        "self_is_class": scope.self_is_class,
        "class_members": scope.class_members,
        "param_aliases": scope.param_aliases,
    }


def _scope_from_dict(data: dict, scopes: dict[str, ModuleScope]) -> Scope:
    return Scope(
        module=scopes[data["module"]],
        local_imports={k: ImportBinding(m, a) for k, (m, a) in data["local_imports"].items()},
        local_alt_imports={
            k: [ImportBinding(m, a) for m, a in v] for k, v in data["local_alt_imports"].items()
        },
        locals=set(data["locals"]),
        self_name=data["self_name"],
        self_class=data["self_class"],
        self_is_class=bool(data["self_is_class"]),
        class_members=dict(data["class_members"]),
        param_aliases=dict(data["param_aliases"]),
    )


def apply_fact(out: _Output, writer: str, fact: tuple[str, ...]) -> None:
    """Record what ``_ReferenceCollector._root_facts`` says a write by
    ``writer`` changes."""
    kind, first, second = fact[0], fact[1], fact[2]
    if kind == "value":
        out.value_writes.add((first, writer))
    elif kind == "attribute":
        out.attr_value_writes.add((first, second, writer))
    elif kind == "instance":
        out.instance_writes.add((first, writer, second))
    elif kind == "result":
        out.result_writes.add((writer, first, second))
    elif kind == "direct_result":
        out.direct_result_writes.add((writer, first, second))
    elif kind == "own":
        out.own_class_writes.add((first, second))
    elif kind == "anyclass":
        out.any_class_writes.add((first, writer, second))


def _output_to_dict(out: _Output) -> dict:
    return {
        "edges": [[e.source, e.target, e.kind, e.detail] for e in sorted(out.edges)],
        "unresolved": [[u.symbol, u.kind, u.name, u.detail] for u in sorted(out.unresolved)],
        "external": [[x.symbol, x.module] for x in sorted(out.external)],
        "call_sites": {
            f: [
                [
                    s.positional,
                    s.keywords,
                    s.unbounded,
                    s.receiver_bound,
                    s.positional_classes,
                    s.keyword_classes,
                    s.positional_sources,
                    s.keyword_sources,
                    s.caller,
                    s.positional_params,
                    s.keyword_params,
                ]
                for s in sites
            ]
            for f, sites in out.call_sites.items()
        },
        "escapes": sorted(out.escapes),
        "reflection": sorted(list(r) for r in out.reflection),
        "class_bases": {c: list(b) for c, b in sorted(out.class_bases.items())},
        "open_classes": sorted(out.open_classes),
        "doc_decorated": sorted(out.doc_decorated),
        "decorations": sorted(list(d) for d in out.decorations),
        "external_writes": sorted(list(w) for w in out.external_writes),
        "external_reads": sorted(list(r) for r in out.external_reads),
        "external_lookups": sorted(list(x) for x in out.external_lookups),
        "table_imports": sorted([s, d, list(n)] for s, d, n in out.table_imports),
        "process_writes": sorted(list(w) for w in out.process_writes),
        "rebound": sorted(out.rebound),
        "main_guarded": sorted(list(m) for m in out.main_guarded),
        "class_attributes": {
            c: dict(sorted(a.items())) for c, a in sorted(out.class_attributes.items())
        },
        "returns": {f: sorted(c) for f, c in sorted(out.returns.items())},
        "external_returns": sorted(out.external_returns),
        "func_params": {f: [p.positional, p.bound, p.defaults] for f, p in out.func_params.items()},
        "param_dynamics": [
            [
                pd.function,
                pd.param,
                pd.kind,
                pd.base,
                _scope_to_dict(pd.scope),
                pd.detail,
                pd.self_class,
            ]
            for pd in out.param_dynamics
        ],
        "attr_writes": [[w.cls, w.attr, w.method, w.binding] for w in out.attr_writes],
        "attr_unbound": sorted(out.attr_unbound),
        "attr_refs": [[r.source, r.cls, r.attr, r.rest, r.chain] for r in out.attr_refs],
        "param_writes": sorted(list(w) for w in out.param_writes),
        "passes": sorted(list(p) for p in out.passes),
        "stores": sorted(list(s) for s in out.stores),
        "computed_passes": sorted(list(c) for c in out.computed_passes),
        "held_passes": sorted(list(h) for h in out.held_passes),
        "memoized": sorted(out.memoized),
        "kept_results": sorted(out.kept_results),
        "script_paths": sorted(list(p) for p in out.script_paths),
        "test_runs": sorted(list(t) for t in out.test_runs),
        "handed_instances": sorted(list(h) for h in out.handed_instances),
        "state_returns": sorted(list(r) for r in out.state_returns),
        "result_writes": sorted(list(w) for w in out.result_writes),
        "whole_returns": sorted(list(r) for r in out.whole_returns),
        "direct_result_writes": sorted(list(w) for w in out.direct_result_writes),
        "value_writes": sorted(list(w) for w in out.value_writes),
        "instance_bindings": sorted(list(b) for b in out.instance_bindings),
        "instance_writes": sorted(list(w) for w in out.instance_writes),
        "attr_value_writes": sorted(list(w) for w in out.attr_value_writes),
        "own_class_writes": sorted(out.own_class_writes),
        "any_class_writes": sorted(list(w) for w in out.any_class_writes),
        "held_binds": sorted(list(b) for b in out.held_binds),
        "attr_values": sorted(list(v) for v in out.attr_values),
        "param_flavors": sorted(list(f) for f in out.param_flavors),
        "whole_passes": sorted(list(p) for p in out.whole_passes),
    }


def _output_from_dict(data: dict, scopes: dict[str, ModuleScope]) -> _Output:
    out = _Output()
    out.edges = {Edge(s, t, EdgeKind(k), d) for s, t, k, d in data["edges"]}
    out.unresolved = {
        UnresolvedReference(s, UnresolvedKind(k), n, d) for s, k, n, d in data["unresolved"]
    }
    out.external = {ExternalReference(*x) for x in data["external"]}
    for f, sites in data["call_sites"].items():
        out.call_sites[f] = [
            _CallSite(
                positional=[_tuples(v) for v in positional],
                keywords={k: _tuples(v) for k, v in keywords.items()},
                unbounded=unbounded,
                receiver_bound=receiver_bound,
                positional_classes=list(positional_classes),
                keyword_classes=dict(keyword_classes),
                positional_sources=list(positional_sources),
                keyword_sources=dict(keyword_sources),
                caller=caller,
                positional_params=list(positional_params),
                keyword_params=dict(keyword_params),
            )
            for (
                positional,
                keywords,
                unbounded,
                receiver_bound,
                positional_classes,
                keyword_classes,
                positional_sources,
                keyword_sources,
                caller,
                positional_params,
                keyword_params,
            ) in sites
        ]
    out.escapes = set(data["escapes"])
    out.reflection = {(s, d) for s, d in data["reflection"]}
    out.class_attributes = {c: dict(a) for c, a in data["class_attributes"].items()}
    out.class_bases = {c: tuple(b) for c, b in data["class_bases"].items()}
    out.open_classes = set(data["open_classes"])
    out.doc_decorated = set(data["doc_decorated"])
    out.decorations = {(a, b, c) for a, b, c in data["decorations"]}
    out.external_writes = {(m, w) for m, w in data["external_writes"]}
    out.external_reads = {(s, d) for s, d in data["external_reads"]}
    out.external_lookups = {(a, b, c, d) for a, b, c, d in data["external_lookups"]}
    out.table_imports = {(s, d, tuple(n)) for s, d, n in data["table_imports"]}
    out.process_writes = {(s, w) for s, w in data["process_writes"]}
    out.rebound = set(data["rebound"])
    out.main_guarded = {(a, b, c) for a, b, c in data["main_guarded"]}
    out.returns = {f: tuple(c) for f, c in data["returns"].items()}
    out.external_returns = set(data["external_returns"])
    out.func_params = {
        f: _FuncParams(
            positional=list(positional),
            bound=bound,
            defaults={k: _tuples(v) for k, v in defaults.items()},
        )
        for f, (positional, bound, defaults) in data["func_params"].items()
    }
    out.param_dynamics = [
        _ParamDynamic(
            function, param, kind, base, _scope_from_dict(scope, scopes), detail, self_class
        )
        for function, param, kind, base, scope, detail, self_class in data["param_dynamics"]
    ]
    out.attr_writes = [_AttrWrite(*w) for w in data["attr_writes"]]
    out.attr_unbound = {(c, a) for c, a in data["attr_unbound"]}
    out.attr_refs = [_AttrRef(*r) for r in data["attr_refs"]]
    out.param_writes = {(f, p) for f, p in data["param_writes"]}
    out.passes = {(a, b, c, d, e, bool(f)) for a, b, c, d, e, f in data["passes"]}
    out.stores = {(a, b) for a, b in data["stores"]}
    out.computed_passes = {(a, b, c, d) for a, b, c, d in data["computed_passes"]}
    out.held_passes = {(a, b, c, d, e, bool(f)) for a, b, c, d, e, f in data["held_passes"]}
    out.memoized = set(data["memoized"])
    out.kept_results = set(data["kept_results"])
    out.script_paths = {(a, b) for a, b in data["script_paths"]}
    out.test_runs = {(a, b, c) for a, b, c in data["test_runs"]}
    out.handed_instances = {(a, b) for a, b in data["handed_instances"]}
    out.state_returns = {(f, r) for f, r in data["state_returns"]}
    out.result_writes = {(w, c, m) for w, c, m in data["result_writes"]}
    out.whole_returns = {(f, r) for f, r in data["whole_returns"]}
    out.direct_result_writes = {(w, c, m) for w, c, m in data["direct_result_writes"]}
    out.value_writes = {(v, w) for v, w in data["value_writes"]}
    out.instance_bindings = {(v, o) for v, o in data["instance_bindings"]}
    out.instance_writes = {(c, w, h) for c, w, h in data["instance_writes"]}
    out.attr_value_writes = {(c, a, w) for c, a, w in data["attr_value_writes"]}
    out.own_class_writes = {(c, a) for c, a in data["own_class_writes"]}
    out.any_class_writes = {(a, w, m) for a, w, m in data["any_class_writes"]}
    out.held_binds = {(c, a, w, k, f, s) for c, a, w, k, f, s in data["held_binds"]}
    out.attr_values = {(c, a, bool(ok)) for c, a, ok in data["attr_values"]}
    out.param_flavors = {(f, p, h) for f, p, h in data["param_flavors"]}
    out.whole_passes = {(c, w, e, n, s, bool(b)) for c, w, e, n, s, b in data["whole_passes"]}
    return out


def symbol_from_dict(data: dict) -> Symbol:
    """A Symbol from its ``vars``/``asdict`` form, as JSON gives it back
    (lists where the dataclass holds tuples)."""
    data = dict(data)
    data["line_ranges"] = tuple(tuple(r) for r in data["line_ranges"])
    data["imports"] = tuple(data["imports"])
    data["import_layout"] = tuple(data["import_layout"])
    data["statement_layout"] = tuple(data["statement_layout"])
    data["annotation_names"] = tuple(data["annotation_names"])
    data["kind"] = SymbolKind(data["kind"])
    return Symbol(**data)


def _facts_to_dict(
    scope: ModuleScope, symbols: list[Symbol], classes: list[ClassScope], edges: set[Edge]
) -> dict:
    """A module's first-pass output: a pure function of its file (given its
    module name), so it is cached by content. ``env`` digests the part other
    modules' resolution can observe (names, kinds, class members); it feeds
    the environment fingerprint that keys second-pass outputs."""
    record = {
        "imports": {k: [b.module, b.attr] for k, b in scope.imports.items()},
        "alt_imports": {
            k: [[b.module, b.attr] for b in v] for k, v in sorted(scope.alt_imports.items())
        },
        "star_imports": list(scope.star_imports),
        "bindings": sorted(scope.bindings),
        "members": dict(scope.members),
        "variables": dict(scope.variables),
        "literal_names": {
            k: (list(v) if v is not None else None) for k, v in scope.literal_names.items()
        },
        "uses": [r.to_list() for r in scope.uses],
        "containers": sorted(scope.containers),
        "immutables": {k: list(v) for k, v in sorted(scope.immutables.items())},
        "literal_sources": {k: list(v) for k, v in scope.literal_sources.items()},
        "lazy": {
            k: [w, list(n), list(d)] for k, (w, n, d) in sorted(scope.lazy_candidates.items())
        },
        "symbols": [dict(vars(s)) for s in symbols],  # flat and frozen: no deep copy needed
        "classes": [
            {
                "id": c.id,
                "enclosing": c.enclosing.id if c.enclosing is not None else None,
                "members": dict(c.members),
                "bindings": sorted(c.bindings),
                "base_chains": c.base_chains,
                "base_names": c.base_names,
                "plain": c.plain,
                "kept": c.kept,
                "declared": sorted(c.declared),
                "tables": sorted(c.tables),
                "match_args": list(c.match_args) if c.match_args is not None else None,
                "stateless": list(c.stateless) if c.stateless is not None else None,
                "immutables": (
                    {k: list(v) for k, v in c.immutables.items()}
                    if c.immutables is not None
                    else None
                ),
            }
            for c in classes
        ],
        "edges": [[e.source, e.target, e.kind, e.detail] for e in sorted(edges)],
    }
    env = [
        scope.name,
        record["imports"],
        record["alt_imports"],
        record["star_imports"],
        record["bindings"],
        record["members"],
        record["variables"],
        record["uses"],
        record["containers"],
        [
            [
                s.id,
                s.kind,
                s.module,
                s.reads_docstrings,
                s.passive_decorator,
                s.passive_factory,
                s.dispatcher,
            ]
            for s in symbols
        ],
        [
            [c["id"], c["enclosing"], c["members"], c["bindings"], c["match_args"]]
            for c in record["classes"]
        ],
    ]
    record["env"] = _digest(json.dumps(env, sort_keys=True))
    return record

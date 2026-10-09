"""Per-module facts the first pass records and the second pass reads, and
their JSON codec for the module cache."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from diffcone.indexer.scopes import ClassScope, ImportBinding, ModuleScope, Scope
from diffcone.indexer.syntax import _digest
from diffcone.model import Edge, ExternalReference, Symbol, UnresolvedReference


@dataclass
class _FuncParams:
    positional: list[str]  # including self/cls for bound methods
    bound: bool
    defaults: dict[str, tuple[str, ...] | None]
    has_varargs: bool


@dataclass
class _CallSite:
    positional: list[tuple[str, ...] | None]  # candidates per positional argument
    keywords: dict[str, tuple[str, ...] | None]
    unbounded: bool  # *args / **kwargs at the call site
    receiver_bound: bool  # ``obj.m(...)`` / ``self.m(...)``: self is implicit
    # The symbol the call sits in: a by-name import is attributed to the
    # caller that named the module, not to the helper that imports it.
    caller: str = ""
    # Per argument, the caller's own parameter it is, when it is exactly
    # that: ``skip_if_no(name)`` passes its parameter to the importer, so the
    # answer is one level further out.
    positional_params: list[str | None] = field(default_factory=list)
    keyword_params: dict[str, str | None] = field(default_factory=dict)

    def param_for(self, param: str, info: _FuncParams) -> str | None:
        """The caller's parameter passed for ``param`` here, if it is one."""
        if self.unbounded:
            return None
        if param in self.keyword_params:
            return self.keyword_params[param]
        if param in info.positional:
            index = info.positional.index(param)
            if info.bound and self.receiver_bound:
                index -= 1
            if 0 <= index < len(self.positional_params):
                return self.positional_params[index]
        return None

    # The class each argument is an instance of, where the argument says so
    # (``C()`` or ``C``); None when it does not. Used to bound what a
    # ``getattr`` on a parameter may read.
    positional_classes: list[str | None] = field(default_factory=list)
    keyword_classes: dict[str, str | None] = field(default_factory=dict)
    # The function an argument came out of (``make()``, or a local assigned
    # once from it): its return class answers once every module is indexed.
    positional_sources: list[str | None] = field(default_factory=list)
    keyword_sources: dict[str, str | None] = field(default_factory=dict)

    def class_for(self, param: str, info: _FuncParams) -> str | None:
        """The class of the argument passed for ``param`` here, if it says."""
        if self.unbounded:
            return None
        if param in self.keyword_classes:
            return self.keyword_classes[param]
        if param in info.positional:
            index = info.positional.index(param)
            if info.bound and self.receiver_bound:
                index -= 1
            if 0 <= index < len(self.positional_classes):
                return self.positional_classes[index]
        return None

    def value_for(self, param: str, info: _FuncParams) -> tuple[str, ...] | None:
        if self.unbounded:
            return None
        if param in self.keywords:
            return self.keywords[param]
        if param in info.positional:
            index = info.positional.index(param)
            if info.bound and self.receiver_bound:
                index -= 1
            if 0 <= index < len(self.positional):
                return self.positional[index]
        return info.defaults.get(param)


@dataclass
class _ParamDynamic:
    function: str
    param: str  # a parameter name, or an instance attribute name with self_class
    kind: str  # "getattr" | "import"
    base: list[str] | None  # receiver chain for getattr, when it is a name chain
    scope: Scope
    detail: str
    self_class: str = ""  # set when the name is ``self.<param>`` of this class


@dataclass
class _AttrWrite:
    """A write of ``self.<attr>`` in a method of ``cls``. ``binding`` is what
    an ``__init__`` assignment binds (``["param", name]``, ``["strings",
    [...]]``, ``["symbol", id]``); None for any other write."""

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
    # ...``, ``setattr(logging, ...)``, ``monkeypatch.setattr("logging.x",
    # ...)``), as (module, the symbol writing), and lookups by a name nothing
    # bounds on an external module: (symbol, module, "dynamic" or
    # "reflection", detail). Such a lookup can reach in-scope code only
    # through a write onto that module, which only the whole tree can tell
    # (Indexer._external_lookups).
    external_writes: set[tuple[str, str]] = field(default_factory=set)
    external_lookups: set[tuple[str, str, str, str]] = field(default_factory=set)

    def merge(self, other: _Output) -> None:
        self.edges |= other.edges
        self.unresolved |= other.unresolved
        self.external |= other.external
        for function, sites in other.call_sites.items():
            self.call_sites[function].extend(sites)
        self.escapes |= other.escapes
        self.func_params.update(other.func_params)
        self.returns.update(other.returns)
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
        self.external_lookups |= other.external_lookups


def _tuples(value: list | None) -> tuple[str, ...] | None:
    return None if value is None else tuple(value)


def _scope_to_dict(scope: Scope) -> dict:
    """The part of a function scope that deferred parameter-dynamic expansion
    resolves names against (module, imports, locals, self, aliases)."""
    return {
        "module": scope.module.name,
        "local_imports": {k: [b.module, b.attr] for k, b in scope.local_imports.items()},
        "locals": sorted(scope.locals),
        "self_name": scope.self_name,
        "self_class": scope.self_class,
        "param_aliases": scope.param_aliases,
    }


def _scope_from_dict(data: dict, scopes: dict[str, ModuleScope]) -> Scope:
    return Scope(
        module=scopes[data["module"]],
        local_imports={k: ImportBinding(m, a) for k, (m, a) in data["local_imports"].items()},
        locals=set(data["locals"]),
        self_name=data["self_name"],
        self_class=data["self_class"],
        param_aliases=dict(data["param_aliases"]),
    )


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
        "external_lookups": sorted(list(x) for x in out.external_lookups),
        "class_attributes": {
            c: dict(sorted(a.items())) for c, a in sorted(out.class_attributes.items())
        },
        "returns": {f: sorted(c) for f, c in sorted(out.returns.items())},
        "func_params": {
            f: [p.positional, p.bound, p.defaults, p.has_varargs]
            for f, p in out.func_params.items()
        },
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
    }


def _output_from_dict(data: dict, scopes: dict[str, ModuleScope]) -> _Output:
    out = _Output()
    out.edges = {Edge(*e) for e in data["edges"]}
    out.unresolved = {UnresolvedReference(*u) for u in data["unresolved"]}
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
    out.external_lookups = {(a, b, c, d) for a, b, c, d in data["external_lookups"]}
    out.returns = {f: tuple(c) for f, c in data["returns"].items()}
    out.func_params = {
        f: _FuncParams(
            positional=list(positional),
            bound=bound,
            defaults={k: _tuples(v) for k, v in defaults.items()},
            has_varargs=has_varargs,
        )
        for f, (positional, bound, defaults, has_varargs) in data["func_params"].items()
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
    return out


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
        "mutations": sorted(scope.mutations),
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
        record["mutations"],
        [[s.id, s.kind, s.module, s.reads_docstrings] for s in symbols],
        [[c["id"], c["enclosing"], c["members"], c["bindings"]] for c in record["classes"]],
    ]
    record["env"] = _digest(json.dumps(env, sort_keys=True))
    return record

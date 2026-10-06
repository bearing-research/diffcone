"""The indexer: parses a snapshot's modules (first pass, cacheable per
module), then resolves references into edges (second pass)."""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from typing import Any

from diffcone.cython import read as read_cython
from diffcone.indexer.definitions import (
    _ATTRIBUTE_HOOKS,
    _EXPLICIT_SPECIAL_METHODS,
    _STRUCTURAL_BASES,
    _annotated_args,
    _canonical_imports,
    _class_attributes,
    _end_line,
    _flatten_chain,
    _future_annotations,
    _has_annotations,
    _has_decorator,
    _inert_def,
    _is_inert_decorator,
    _is_literal,
    _is_special_method,
    _is_staticmethod,
    _rebound_names,
    _start_line,
    _variable_statements,
)
from diffcone.indexer.facts import (
    _AttrWrite,
    _facts_to_dict,
    _FuncParams,
    _Output,
    _output_from_dict,
    _output_to_dict,
    _ParamDynamic,
    _tuples,
)
from diffcone.indexer.literals import (
    INDEXED,
    NESTED_SCOPES,
    _collect_literal_bindings,
    _collect_store_names,
    _LocalBindings,
    _module_mutations,
)
from diffcone.indexer.references import _ReferenceCollector
from diffcone.indexer.scopes import (
    ClassScope,
    External,
    ImportBinding,
    Local,
    ModuleNode,
    ModuleScope,
    Node,
    Resolved,
    Scope,
    Unresolved,
    VariableStatement,
    _absolute_module,
)
from diffcone.indexer.syntax import (
    BUILTIN_NAMES,
    DEF_NODES,
    FUNC_NODES,
    IMPORT_ATTRIBUTION_DEPTH,
    _digest,
    _docstring_hash,
    _split_docstring,
    decode_source,
    hash_nodes,
    hash_scope_body,
    iter_scope_statements,
)
from diffcone.model import (
    CLASS,
    DEFINED_IN,
    FUNCTION,
    IMPORTS,
    IMPORTS_NAME,
    METHOD,
    MODULE,
    REFERENCES,
    UNRESOLVED_ATTRIBUTE,
    UNRESOLVED_DYNAMIC,
    UNRESOLVED_NAME,
    VARIABLE,
    AnalysisError,
    Edge,
    ExternalReference,
    SourceIndex,
    Symbol,
    UnresolvedReference,
)
from diffcone.snapshot import (
    Snapshot,
    child_modules,
    member_symbol_id,
    module_name_for,
)


class Indexer:
    def __init__(self, snapshot: Snapshot, module_cache=None) -> None:
        self.snapshot = snapshot
        self.index = SourceIndex(
            snapshot=snapshot.info,
            other_files=dict(snapshot.other_files),
            cython={
                path: read_cython(path, content.decode("utf-8", "surrogateescape"))
                for path, content in sorted(snapshot.cython_files.items())
            },
        )
        self.index.errors.extend(snapshot.errors)
        # Optional per-module cache of first-pass facts and second-pass
        # outputs (diffcone.cache.ModuleCache). Applies to every snapshot kind.
        self.module_cache = module_cache
        self.scopes: dict[str, ModuleScope] = {}
        self.class_scopes: dict[str, ClassScope] = {}
        self._module_prefixes: set[str] = set()
        self._bases_final = False
        # Where writes go: the global output is backed by the index; pass 2
        # swaps in a per-module output so it can be cached (see _Output).
        self._global = _Output(
            edges=self.index.edges,
            unresolved=self.index.unresolved,
            external=self.index.external,
            reflection=self.index.reflection,
            class_attributes=self.index.class_attributes,
            class_bases=self.index.class_bases,
        )
        self.out = self._global
        # Symbols and class scopes added by the module being indexed, and
        # whether one of its symbols collided with an earlier module's.
        self._added_symbols: list[Symbol] = []
        self._added_classes: list[ClassScope] = []
        self._collided = False
        # Transitive in-scope descendants per class, built once bases are final.
        self._descendants: dict[str, tuple[str, ...]] = {}
        # Import bindings being resolved (guards self-referential imports).
        self._resolving_bindings: set[tuple[str, str]] = set()
        # Classes with an unresolved ``super().<name>``, per name (final pass).
        self._super_misses: dict[str, set[str]] = defaultdict(set)

    # -- pass 1 ---------------------------------------------------------------

    def build(self) -> SourceIndex:
        cache = self.module_cache
        facts: dict[str, dict | None] = {}
        candidates = [
            (path, module)
            for path in sorted(self.snapshot.files)
            if (module := module_name_for(path, self.snapshot.source_roots)) is not None
        ]
        # Submodule names per package: a top-level binding of that name gets
        # a distinct identity (member_symbol_id).
        self._children = child_modules(self.snapshot)
        keys: dict[str, str] = {}  # path -> cache key; every record is loaded in one query
        if cache is not None:
            keys = {p: cache.key(m, p, self.snapshot.files[p]) for p, m in candidates}
        loaded = cache.load_facts(list(keys.values())) if cache is not None else {}
        for path, module in candidates:
            if module in self.scopes:
                other = self.scopes[module].path
                self._error(path, f"module {module!r} is also defined by {other}")
                continue
            record = loaded.get(keys[path]) if path in keys else None
            if record is None:
                tree = self._parse(path, module)
                if tree is None:
                    continue
            else:
                tree = None
            scope = ModuleScope(
                name=module, path=path, is_package=path.endswith("__init__.py"), tree=tree
            )
            scope.cache_key = keys.get(path)
            self.scopes[module] = scope
            self.index.modules.add(module)
            facts[module] = record
        new_facts: dict[str, dict] = {}
        new_resolved: dict[str, dict] = {}
        for module in self.index.modules:
            parts = module.split(".")
            for i in range(1, len(parts) + 1):
                self._module_prefixes.add(".".join(parts[:i]))
        for module in sorted(self.scopes):
            scope = self.scopes[module]
            record = facts[module]
            if record is not None:
                # A record whose symbols collide with an earlier module's (an
                # error either way) is not applied, so the errors come out
                # exactly as without a cache; a malformed record is a miss.
                try:
                    applied = self._apply_facts(scope, record)
                except (KeyError, TypeError, ValueError, AttributeError):
                    applied = False
                if applied:
                    continue
                scope.tree = self._parse(scope.path, module)
                if scope.tree is None:  # pragma: no cover - same content parsed before
                    del self.scopes[module]
                    self.index.modules.discard(module)
                    continue
            self._added_symbols, self._added_classes, self._collided = [], [], False
            self.out = _Output()
            try:
                self._index_module(scope)
            finally:
                captured, self.out = self.out, self._global
            self._global.merge(captured)
            if cache is not None:
                record = _facts_to_dict(
                    scope, self._added_symbols, self._added_classes, captured.edges
                )
                scope.env_digest = record["env"]
                if not self._collided and scope.cache_key is not None:
                    new_facts[scope.cache_key] = record
        self._unbind_mutated_variables()
        for class_id in sorted(self.class_scopes):
            self._ensure_bases(class_id)
        self._bases_final = True  # MROs may be memoised from here on
        self._build_descendants()
        self._external_callers()
        fingerprint = self._environment_fingerprint() if cache is not None else ""
        loaded = cache.load_resolved(list(keys.values()), fingerprint) if cache else {}
        for module in sorted(self.scopes):
            scope = self.scopes[module]
            key = scope.cache_key
            out: _Output | None = None
            if key is not None and key in loaded:
                try:
                    out = _output_from_dict(loaded[key], self.scopes)
                except (KeyError, TypeError, ValueError, AttributeError):
                    out = None  # malformed record: resolve as on a miss
            if out is None:
                if scope.tree is None:
                    self._load_tree(scope)
                self.out = _Output()
                try:
                    self._resolve_module(scope)
                finally:
                    out, self.out = self.out, self._global
                if key is not None:
                    new_resolved[key] = _output_to_dict(out)
            self._global.merge(out)
        self._resolve_param_dynamics()
        # Classes whose instances (or the class itself) are handed to someone
        # else: whoever holds one may read any attribute off it by a name
        # nothing resolves, so holding it depends on its members.
        returns = self._global.returns
        self.index.escaped_classes = {
            cls
            for sites in self._global.call_sites.values()
            for site in sites
            for cls in [
                *site.positional_classes,
                *site.keyword_classes.values(),
                # A factory's class counts as handed on too: every return of
                # that function yields it, so that is what the callee holds.
                *(c for f in site.positional_sources if f for c in returns.get(f, ())),
                *(c for f in site.keyword_sources.values() if f for c in returns.get(f, ())),
            ]
            if cls is not None
        }
        if cache is not None and (new_facts or new_resolved):
            cache.store(new_facts, new_resolved, fingerprint, list(keys.values()))
        return self.index

    def _parse(self, path: str, module: str) -> ast.Module | None:
        try:
            source = decode_source(self.snapshot.files[path])
            return ast.parse(source, filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            self._error(path, f"cannot parse: {exc}")
            self.index.failed_modules.add(module)
            return None

    def _load_tree(self, scope: ModuleScope) -> None:
        """Parse a module whose facts came from the cache but which must be
        resolved again (its file or the environment changed)."""
        tree = self._parse(scope.path, scope.name)
        if tree is None:  # pragma: no cover - the same content parsed before
            tree = ast.Module(body=[], type_ignores=[])
        scope.tree = tree
        _, _, variable_stmts = self._module_statements(scope, register_imports=False)
        scope.variable_stmts = {n: s for n, s in variable_stmts.items() if n in scope.variables}

    def _apply_facts(self, scope: ModuleScope, record: dict) -> bool:
        """Install a cached facts record. Everything is built before anything
        is stored, so a malformed record (which raises) or one whose symbols
        collide with an earlier module's (returns False) leaves no trace."""
        imports = {k: ImportBinding(m, a) for k, (m, a) in record["imports"].items()}
        star_imports = list(record["star_imports"])
        bindings = set(record["bindings"])
        members = dict(record["members"])
        variables = dict(record["variables"])
        literal_names = {k: _tuples(v) for k, v in record["literal_names"].items()}
        mutations = frozenset(record["mutations"])
        env_digest = str(record["env"])
        symbols: list[Symbol] = []
        for data in record["symbols"]:
            data = dict(data)
            data["line_ranges"] = tuple(tuple(r) for r in data["line_ranges"])
            data["imports"] = tuple(data["imports"])
            symbols.append(Symbol(**data))
        classes: dict[str, ClassScope] = {}
        for data in record["classes"]:
            enclosing = classes[data["enclosing"]] if data["enclosing"] else None
            classes[data["id"]] = ClassScope(
                id=str(data["id"]),
                module=scope,
                enclosing=enclosing,
                members=dict(data["members"]),
                bindings=set(data["bindings"]),
                base_chains=[list(c) if c is not None else None for c in data["base_chains"]],
                base_names=[list(c) if c is not None else None for c in data["base_names"]],
                plain=bool(data["plain"]),
            )
        edges = {Edge(*e) for e in record["edges"]}
        if any(s.id in self.index.symbols for s in symbols):
            return False
        # Top-level identities depend on which submodules exist: a record
        # made before a shadowed submodule was added (or removed) is stale.
        for name, symbol_id in [*members.items(), *variables.items()]:
            if symbol_id != self._member_id(scope.name, name):
                return False
        scope.imports, scope.star_imports, scope.bindings = imports, star_imports, bindings
        scope.members, scope.variables, scope.literal_names = members, variables, literal_names
        scope.mutations = mutations
        scope.env_digest = env_digest
        for symbol in symbols:
            self._add_symbol(symbol)
        self.class_scopes.update(classes)
        self.out.edges |= edges
        return True

    def _unbind_mutated_variables(self) -> None:
        """A module-level container that any module mutates in place is not
        the literal it was assigned: unbind it everywhere (its own module and
        the modules that import it), so names drawn from it stay dynamic."""
        mutated: set[tuple[str, str]] = set()
        for scope in self.scopes.values():
            for name in scope.mutations:
                if name in scope.variables:
                    mutated.add((scope.name, name))
                binding = scope.imports.get(name)
                if binding is not None and binding.attr is not None:
                    mutated.add((binding.module, binding.attr))
        for module, name in mutated:
            target = self.scopes.get(module)
            if target is not None and name in target.literal_names:
                target.literal_names[name] = target.literal_names[name + INDEXED] = None

    def _environment_fingerprint(self) -> str:
        """Digest of everything a module's resolution reads from other
        modules: their observable facts plus every class's resolved bases,
        and the root specs (whether ``__name__`` is a module's runtime name)."""
        material = [
            sorted(self.snapshot.source_roots),
            [[m, self.scopes[m].env_digest] for m in sorted(self.scopes)],
            [[c, cs.bases, cs.complete] for c, cs in sorted(self.class_scopes.items())],
        ]
        return _digest(json.dumps(material))

    def _resolve_param_dynamics(self) -> None:
        """Expand ``getattr(x, p)`` / ``import_module(p)`` where ``p`` is a
        parameter or an instance attribute, using the literal strings every
        resolved call site passes (for an attribute: what ``__init__`` binds
        it to, see _attribute_writes). A function that escapes (used as a
        value, or whose name occurs as an unresolved reference so callers may
        be unknown) or has an unbounded call site stays dynamic.

        Expanding a ``getattr`` can itself make a function escape (its value
        is used) or record a new name-bounded reference (a name that cannot
        be resolved on its receiver), either of which can unbound another
        expansion, so the candidates are recomputed until both the escape
        set and the unresolved names are stable."""
        unresolved_names = {
            u.name for u in self.index.unresolved if u.name and not u.detail.startswith("super().")
        }
        # ``super().m`` that did not resolve in class K can still only reach
        # an ``m`` after K in the MRO of K or of a subclass of K.
        self._super_misses.clear()
        for u in self.index.unresolved:
            if u.name and u.detail.startswith("super()."):
                source = self.index.symbols.get(u.symbol)
                while source is not None and source.kind != CLASS:
                    source = self.index.symbols.get(source.container or "")
                if source is None:
                    unresolved_names.add(u.name)
                else:
                    self._super_misses[u.name].add(source.id)
        writes: dict[tuple[str, str], list[_AttrWrite]] = defaultdict(list)
        for w in self.out.attr_writes:
            writes[(w.cls, w.attr)].append(w)
        while True:
            escapes = set(self.out.escapes)
            names = set(unresolved_names)
            planned: list[tuple[_ParamDynamic, list[str] | None]] = []
            for pd in self.out.param_dynamics:
                if pd.self_class:
                    values = self._attribute_strings(
                        pd.self_class, pd.param, writes, unresolved_names
                    )
                else:
                    values = self._param_values(pd.function, pd.param, unresolved_names)
                planned.append((pd, values))
            for pd, values in planned:
                if values is None or pd.kind != "getattr":
                    continue
                for name in dict.fromkeys(values):
                    if pd.base is None:
                        unresolved_names.add(name)
                        continue
                    node, rest = self.resolve_chain_names(pd.base + [name], pd.scope)
                    if isinstance(node, Resolved) and not node.detail:
                        self.escape(node)
                    elif isinstance(node, Unresolved) and node.name:
                        unresolved_names.add(node.name)
                    unresolved_names.update(rest)
            if self.out.escapes == escapes and unresolved_names == names:
                break
        for pd, values in planned:
            if pd.kind == "import" and self._import_per_caller(pd):
                continue
            if values is None:
                # The name is unbounded. If the *receiver* is one the call
                # sites name, the read is still bounded: it can only be an
                # attribute of those classes, so depend on their members
                # rather than on everything (see _receiver_classes).
                classes = self._receiver_classes(pd, writes) if pd.kind == "getattr" else None
                if classes:
                    for member in sorted(self._class_members(classes)):
                        self.out.edges.add(
                            Edge(pd.function, member, REFERENCES, "attribute read dynamically")
                        )
                    continue
                self.out.unresolved.add(
                    UnresolvedReference(pd.function, UNRESOLVED_DYNAMIC, "", pd.detail)
                )
                continue
            for name in dict.fromkeys(values):
                if pd.kind == "import":
                    if name.startswith("."):
                        self.out.unresolved.add(
                            UnresolvedReference(pd.function, UNRESOLVED_DYNAMIC, "", pd.detail)
                        )
                    else:
                        self._module_import_edge(pd.function, name)
                elif pd.base is None:
                    self.out.unresolved.add(
                        UnresolvedReference(
                            pd.function, UNRESOLVED_ATTRIBUTE, name, f"getattr(..., {name!r})"
                        )
                    )
                else:
                    chain = ".".join(pd.base + [name])
                    node, rest = self.resolve_chain_names(pd.base + [name], pd.scope)
                    self._record(pd.function, node, chain=chain)
                    for extra in rest:
                        self.out.unresolved.add(
                            UnresolvedReference(pd.function, UNRESOLVED_ATTRIBUTE, extra, chain)
                        )
        for ref in self.out.attr_refs:
            bound = self._attribute_writes(ref.cls, ref.attr, writes, unresolved_names)
            if bound is None or any(binding[0] != "symbol" for _, binding in bound):
                continue
            for _, binding in bound:
                node: Node = Resolved(binding[1])
                for attr in ref.rest:
                    node = self._step(node, attr)
                if isinstance(node, Resolved) and not ref.rest and node.symbol != ref.source:
                    self.out.edges.add(
                        Edge(ref.source, node.symbol, REFERENCES, f"self.{ref.attr}")
                    )
                else:
                    self._record(ref.source, node, chain=ref.chain)

    def _import_per_caller(self, pd: _ParamDynamic) -> bool:
        """``import_optional_dependency(name)``: resolve the parameter per call
        site rather than once for the function.

        A caller that passes a literal can only cause an import of *that*
        module, so the edge belongs to it; one that passes something unbounded
        keeps the dynamic reference, and only what reaches that caller is
        selected conservatively. Attributing the import to the caller rather
        than to the helper that runs it is deliberate: a target reaching the
        caller reaches the import, and the helper's other callers did not name
        that module. Returns False when the callers are not known, which
        leaves the all-or-nothing treatment in place.
        """
        attributed = self._attributed_imports(pd.function, pd.param, set())
        if attributed is None:
            return False
        for caller, names in attributed:
            if names is None:
                self.out.unresolved.add(
                    UnresolvedReference(caller, UNRESOLVED_DYNAMIC, "", pd.detail)
                )
                continue
            for name in dict.fromkeys(names):
                self._module_import_edge(caller, name)
        return True

    def _attributed_imports(
        self, function: str, param: str, seen: set[tuple[str, str]], depth: int = 0
    ) -> list[tuple[str, tuple[str, ...] | None]] | None:
        """Per call site of ``function``, who imports what through ``param``.

        A site that passes its own parameter answers one level further out --
        pandas' ``skip_if_no(name)`` hands its parameter to the importer, and
        its own callers name the module -- so the search follows it, with a
        depth cap and a guard against a cycle. None when the callers cannot
        be known at all."""
        if (function, param) in seen or depth > IMPORT_ATTRIBUTION_DEPTH:
            return None
        seen.add((function, param))
        info = self.out.func_params.get(function)
        symbol = self.index.symbols.get(function)
        sites = self.out.call_sites.get(function, [])
        if info is None or symbol is None or not sites:
            return None
        if function in self.out.escapes or self._super_may_reach(symbol):
            return None  # it may be called from somewhere unseen
        if not all(site.caller for site in sites):
            return None  # an older cache entry, without the caller recorded
        found: list[tuple[str, tuple[str, ...] | None]] = []
        for site in sites:
            names = site.value_for(param, info)
            if names is not None and not any(name.startswith(".") for name in names):
                found.append((site.caller, tuple(names)))
                continue
            outer = site.param_for(param, info) if names is None else None
            deeper = (
                self._attributed_imports(site.caller, outer, seen, depth + 1)
                if outer is not None
                else None
            )
            found.extend(deeper if deeper is not None else [(site.caller, None)])
        return found

    def _class_members(self, classes: set[str]) -> set[str]:
        """Every symbol inside those classes and their in-scope subclasses: an
        instance of one may be an instance of the other."""
        family = set(classes)
        for cls in classes:
            family.update(self._descendants.get(cls, ()))
        return {
            symbol
            for symbol in self.index.symbols
            for cls in family
            if symbol.startswith(cls + ".")
        }

    def _receiver_classes(
        self, pd: _ParamDynamic, writes: dict[tuple[str, str], list[_AttrWrite]]
    ) -> set[str] | None:
        """The classes the receiver of ``getattr(receiver, <unbounded>)`` may
        be an instance of, or None when nothing says.

        Two shapes carry the answer. A receiver that is a parameter is
        whatever the call sites pass (``invoke(Provider(), name)``). A
        receiver that is ``self.<attr>`` is what ``__init__`` bound it to,
        which is usually a parameter of its own, so the constructions answer
        instead (structlog's ``getattr(self._logger, method_name)``)."""
        if pd.base is None or pd.self_class:
            return None
        info = self.out.func_params.get(pd.function)
        if info is None:
            return None
        if len(pd.base) == 1 and pd.base[0] in info.positional:
            return self._passed_classes(pd.function, pd.base[0])
        if (
            len(pd.base) == 2
            and pd.scope.self_class
            and pd.base[0] == pd.scope.self_name
            and not pd.scope.self_is_class
        ):
            return self._attribute_classes(pd.scope.self_class, pd.base[1], writes)
        return None

    def _passed_classes(self, function: str, param: str) -> set[str] | None:
        """The classes every resolved call site passes for ``param``; None
        when the function may be called from somewhere unseen or a site says
        nothing about what it passes."""
        info = self.out.func_params.get(function)
        symbol = self.index.symbols.get(function)
        sites = self.out.call_sites.get(function, [])
        if (
            info is None
            or symbol is None
            or not sites
            or function in self.out.escapes
            or self._super_may_reach(symbol)
        ):
            return None
        found: set[str] = set()
        for site in sites:
            cls = site.class_for(param, info)
            if cls is None:
                return None
            found.add(cls)
        return found or None

    def _attribute_classes(
        self, class_id: str, attr: str, writes: dict[tuple[str, str], list[_AttrWrite]]
    ) -> set[str] | None:
        """What ``self.<attr>`` holds, as classes: every write must assign a
        class, or a parameter whose constructions all pass one.

        The guards are this rule's own, not ``_attribute_writes``'s. Both
        refuse when the attribute is written through a receiver whose type is
        unknown, is a class-level name, or the class customises attribute
        access. This one does not refuse merely because the class escapes:
        an unseen subclass lives in code outside the source roots, and what
        such code puts in the attribute comes from there too. A construction
        we *can* see whose argument says nothing still gives up (below), which
        is the case that matters -- a factory inside the project."""
        unbound = self.out.attr_unbound
        if ("", "*") in unbound or ("", attr) in unbound:
            return None
        family: set[str] = set()
        for cid in (class_id, *self._descendants.get(class_id, ())):
            family.update(self._mro(cid))
        bound: list[tuple[_AttrWrite, list[Any]]] = []
        for cid in sorted(family):
            cscope = self.class_scopes.get(cid)
            if cscope is None or cscope.opaque or (cid, "*") in unbound or (cid, attr) in unbound:
                return None
            if attr in cscope.members or attr in cscope.bindings:
                return None
            if _ATTRIBUTE_HOOKS & cscope.members.keys():
                return None
            for w in writes.get((cid, attr), ()):
                if w.binding is None:
                    return None
                bound.append((w, w.binding))
        if not bound:
            return None
        found: set[str] = set()
        for w, (kind, value) in bound:
            if kind == "symbol":
                symbol = self.index.symbols.get(value)
                if symbol is None or symbol.kind != CLASS:
                    return None
                found.add(value)
            elif kind == "param":
                passed = self._passed_classes(w.method, value)
                if passed is None:
                    return None
                found |= passed
            else:
                return None
        return found or None

    def _param_values(
        self, function: str, param: str, unresolved_names: set[str]
    ) -> list[str] | None:
        """The literal strings every call site of ``function`` passes for
        ``param``, or None when some caller may be unseen or unbounded."""
        info = self.out.func_params.get(function)
        symbol = self.index.symbols.get(function)
        sites = self.out.call_sites.get(function, [])
        if (
            info is None
            or symbol is None
            or function in self.out.escapes
            or symbol.name in unresolved_names
            or self._super_may_reach(symbol)
            or not sites
            or self._constructor_escapes(symbol, unresolved_names)
        ):
            return None
        values: list[str] = []
        for site in sites:
            found = site.value_for(param, info)
            if found is None:
                return None
            values.extend(found)
        return values

    def lookup_super(self, class_id: str, attr: str) -> Node:
        """``super().<attr>`` in ``class_id``: the next definition after it in
        its MRO, plus (as overrides) what follows it in the MRO of each
        in-scope subclass, where a mixin may come first."""
        hit = self.lookup_in_class(class_id, attr, skip_self=True)
        base = (hit.symbol, hit.detail) if isinstance(hit, Resolved) else None
        extra: list[tuple[str, str]] = []
        for sub in self._descendants.get(class_id, ()):
            mro = self._mro(sub)
            if class_id not in mro:
                continue
            for cid in mro[mro.index(class_id) + 1 :]:
                cscope = self.class_scopes[cid]
                if attr in cscope.members:
                    pair = (cscope.members[attr], "")
                elif attr in cscope.bindings:
                    pair = (cid, f"attribute:{attr}")
                else:
                    continue
                if pair != base and pair not in extra:
                    extra.append(pair)
                break
        if not extra or not isinstance(hit, Resolved):
            return hit
        return Resolved(hit.symbol, hit.detail, hit.uncertain_attr, overrides=tuple(extra))

    def _super_may_reach(self, symbol: Symbol) -> bool:
        """An unresolved ``super().<name>`` in class K may call ``symbol``
        when its class follows K in some in-scope MRO."""
        owner = symbol.container
        for k in self._super_misses.get(symbol.name, ()):
            for cid in (k, *self._descendants.get(k, ())):
                mro = self._mro(cid)
                if k in mro and owner in mro[mro.index(k) + 1 :]:
                    return True
        return False

    def _constructor_escapes(self, symbol: Symbol, unresolved_names: set[str]) -> bool:
        """An ``__init__`` also runs whenever a class that inherits it is
        constructed: that happens unseen when such a class escapes or its name
        occurs as an unresolved reference."""
        if symbol.kind != METHOD or symbol.name != "__init__":
            return False
        owner = symbol.container
        if owner not in self.class_scopes:
            return True
        for cid in (owner, *self._descendants.get(owner, ())):
            init = self.lookup_in_class(cid, "__init__")
            if not (isinstance(init, Resolved) and init.symbol == symbol.id):
                continue
            if cid in self.out.escapes or self.index.symbols[cid].name in unresolved_names:
                return True
        return False

    def _attribute_writes(
        self,
        class_id: str,
        attr: str,
        writes: dict[tuple[str, str], list[_AttrWrite]],
        unresolved_names: set[str],
    ) -> list[tuple[_AttrWrite, list[Any]]] | None:
        """What ``self.<attr>`` may hold in a method of ``class_id``: its
        bound writes with their bindings, or None when it cannot be bounded. The
        instance may belong to any in-scope subclass, so every class in the
        MRO of the class or of a subclass counts; each must be plain and
        fully in scope, none may define the attribute at class level or
        customise attribute access, and every write must be a bounded
        ``__init__`` assignment. A class that escapes, or whose name occurs
        as an unresolved reference, may have subclasses the index cannot
        see (``class S(Base)`` with ``Base = Foo if X else Bar``), whose
        writes are unknown."""
        unbound = self.out.attr_unbound
        if ("", "*") in unbound or ("", attr) in unbound:
            return None
        family: set[str] = set()
        for cid in (class_id, *self._descendants.get(class_id, ())):
            family.update(self._mro(cid))
        bound: list[tuple[_AttrWrite, list[Any]]] = []
        for cid in sorted(family):
            cscope = self.class_scopes[cid]
            if not cscope.plain or cscope.opaque or (cid, "*") in unbound or (cid, attr) in unbound:
                return None
            if cid in self.out.escapes or self.index.symbols[cid].name in unresolved_names:
                return None
            if attr in cscope.members or attr in cscope.bindings:
                return None
            if _ATTRIBUTE_HOOKS & cscope.members.keys():
                return None
            for w in writes.get((cid, attr), ()):
                if w.binding is None:
                    return None
                bound.append((w, w.binding))
        return bound or None

    def _attribute_strings(
        self,
        class_id: str,
        attr: str,
        writes: dict[tuple[str, str], list[_AttrWrite]],
        unresolved_names: set[str],
    ) -> list[str] | None:
        bound = self._attribute_writes(class_id, attr, writes, unresolved_names)
        if bound is None:
            return None
        values: list[str] = []
        for w, (kind, value) in bound:
            if kind == "strings":
                values.extend(value)
            elif kind == "param":
                found = self._param_values(w.method, value, unresolved_names)
                if found is None:
                    return None
                values.extend(found)
            else:
                return None
        return values

    def _error(self, path: str, message: str) -> None:
        self.index.errors.append(
            AnalysisError(revision=self.snapshot.revision, path=path, message=message)
        )

    def _add_symbol(self, symbol: Symbol) -> bool:
        existing = self.index.symbols.get(symbol.id)
        if existing is not None:
            self._error(
                symbol.path,
                f"symbol identity {symbol.id!r} collides with {existing.kind} in {existing.path}",
            )
            self._collided = True
            return False
        self.index.symbols[symbol.id] = symbol
        self._added_symbols.append(symbol)
        return True

    def _module_statements(
        self, scope: ModuleScope, *, register_imports: bool
    ) -> tuple[list[ast.stmt], list[ast.stmt], dict[str, VariableStatement]]:
        """(all scope statements, body without docstring, variable statements)
        of a parsed module; records the import statements and, unless the
        import table was served by the cache, fills it."""
        assert scope.tree is not None
        stmts = list(iter_scope_statements(scope.tree.body))
        scope.import_nodes = [s for s in stmts if isinstance(s, (ast.Import, ast.ImportFrom))]
        if register_imports:
            for node in scope.import_nodes:
                self._register_imports(scope, node, scope.imports, scope.star_imports)
        body = _split_docstring(scope.tree.body)[1]
        return stmts, body, _variable_statements(scope, body)

    def _index_module(self, scope: ModuleScope) -> None:
        assert scope.tree is not None
        stmts, body, variable_stmts = self._module_statements(scope, register_imports=True)
        for stmt in stmts:
            if not isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
                scope.bindings |= _collect_store_names(stmt)
        scope.literal_names = _collect_literal_bindings(scope.tree, {})
        scope.mutations = frozenset(_module_mutations(scope.tree))
        imports = tuple(sorted(_canonical_imports(scope)))
        module_body_hash = hash_scope_body(
            [s for s in body if s not in variable_stmts.values()], strip_imports=True
        )
        module_doc_hash = _docstring_hash([scope.tree.body])
        self._add_symbol(
            Symbol(
                id=scope.name,
                kind=MODULE,
                module=scope.name,
                name=scope.name.rsplit(".", 1)[-1],
                path=scope.path,
                lineno=1,
                body_hash=module_body_hash,
                docstring_hash=module_doc_hash,
                definition_hash=_digest("\n".join(imports)),
                container=None,
                line_ranges=((1, _end_line(scope.tree)),),
                imports=imports,
            )
        )
        self._index_definitions(scope, scope.tree.body, scope.name, scope.members, None)
        # Module-level statements that mention a variable may mutate it in place
        # (``REGISTRY[k] = v``, ``NAMES.append(x)``, ``CONFIG.update(...)``), so
        # they are part of that variable's body, not only of the module's.
        mutators: dict[str, list[ast.stmt]] = defaultdict(list)
        variable_ids = {id(s) for s in variable_stmts.values()}
        for stmt in body:
            if isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
                continue
            if id(stmt) in variable_ids:
                continue
            mentioned = {
                n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and n.id in variable_stmts
            }
            for name in mentioned:
                mutators[name].append(stmt)
        for name, stmt in variable_stmts.items():
            if name in scope.members:
                continue  # also a def/class: Python's last binding wins; stay conservative
            symbol_id = self._member_id(scope.name, name)
            value = stmt.value
            assert value is not None  # _variable_statements keeps assignments with a value
            symbol = Symbol(
                id=symbol_id,
                kind=VARIABLE,
                module=scope.name,
                name=name,
                path=scope.path,
                lineno=stmt.lineno,
                body_hash=hash_nodes([value, *mutators.get(name, [])]),
                definition_hash="",
                container=scope.name,
                line_ranges=tuple(
                    (s.lineno, _end_line(s)) for s in (stmt, *mutators.get(name, []))
                ),
                # Binding a literal runs no code when the module is imported;
                # only readers of the value can observe the change. ``__all__``
                # is not inert: it decides what ``from m import *`` binds.
                inert_definition=(
                    name != "__all__" and not mutators.get(name) and _is_literal(value)
                ),
            )
            if self._add_symbol(symbol):
                scope.variables[name] = symbol_id
                scope.variable_stmts[name] = stmt
                self.out.edges.add(Edge(symbol_id, scope.name, DEFINED_IN))

    def _register_imports(
        self,
        scope: ModuleScope,
        node: ast.stmt,
        table: dict[str, ImportBinding],
        stars: list[str],
    ) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = ImportBinding(alias.name, None)
                else:
                    table[alias.name.split(".")[0]] = ImportBinding(alias.name.split(".")[0], None)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(scope, node.module, node.level)
            for alias in node.names:
                if alias.name == "*":
                    stars.append(base)
                else:
                    table[alias.asname or alias.name] = ImportBinding(base, alias.name)

    def _returned_class(self, node: ast.AST, scope: Scope) -> tuple[str, ...] | None:
        """The classes ``node`` returns, when every ``return`` in it yields
        one: ``def make(): return Provider()`` yields that class, and a
        factory that picks between two yields both. One return that says
        something else, or none at all, says nothing -- a guess here would
        bind a class that never reaches the caller."""
        found: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, NESTED_SCOPES) and inner is not node:
                continue
            if not isinstance(inner, ast.Return) or inner.value is None:
                continue
            cls = self._expression_class(inner.value, scope)
            if cls is None:
                return None
            found.add(cls)
        return tuple(sorted(found)) or None

    def _expression_class(self, expr: ast.expr, scope: Scope) -> str | None:
        """The class an expression is an instance of, when it says so: ``C()``
        constructs one, ``C`` is the class itself."""
        target = expr.func if isinstance(expr, ast.Call) else expr
        parts = _flatten_chain(target)
        if parts is None:
            return None
        node = self.resolve_chain(parts, scope)
        if not isinstance(node, Resolved) or node.detail:
            return None
        symbol = self.index.symbols.get(node.symbol)
        return node.symbol if symbol is not None and symbol.kind == CLASS else None

    def _index_definitions(
        self,
        scope: ModuleScope,
        body: list[ast.stmt],
        container_id: str,
        members: dict[str, str],
        class_scope: ClassScope | None,
    ) -> None:
        funcs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        classes: dict[str, list[ast.ClassDef]] = {}
        order: list[str] = []
        for stmt in iter_scope_statements(body):
            if isinstance(stmt, FUNC_NODES):
                funcs.setdefault(stmt.name, []).append(stmt)
            elif isinstance(stmt, ast.ClassDef):
                classes.setdefault(stmt.name, []).append(stmt)
            else:
                continue
            if stmt.name not in order:
                order.append(stmt.name)
        for name in order:
            if class_scope is None and container_id == scope.name:
                symbol_id = self._member_id(scope.name, name)
            else:
                symbol_id = f"{container_id}.{name}"
            if name in classes:
                nodes = classes[name]
                first = nodes[0]
                member_names = sorted(
                    {
                        s.name
                        for n in nodes
                        for s in iter_scope_statements(n.body)
                        if isinstance(s, DEF_NODES)
                    }
                )

                def class_hashes(nodes=nodes, member_names=member_names):
                    definition_parts: list[ast.AST] = []
                    for n in nodes:
                        definition_parts += (
                            list(n.bases) + list(n.keywords) + list(n.decorator_list)
                        )
                    return (
                        _digest(
                            "\n".join(
                                hash_scope_body(_split_docstring(n.body)[1], strip_imports=False)
                                for n in nodes
                            )
                        ),
                        _digest(hash_nodes(definition_parts) + "|" + ",".join(member_names)),
                        _docstring_hash([n.body for n in nodes]),
                    )

                body_hash, definition_hash, doc_hash = class_hashes()
                symbol = Symbol(
                    id=symbol_id,
                    kind=CLASS,
                    module=scope.name,
                    name=name,
                    path=scope.path,
                    lineno=first.lineno,
                    body_hash=body_hash,
                    definition_hash=definition_hash,
                    container=container_id,
                    line_ranges=tuple((_start_line(n), _end_line(n)) for n in nodes),
                    docstring_hash=doc_hash,
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.out.edges.add(Edge(symbol_id, container_id, DEFINED_IN))
                cscope = ClassScope(id=symbol_id, module=scope, enclosing=class_scope)
                cscope.plain = len(nodes) == 1 and not (first.decorator_list or first.keywords)
                for n in nodes:
                    cscope.base_chains.extend(_flatten_chain(b) for b in n.bases)
                    cscope.base_names.extend(
                        _flatten_chain(b.value if isinstance(b, ast.Subscript) else b)
                        for b in n.bases
                    )
                self.class_scopes[symbol_id] = cscope
                self._added_classes.append(cscope)
                for n in nodes:
                    for stmt in iter_scope_statements(n.body):
                        if not isinstance(stmt, DEF_NODES):
                            cscope.bindings |= _collect_store_names(stmt)
                # Every definition of the class (``if``/``else`` variants) is
                # one symbol, so its members are indexed together: a method
                # defined in several of them is one symbol too.
                bodies = [stmt for n in nodes for stmt in n.body]
                self._index_definitions(scope, bodies, symbol_id, cscope.members, cscope)
                # Special methods run implicitly on instances (``==``, ``len()``,
                # calling one, ``with``): whatever references the class may
                # trigger them, so the class depends on them.
                for member, member_id in sorted(cscope.members.items()):
                    member_symbol = self.index.symbols.get(member_id)
                    if (
                        _is_special_method(member)
                        and member_symbol is not None
                        and member_symbol.kind == METHOD
                    ):
                        self.out.edges.add(Edge(symbol_id, member_id, REFERENCES, "special_method"))
            else:
                nodes = funcs[name]
                first = nodes[0]

                def function_hashes(nodes=nodes):
                    definition_parts: list[ast.AST] = []
                    annotation_parts: list[ast.AST] = []
                    for n in nodes:
                        definition_parts.append(n.args)
                        definition_parts += list(n.decorator_list)
                        if n.returns is not None:
                            annotation_parts.append(n.returns)
                    # The definition hash excludes annotations: detach them
                    # while hashing (no copy of the tree) and restore them.
                    detached = [a for n in nodes for a in _annotated_args(n.args)]
                    annotations = [a.annotation for a in detached]
                    for a in detached:
                        a.annotation = None
                    try:
                        definition = hash_nodes(definition_parts)
                    finally:
                        for a, annotation in zip(detached, annotations, strict=True):
                            a.annotation = annotation
                    annotation_parts = [
                        *(a for a in annotations if a is not None),
                        *annotation_parts,
                    ]
                    return (
                        _digest(hash_nodes(annotation_parts)),
                        _digest(
                            "\n".join(hash_nodes(list(_split_docstring(n.body)[1])) for n in nodes)
                        ),
                        _digest(definition + "|" + ",".join(type(n).__name__ for n in nodes)),
                        _docstring_hash([n.body for n in nodes]),
                    )

                annotation_hash, body_hash, definition_hash, doc_hash = function_hashes()
                deferred = (
                    _future_annotations(scope)
                    and all(_is_inert_decorator(d, scope) for n in nodes for d in n.decorator_list)
                    and (class_scope is None or class_scope.plain)
                )
                inert = all(_inert_def(n, scope) for n in nodes) and (
                    class_scope is None or class_scope.plain
                )
                inert = inert and (deferred or not any(_has_annotations(n) for n in nodes))
                symbol = Symbol(
                    id=symbol_id,
                    kind=METHOD if class_scope is not None else FUNCTION,
                    module=scope.name,
                    name=name,
                    path=scope.path,
                    lineno=first.lineno,
                    body_hash=body_hash,
                    docstring_hash=doc_hash,
                    definition_hash=definition_hash,
                    container=container_id,
                    line_ranges=tuple((_start_line(n), _end_line(n)) for n in nodes),
                    annotation_hash=annotation_hash,
                    deferred_annotations=deferred,
                    inert_definition=inert,
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.out.edges.add(Edge(symbol_id, container_id, DEFINED_IN))

    def _member_id(self, module: str, name: str) -> str:
        return member_symbol_id(module, name, self._children.get(module, frozenset()))

    def modules_with_prefix(self, prefix: str) -> tuple[str, ...]:
        return tuple(sorted(m for m in self.scopes if m.startswith(prefix)))

    def symbol_names_with_prefix(self, prefix: str) -> tuple[str, ...]:
        names = {s.name for s in self.index.symbols.values() if s.name.startswith(prefix)}
        return tuple(sorted(names))

    # -- inheritance ----------------------------------------------------------

    def _ensure_bases(self, class_id: str) -> None:
        """Resolve a class's bases on demand (a dotted base such as
        ``Zed.Inner`` may need another class's MRO first)."""
        cscope = self.class_scopes[class_id]
        if cscope.bases_state:
            return
        cscope.bases_state = 1
        for parts in cscope.base_chains:
            node = self._resolve_base_expr(parts, cscope)
            self._record(cscope.id, node, chain=".".join(parts) if parts else "<expr>")
            if (
                isinstance(node, Resolved)
                and not node.detail
                and not node.uncertain_attr
                and node.symbol in self.class_scopes
                and node.symbol != cscope.id
            ):
                cscope.bases.append(node.symbol)
            else:
                cscope.complete = False  # external, dynamic (``Generic[T]``) or unknown
                if not (parts == ["object"] and node is None):
                    cscope.opaque = True
        for name in cscope.base_names:
            if self._calls_back(name, cscope):
                cscope.external_base = True
        cscope.bases_state = 2

    def _calls_back(self, name: list[str] | None, cscope: ClassScope) -> bool:
        """Whether a base (as written) is code outside the source roots that
        may call the subclass's methods: not an in-scope class (its own
        status is inherited through the MRO), not a builtin, not a purely
        structural typing/abc base. An unknown expression counts."""
        if name is None:
            return True
        node = self._resolve_base_expr(name, cscope)
        if isinstance(node, Resolved) and not node.detail and node.symbol in self.class_scopes:
            return False
        head = name[0]
        binding = cscope.module.imports.get(head)
        if binding is None:
            if node is None and head in BUILTIN_NAMES and len(name) == 1:
                return False
            canonical = ".".join(name)
        else:
            base = binding.module if binding.attr is None else f"{binding.module}.{binding.attr}"
            canonical = ".".join([base, *name[1:]])
        return canonical not in _STRUCTURAL_BASES

    def _resolve_base_expr(self, parts: list[str] | None, cscope: ClassScope) -> Node:
        """A base name is looked up in the enclosing class body (for nested
        classes) and then in the module, as Python does when the class
        statement executes."""
        if parts is None:
            return None  # ``Generic[T]``, ``namedtuple(...)``: the collector visits it
        enclosing = cscope.enclosing
        if enclosing is not None and parts[0] in enclosing.members:
            node: Node = Resolved(enclosing.members[parts[0]])
            for attr in parts[1:]:
                node = self._step(node, attr)
            return node
        if enclosing is not None and parts[0] in enclosing.bindings:
            return Resolved(enclosing.id, detail=f"attribute:{parts[0]}")
        return self.resolve_chain(parts, Scope(module=cscope.module))

    def _mro(self, class_id: str) -> list[str]:
        """Linearisation over in-scope classes: the class, then its bases
        depth-first left to right keeping the last occurrence of a repeated
        base (C3 for ordinary hierarchies; documented as an approximation).
        Memoised only once every class's bases are resolved."""
        cscope = self.class_scopes[class_id]
        if cscope.mro is not None:
            return cscope.mro
        self._ensure_bases(class_id)
        if cscope.in_mro:
            return [class_id]  # inheritance cycle: stop here
        cscope.in_mro = True
        try:
            order: list[str] = []
            for base in cscope.bases:
                order.extend(self._mro(base))
        finally:
            cscope.in_mro = False
        seen: set[str] = {class_id}
        tail: list[str] = []
        for cid in reversed(order):
            if cid not in seen:
                seen.add(cid)
                tail.append(cid)
        result = [class_id, *reversed(tail)]
        if self._bases_final:
            cscope.mro = result
        return result

    def lookup_in_class(
        self, class_id: str, attr: str, *, skip_self: bool = False, dispatch: bool = False
    ) -> Node:
        """Resolve ``attr`` on a class through its in-scope MRO.

        A hit found after a class whose bases are not all known is marked
        uncertain: an override in the unknown part of the hierarchy could
        win, so the edge is recorded together with a name-bounded unresolved
        reference. Not found anywhere yields the unresolved reference alone.

        With ``dispatch`` (a lookup on ``self``/``cls``) the receiver may be
        an instance of any in-scope subclass, so every subclass that defines
        ``attr`` itself is returned as an override to record as well.
        """
        uncertain = False
        for cid in self._mro(class_id)[1 if skip_self else 0 :]:
            cscope = self.class_scopes[cid]
            hit: Resolved | None = None
            if attr in cscope.members:
                hit = Resolved(cscope.members[attr], uncertain_attr=attr if uncertain else "")
            elif attr in cscope.bindings:
                hit = Resolved(
                    cid, detail=f"attribute:{attr}", uncertain_attr=attr if uncertain else ""
                )
            if hit is not None:
                if dispatch:
                    hit = Resolved(
                        hit.symbol,
                        hit.detail,
                        hit.uncertain_attr,
                        overrides=self._overrides_of(class_id, attr, (hit.symbol, hit.detail)),
                    )
                return hit
            if not cscope.complete:
                uncertain = True
        return Unresolved(UNRESOLVED_ATTRIBUTE, attr)

    def _external_callers(self) -> None:
        """A class whose MRO has a base outside the source roots (a
        transport, a handler, a visitor) may have any method called by that
        code, which nothing in the source roots shows: the class depends on
        every method it defines, like its special methods. The generic
        bases of ``Base[T]`` are not in the MRO; their status counts too."""
        for class_id in sorted(self.class_scopes):
            cscope = self.class_scopes[class_id]
            related = set(self._mro(class_id))
            for chain, name in zip(cscope.base_chains, cscope.base_names, strict=True):
                if chain is None and name is not None:  # ``Base[T]``
                    node = self._resolve_base_expr(name, cscope)
                    if isinstance(node, Resolved) and node.symbol in self.class_scopes:
                        related |= set(self._mro(node.symbol))
            if not any(self.class_scopes[c].external_base for c in related):
                continue
            for member, member_id in sorted(cscope.members.items()):
                symbol = self.index.symbols.get(member_id)
                if symbol is None or symbol.kind != METHOD or _is_special_method(member):
                    continue
                if member in _EXPLICIT_SPECIAL_METHODS:
                    continue
                self.out.edges.add(Edge(class_id, member_id, REFERENCES, "external_base"))

    def _build_descendants(self) -> None:
        subclasses: dict[str, set[str]] = defaultdict(set)
        for cscope in self.class_scopes.values():
            for base in cscope.bases:
                subclasses[base].add(cscope.id)
        for class_id in self.class_scopes:
            seen = {class_id}
            order: list[str] = []
            stack = [class_id]
            while stack:
                for sub in sorted(subclasses.get(stack.pop(), ())):
                    if sub not in seen:
                        seen.add(sub)
                        order.append(sub)
                        stack.append(sub)
            self._descendants[class_id] = tuple(order)

    def _overrides_of(
        self, class_id: str, attr: str, base_hit: tuple[str, str]
    ) -> tuple[tuple[str, str], ...]:
        """What ``attr`` resolves to on each in-scope descendant of
        ``class_id`` when that differs from the base hit: a method defined by
        the descendant, one it inherits from a mixin outside the base's
        hierarchy, or a class-attribute rebinding."""
        assert self._bases_final, "overrides need every class's bases resolved"
        found: list[tuple[str, str]] = []
        for sub in self._descendants.get(class_id, ()):
            hit = self.lookup_in_class(sub, attr)
            if isinstance(hit, Resolved):
                pair = (hit.symbol, hit.detail)
                if pair != base_hit and pair not in found:
                    found.append(pair)
        return tuple(found)

    # -- pass 2 ---------------------------------------------------------------

    def _module_in_scope(self, name: str) -> bool:
        return name in self._module_prefixes

    def _resolve_module(self, scope: ModuleScope) -> None:
        assert scope.tree is not None  # parsed before a module is resolved
        module_scope = Scope(module=scope)
        # Module-level imports -> init-time edges.
        for node in scope.import_nodes:
            self._import_edges(scope.name, scope, node)
        top_level = [
            s
            for s in scope.tree.body
            if not isinstance(s, DEF_NODES + (ast.Import, ast.ImportFrom))
        ]
        collector = _ReferenceCollector(self, scope.name, module_scope, skip_defs=True)
        variable_ids = {
            id(stmt): symbol
            for name, stmt in scope.variable_stmts.items()
            for symbol in [scope.variables[name]]
        }
        for stmt in top_level:
            owner = variable_ids.get(id(stmt))
            if owner is not None:
                # The right-hand side's references belong to the variable symbol.
                _ReferenceCollector(self, owner, module_scope, skip_defs=True).visit(stmt)
            else:
                collector.visit(stmt)
        self._resolve_definitions(scope, scope.tree.body, scope.members, None)

    def _resolve_definitions(
        self,
        scope: ModuleScope,
        body: list[ast.stmt],
        members: dict[str, str],
        class_scope: ClassScope | None,
    ) -> None:
        for stmt in iter_scope_statements(body):
            if isinstance(stmt, ast.ClassDef):
                symbol_id = members.get(stmt.name)
                if symbol_id is None or symbol_id not in self.class_scopes:
                    continue
                cscope = self.class_scopes[symbol_id]
                class_level = Scope(module=scope, locals=set(cscope.bindings))
                # The class statement (bases, decorators, body) runs when the
                # module is imported, nested classes included: its references
                # are the class's and, as import-time code, the module's.
                collectors = [
                    _ReferenceCollector(self, source, class_level, skip_defs=True)
                    for source in (symbol_id, scope.name)
                ]
                for creator in (symbol_id, scope.name):
                    self.class_creation(creator, cscope.bases, stmt.keywords, Scope(module=scope))
                # Name-chain bases were resolved (and recorded) by _ensure_bases;
                # only dynamic base expressions still need their references collected.
                dynamic_bases = [b for b in stmt.bases if _flatten_chain(b) is None]
                for collector in collectors:
                    for expr in dynamic_bases + list(stmt.keywords) + list(stmt.decorator_list):
                        collector.visit(expr)
                    for inner in stmt.body:
                        if not isinstance(inner, DEF_NODES):
                            collector.visit(inner)
                attributes = _class_attributes(stmt)
                previous = self.out.class_attributes.get(symbol_id)
                if previous is not None:  # a conditional second definition
                    for name in previous.keys() | attributes.keys():
                        attributes[name] = _digest(
                            previous.get(name, "") + "|" + attributes.get(name, "")
                        )
                self.out.class_attributes[symbol_id] = attributes
                self.out.class_bases[symbol_id] = tuple(sorted(set(cscope.bases)))
                self._resolve_definitions(scope, stmt.body, cscope.members, cscope)
            elif isinstance(stmt, FUNC_NODES):
                symbol_id = members.get(stmt.name)
                if symbol_id is None:
                    continue
                self._resolve_function(scope, stmt, symbol_id, class_scope)

    def _resolve_function(
        self,
        scope: ModuleScope,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        symbol_id: str,
        class_scope: ClassScope | None,
    ) -> None:
        fscope = Scope(module=scope, locals=_LocalBindings().collect(node), literal_node=node)
        bound_method = class_scope is not None and not _is_staticmethod(node)
        if bound_method:
            params = node.args.posonlyargs + node.args.args
            if params:
                fscope.self_name = params[0].arg
                fscope.self_class = class_scope.id
                fscope.method = symbol_id
                fscope.self_is_class = _has_decorator(node, "classmethod")
        fscope.rebound = frozenset(_rebound_names(node))
        positional = [a.arg for a in node.args.posonlyargs + node.args.args]
        fscope.params = {name: i for i, name in enumerate(positional)}
        fscope.params.update({a.arg: None for a in node.args.kwonlyargs})
        defaults: dict[str, tuple[str, ...] | None] = {}
        n_defaults = len(node.args.defaults)
        for name, default in zip(
            positional[len(positional) - n_defaults :], node.args.defaults, strict=True
        ):
            defaults[name] = fscope.string_candidates(default)
        for a, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            if default is not None:
                defaults[a.arg] = fscope.string_candidates(default)
        self.out.func_params[symbol_id] = _FuncParams(
            positional=positional,
            bound=bound_method,
            defaults=defaults,
            has_varargs=node.args.vararg is not None or node.args.kwarg is not None,
        )
        returned = self._returned_class(node, fscope)
        if returned is not None:
            self.out.returns[symbol_id] = returned
        # Function-local imports are visible to the whole body.
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Import, ast.ImportFrom)):
                stars: list[str] = []
                self._register_imports(scope, inner, fscope.local_imports, stars)
                self._import_edges(symbol_id, scope, inner, local=True)
        # Decorators, defaults and annotations evaluate where the ``def``
        # statement runs, so the function's own parameters must not shadow
        # them (``def f(info=info)`` refers to the module-level ``info``).
        outer_scope = Scope(
            module=scope,
            locals=set(class_scope.bindings) if class_scope is not None else set(),
        )
        outer = _ReferenceCollector(self, symbol_id, outer_scope, skip_defs=True)
        # Decorators, defaults and eagerly evaluated annotations run when the
        # ``def`` does, i.e. when the module is imported (a decorator such as
        # ``@app.get("/")`` calls into a framework then): the module depends on
        # what they reference too.
        at_import = _ReferenceCollector(self, scope.name, outer_scope, skip_defs=True)
        for expr in (
            node.decorator_list
            + [d for d in node.args.defaults]
            + [d for d in node.args.kw_defaults if d is not None]
        ):
            at_import.visit(expr)
        if not _future_annotations(scope):
            for arg in ast.walk(node.args):
                if isinstance(arg, ast.arg) and arg.annotation is not None:
                    at_import.visit(arg.annotation)
            if node.returns is not None:
                at_import.visit(node.returns)
        for dec in node.decorator_list:
            outer.visit(dec)
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for arg in all_args + [a for a in (node.args.vararg, node.args.kwarg) if a]:
            if arg.annotation is not None:
                outer.visit(arg.annotation)
        if node.returns is not None:
            outer.visit(node.returns)
        for name, default in zip(
            positional[len(positional) - n_defaults :] + [a.arg for a in node.args.kwonlyargs],
            list(node.args.defaults) + list(node.args.kw_defaults),
            strict=True,
        ):
            if default is None:
                continue
            outer.visit(default)
            target = (
                self.resolve_chain(parts, outer_scope)
                if (parts := _flatten_chain(default))
                else None
            )
            if isinstance(target, Resolved) and not target.detail:
                aliased = self.index.symbols.get(target.symbol)
                if aliased is not None and aliased.kind == VARIABLE:
                    fscope.param_aliases[name] = aliased.id
        collector = _ReferenceCollector(self, symbol_id, fscope)
        for stmt in node.body:
            collector.visit(stmt)

    def _import_edges(
        self, source: str, scope: ModuleScope, node: ast.stmt, local: bool = False
    ) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._module_import_edge(source, alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(scope, node.module, node.level)
            self._module_import_edge(source, base)
            if not self._module_in_scope(base):
                return
            for alias in node.names:
                if alias.name == "*":
                    continue
                target = self._step(ModuleNode(base), alias.name)
                if isinstance(target, ModuleNode):
                    self._module_import_edge(source, target.module)
                    continue
                self._record(
                    source,
                    target,
                    kind=IMPORTS_NAME if not local else REFERENCES,
                    chain=f"from {base} import {alias.name}",
                )

    def _module_import_edge(self, source: str, module: str) -> None:
        if not self._module_in_scope(module):
            if self._module_in_scope(module.split(".")[0]):
                self.out.unresolved.add(
                    UnresolvedReference(
                        source, UNRESOLVED_ATTRIBUTE, module.rsplit(".", 1)[-1], f"import {module}"
                    )
                )
            else:
                self.out.external.add(ExternalReference(source, module))
            return
        parts = module.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix in self.scopes and prefix != source:
                self.out.edges.add(Edge(source, prefix, IMPORTS))

    # -- resolution --------------------------------------------------------------

    def _lookup_base(self, name: str, scope: Scope) -> Node:
        if scope.self_name is not None and name == scope.self_name and scope.self_class:
            return Resolved(scope.self_class, receiver=True)
        if name in scope.local_imports:
            return self._import_binding_node(scope.local_imports[name])
        if name in scope.locals:
            if name in scope.param_aliases:
                return Resolved(scope.param_aliases[name])
            return Local()
        found = self._lookup_in_module(scope.module, name, set())
        if found is not None:
            return found
        if name in BUILTIN_NAMES or (name.startswith("__") and name.endswith("__")):
            return None
        return Unresolved(UNRESOLVED_NAME, name)

    def _lookup_in_module(self, target: ModuleScope, name: str, seen: set[str]) -> Node:
        """Resolve ``name`` as seen from inside ``target``'s global namespace.

        Order: own definitions, import aliases, module-level variables,
        submodules, then star imports. Every analysed star-imported module is
        consulted before an external star import is blamed, so an in-scope
        symbol is never misattributed to a third-party package.
        """
        if name in target.members:
            return Resolved(target.members[name])
        if name in target.imports:
            return self._import_binding_node(target.imports[name])
        if name in target.variables:
            return Resolved(target.variables[name])
        if name in target.bindings:
            return Resolved(target.name, detail=f"attribute:{name}")
        if self._module_in_scope(f"{target.name}.{name}"):
            return ModuleNode(f"{target.name}.{name}")
        external: str | None = None
        for star in target.star_imports:
            if star in seen:
                continue
            seen.add(star)
            star_scope = self.scopes.get(star)
            if star_scope is None:
                if external is None and not self._module_in_scope(star):
                    external = star
                continue
            found = self._lookup_in_module(star_scope, name, seen)
            if isinstance(found, External):
                external = external or found.module
            elif found is not None:
                return found
        return External(external) if external is not None else None

    def _import_binding_node(self, binding: ImportBinding) -> Node:
        if not self._module_in_scope(binding.module):
            if self._module_in_scope(binding.module.split(".")[0]):
                # ``pkg.missing`` inside an analysed package: not an external
                # dependency, but nothing we can see either (deleted module,
                # compiled extension, generated code).
                name = binding.attr or binding.module.rsplit(".", 1)[-1]
                return Unresolved(UNRESOLVED_ATTRIBUTE, name)
            return External(binding.module)
        node: Node = ModuleNode(binding.module)
        if binding.attr is not None:
            # ``pkg/__init__.py: from pkg import ext`` with no ``ext`` module
            # (a compiled extension) resolves through itself: stop there.
            key = (binding.module, binding.attr)
            if key in self._resolving_bindings:
                return Unresolved(UNRESOLVED_ATTRIBUTE, binding.attr)
            self._resolving_bindings.add(key)
            try:
                node = self._step(node, binding.attr)
            finally:
                self._resolving_bindings.discard(key)
        return node

    def _step(self, node: Node, attr: str) -> Node:
        """Resolve one attribute access on a resolved node."""
        if isinstance(node, ModuleNode):
            sub = f"{node.module}.{attr}"
            target = self.scopes.get(node.module)
            if self._module_in_scope(sub):
                # A package binding of the same name wins at runtime once the
                # package has run (it binds after importing the submodule);
                # either may be meant, so the attribute denotes both.
                shadow: Resolved | None = None
                if target is not None:
                    symbol_id = target.members.get(attr) or target.variables.get(attr)
                    if symbol_id is not None:
                        shadow = Resolved(symbol_id)
                    elif attr in target.imports:
                        # ``from .main import main``: the re-exported name.
                        bound = self._import_binding_node(target.imports[attr])
                        if isinstance(bound, Resolved) and bound.symbol != sub:
                            shadow = Resolved(bound.symbol, bound.detail)
                    elif attr in target.bindings:
                        shadow = Resolved(target.name, detail=f"attribute:{attr}")
                if shadow is not None and sub in self.scopes:
                    return Resolved(shadow.symbol, shadow.detail, also=(sub,))
                return ModuleNode(sub)
            if target is None:
                return Unresolved(UNRESOLVED_ATTRIBUTE, attr)
            found = self._lookup_in_module(target, attr, set())
            return found if found is not None else Unresolved(UNRESOLVED_ATTRIBUTE, attr)
        if isinstance(node, External):
            return node
        if isinstance(node, Resolved):
            if node.detail:
                return node  # attribute of an opaque module/class attribute: stop here
            symbol = self.index.symbols.get(node.symbol)
            if symbol is None:
                return node
            if symbol.kind == CLASS:
                return self.lookup_in_class(symbol.id, attr, dispatch=node.receiver)
            if symbol.kind == MODULE:
                return self._step(ModuleNode(symbol.id), attr)
            return node  # attribute on a function object
        if isinstance(node, Unresolved):
            return Unresolved(UNRESOLVED_ATTRIBUTE, attr)
        return None

    def resolve_chain(self, parts: list[str], scope: Scope) -> Node:
        node = self._lookup_base(parts[0], scope)
        if isinstance(node, Local):
            # ``obj.method`` on a local: the method name bounds what it may be.
            return Unresolved(UNRESOLVED_ATTRIBUTE, parts[1]) if len(parts) > 1 else None
        for attr in parts[1:]:
            if node is None:
                return None
            if isinstance(node, Resolved) and node.detail:
                return node
            node = self._step(node, attr)
        return node

    def resolve_chain_names(self, parts: list[str], scope: Scope) -> tuple[Node, tuple[str, ...]]:
        """Like resolve_chain, plus the attribute names after the point where
        resolution stopped (an unknown or opaque value: a local, a failed
        step, a variable, a function, a class-level binding). Each is looked
        up on a value of unknown type, so each is a name-bounded reference."""
        node = self._lookup_base(parts[0], scope)
        if isinstance(node, Local):
            if len(parts) < 2:
                return None, ()
            return Unresolved(UNRESOLVED_ATTRIBUTE, parts[1]), tuple(parts[2:])
        for i, attr in enumerate(parts[1:], 1):
            if node is None or isinstance(node, External):
                return node, ()
            if isinstance(node, Resolved):
                symbol = self.index.symbols.get(node.symbol)
                opaque = node.detail or (symbol is not None and symbol.kind not in (CLASS, MODULE))
                if opaque:
                    return node, tuple(parts[i:])
            node = self._step(node, attr)
            if isinstance(node, Unresolved):
                return node, tuple(parts[i + 1 :])
        return node, ()

    def class_creation(
        self, source: str, bases: list[str], keywords: list[ast.keyword], scope: Scope
    ) -> None:
        """Creating a class runs code of its bases and metaclass: the
        ``__init_subclass__`` that the new class's MRO finds after itself
        (the union of what each in-scope base finds covers it), and an
        in-scope metaclass's ``__new__`` and ``__init__``."""
        hooks: list[tuple[str, str]] = []
        for base in bases:
            hooks.append((base, "__init_subclass__"))
        for kw in keywords:
            parts = _flatten_chain(kw.value) if kw.arg == "metaclass" else None
            if parts is None:
                continue
            meta = self.resolve_chain(parts, scope)
            if isinstance(meta, Resolved) and not meta.detail and meta.symbol in self.class_scopes:
                hooks += [(meta.symbol, "__new__"), (meta.symbol, "__init__")]
        for class_id, name in hooks:
            hit = self.lookup_in_class(class_id, name)
            if isinstance(hit, Resolved) and not hit.detail and hit.symbol != source:
                self.out.edges.add(Edge(source, hit.symbol, REFERENCES, name))

    def escape(self, target: Resolved, *, classes: bool = True) -> None:
        """Record that ``target`` is used as a value (see _mark_escape)."""
        symbol = self.index.symbols.get(target.symbol)
        if symbol is None:
            return
        if symbol.kind in (FUNCTION, METHOD) or (classes and symbol.kind == CLASS):
            self.out.escapes.add(symbol.id)
            for override_id, detail in target.overrides:
                if not detail:
                    self.out.escapes.add(override_id)

    def escape_class_family(self, class_id: str) -> None:
        """The class of an instance of ``class_id``: any in-scope subclass."""
        self.out.escapes.add(class_id)
        self.out.escapes.update(self._descendants.get(class_id, ()))

    def _record(self, source: str, node: Node, kind: str = REFERENCES, chain: str = "") -> None:
        if node is None:
            return
        if isinstance(node, Resolved):
            if node.symbol != source:
                self.out.edges.add(Edge(source, node.symbol, kind, node.detail))
            if node.uncertain_attr:
                self.out.unresolved.add(
                    UnresolvedReference(source, UNRESOLVED_ATTRIBUTE, node.uncertain_attr, chain)
                )
            for symbol_id, detail in node.overrides:
                if symbol_id != source:
                    label = f"override:{detail}" if detail else "override"
                    self.out.edges.add(Edge(source, symbol_id, kind, label))
            for module in node.also:
                if module != source:
                    self.out.edges.add(Edge(source, module, kind, "module"))
            if kind == REFERENCES and not node.detail and node.symbol in self.class_scopes:
                # Using a class (``Foo(...)``, subclassing) runs its constructor.
                for hook in ("__init__", "__new__"):
                    found = self.lookup_in_class(node.symbol, hook)
                    if isinstance(found, Resolved) and found.symbol != source:
                        self.out.edges.add(Edge(source, found.symbol, REFERENCES, "constructor"))
        elif isinstance(node, ModuleNode):
            if node.module in self.scopes and node.module != source:
                self.out.edges.add(Edge(source, node.module, kind, "module"))
        elif isinstance(node, External):
            self.out.external.add(ExternalReference(source, node.module))
        elif isinstance(node, Unresolved):
            self.out.unresolved.add(UnresolvedReference(source, node.kind, node.name, chain))


def build_index(snapshot: Snapshot, module_cache=None) -> SourceIndex:
    return Indexer(snapshot, module_cache=module_cache).build()

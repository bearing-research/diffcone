"""The indexer: parses a snapshot's modules (pass 1, cacheable per module),
then resolves references into edges (pass 2). The work is layered, each
layer a subclass of the one below: state, symbols (pass 1), resolver
(pass 2), dynamics (bounds after pass 2), and here the pipeline."""

from __future__ import annotations

import ast
import json
from collections import defaultdict

from diffcone.indexer.dynamics import DynamicBounds
from diffcone.indexer.facts import (
    _facts_to_dict,
    _Output,
    _output_from_dict,
    _output_to_dict,
    _tuples,
)
from diffcone.indexer.literals import literal_keys
from diffcone.indexer.scopes import ClassScope, ImportBinding, ModuleScope
from diffcone.indexer.syntax import _digest, decode_source
from diffcone.model import REFERENCES, Edge, SourceIndex, Symbol
from diffcone.snapshot import Snapshot, child_modules, module_name_for


class Indexer(DynamicBounds):
    """Builds a SourceIndex from a snapshot: ``Indexer(snapshot).build()``."""

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
        self._registrations()
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

    def _registrations(self) -> None:
        """Edges to code a decorator or a base class may keep and call later:
        from the decorator's receiver (``show`` holds what
        ``@show.register(int)`` registers, ``app`` what ``@app.command``
        does), from each variable a decorator function writes into (the
        ``REG`` a ``@register`` fills), and from a base class whose
        ``__init_subclass__`` runs for its in-scope subclasses. Computed over
        the whole index on every build, as the class model's other global
        edges are."""
        writers: dict[str, set[str]] = defaultdict(set)
        for e in self.index.edges:
            if e.detail == "mutated_by":
                writers[e.target].add(e.source)
        for decorated, decorator, receiver in sorted(self._global.decorations):
            if receiver:
                self.index.edges.add(Edge(receiver, decorated, REFERENCES, "registers"))
            for variable in sorted(writers.get(decorator, ())):
                self.index.edges.add(Edge(variable, decorated, REFERENCES, "registers"))
        for cls, bases in sorted(self.index.class_bases.items()):
            stack, seen = list(bases), set(bases)
            while stack:
                base = stack.pop()
                if f"{base}.__init_subclass__" in self.index.symbols:
                    self.index.edges.add(Edge(base, cls, REFERENCES, "registers"))
                for up in self.index.class_bases.get(base, ()):
                    if up not in seen:
                        seen.add(up)
                        stack.append(up)

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
        alt_imports = {
            k: [ImportBinding(m, a) for m, a in v] for k, v in record["alt_imports"].items()
        }
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
            data["import_layout"] = tuple(data["import_layout"])
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
        scope.alt_imports = alt_imports
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
                for key in literal_keys(name):
                    target.literal_names[key] = None

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


def build_index(snapshot: Snapshot, module_cache=None) -> SourceIndex:
    return Indexer(snapshot, module_cache=module_cache).build()

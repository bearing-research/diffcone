"""The indexer: parses a snapshot's modules (pass 1, cacheable per module),
then resolves references into edges (pass 2). The work is layered, each
layer a subclass of the one below: state, symbols (pass 1), resolver
(pass 2), dynamics (bounds after pass 2), and here the pipeline."""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from dataclasses import replace

from diffcone.indexer.dynamics import DynamicBounds
from diffcone.indexer.facts import (
    _facts_to_dict,
    _Output,
    _output_from_dict,
    _output_to_dict,
    _tuples,
)
from diffcone.indexer.literals import literal_base, literal_keys
from diffcone.indexer.references import SETITEM
from diffcone.indexer.scopes import ClassScope, ImportBinding, ModuleScope
from diffcone.indexer.scripts import apply_scripts
from diffcone.indexer.syntax import _digest, decode_source
from diffcone.indexer.uses import ANY, DYN, MUT, STORE, USE, UseRecord
from diffcone.indexer.writes import apply_writes
from diffcone.model import (
    EXTERNAL_WRITTEN,
    GRAPH_HANDLE,
    GRAPH_MODULES,
    METHOD,
    REFERENCES,
    UNRESOLVED_DYNAMIC,
    WRITES,
    Edge,
    SourceIndex,
    Symbol,
    UnresolvedReference,
)
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
        self._apply_uses()
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
        apply_writes(self)
        self._external_lookups()
        self._table_imports()
        self._resolve_param_dynamics()
        apply_scripts(self)  # after the script parameters are bound
        self._registrations()
        self._write_only_references()
        # Classes whose instances (or the class itself) are handed to someone
        # else: whoever holds one may read any attribute off it by a name
        # nothing resolves, so holding it depends on its members.
        returns = self._global.returns
        self.index.escaped_values = set(self._global.escapes)
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

    def _write_only_references(self) -> None:
        """Settle the references that only change a module variable
        (``X.clear()``, ``X[k] = v``): one keeps the detail WRITES when the
        variable holds a builtin container nothing binds anew, so calling a
        container method on it does the same whatever it holds (an item
        store only on a dict, as a list's may raise on what it holds), and
        becomes an ordinary reference otherwise, and always when an in-scope
        class defines ``__del__`` (emptying a container runs the finalizers
        of what it held). Computed over the whole index on every build:
        whether something rebinds a variable is known only from every
        module."""
        rebound = self._global.rebound
        # Emptying a container drops what it held, which runs the finalizers
        # of in-scope classes (``__del__``): then what it held matters.
        finalizers = any(
            s.kind == METHOD and s.name == "__del__" for s in self.index.symbols.values()
        )
        for edge in sorted(e for e in self.index.edges if e.detail in (WRITES, SETITEM)):
            symbol = self.index.symbols.get(edge.target)
            kind = symbol.builtin_container if symbol is not None else ""
            settled = (
                kind != ""
                and not finalizers
                and edge.target not in rebound
                and (edge.detail == WRITES or kind == "dict")
            )
            self.index.edges.discard(edge)
            self.index.edges.add(replace(edge, detail=WRITES if settled else ""))

    def _external_lookups(self) -> None:
        """A lookup by a name nothing bounds on an external module
        (``getattr(logging, level)``, ``dir(builtins)``) finds what that
        module defines, which is outside the analysis, unless in-scope code
        stored something there: then it counts as the dynamic reference or
        reflection site it is. A write onto a module above or below the one
        looked at counts too (``logging.handlers.x = ...`` and
        ``getattr(logging, n)``), and so does one onto a module found at run
        time (``*``). Such a site's detail says so, and
        ``SourceIndex.external_sites`` keeps the symbols writing there."""
        written = set(self._global.external_writes)
        escaped: set[tuple[str, str]] = set()  # seen by lookups on it or below it
        below: set[tuple[str, str]] = set()  # seen by lookups strictly below it
        suffixes: set[tuple[str, str]] = set()  # ``.a.b``: any module ``*.a.b`` and below
        for mode, module, writer in self._use_external:
            if mode == "write":
                written.add((module, writer))
            elif mode == "escape":
                escaped.add((module, writer))
            elif mode == "below":
                below.add((module, writer))
            else:
                suffixes.add((module, writer))

        def matches(pattern: str, module: str) -> bool:
            # ``PREFIX*``: any module whose name starts with PREFIX (``*``: any).
            return pattern.endswith(ANY) and module.startswith(pattern[:-1])

        def writers(module: str) -> set[str]:
            return (
                {
                    writer
                    for w, writer in written
                    if w == module
                    or matches(w, module)
                    or w.startswith(module + ".")
                    or module.startswith(w + ".")
                }
                # A module handed on may be written by whoever gets it, and so
                # may every module reachable from it as an attribute.
                | {
                    w
                    for e, w in escaped
                    if e == module or matches(e, module) or module.startswith(e + ".")
                }
                | {w for e, w in below if matches(e, module) or module.startswith(e + ".")}
                | {w for e, w in suffixes if e + "." in "." + module + "."}
            )

        # Code that hands a module walking the object graph on (``walk(gc)``)
        # or looks a name up on it by a name nothing bounds: the evidence
        # planner cannot tell which of its functions that code calls.
        for mode, module, writer in self._use_external:
            if mode in ("escape", "below") and module.split(".")[0] in GRAPH_MODULES:
                self.out.reflection.add((writer, GRAPH_HANDLE))
        for symbol, module, _kind, _detail in self._global.external_lookups:
            if module.split(".")[0] in GRAPH_MODULES:
                self.out.reflection.add((symbol, GRAPH_HANDLE))
        for symbol, module, kind, detail in sorted(self._global.external_lookups):
            by = writers(module)
            if not by:
                continue
            detail += EXTERNAL_WRITTEN
            if kind == "reflection":
                self.out.reflection.add((symbol, detail))
            else:
                self.out.unresolved.add(UnresolvedReference(symbol, UNRESOLVED_DYNAMIC, "", detail))
            known = self.index.external_sites.get((symbol, detail), ())
            self.index.external_sites[(symbol, detail)] = tuple(sorted(by.union(known)))

    def _table_imports(self) -> None:
        """Each dynamic import whose name a literal table of its module would
        bound, with the code whose uses may change that module's tables
        (every unbound one of them: more writers only weaken what evidence
        mode concludes)."""
        for symbol, detail, names in sorted(self._global.table_imports):
            found = self.index.symbols.get(symbol)
            scope = self.scopes.get(found.module) if found is not None else None
            if scope is None or not scope.literal_writers:
                continue
            writers = sorted(set().union(*scope.literal_writers.values()))
            self.index.table_imports[(symbol, detail)] = (names, tuple(writers))

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
        uses = tuple(UseRecord.from_list(r) for r in record["uses"])
        containers = frozenset(record["containers"])
        literal_sources = {k: tuple(v) for k, v in record["literal_sources"].items()}
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
        scope.uses, scope.containers = uses, containers
        scope.literal_sources = literal_sources
        scope.env_digest = env_digest
        for symbol in symbols:
            self._add_symbol(symbol)
        self.class_scopes.update(classes)
        self.out.edges |= edges
        return True

    def _apply_uses(self) -> None:
        """Every module's uses of what other modules hold (pass 1's
        UseRecords), resolved over the whole tree: a literal table some use
        may change is unbounded everywhere it is read, and so is every name
        whose values were taken from it; a module that may be written to
        has none of its tables bounded. A record whose target was computed
        from a literal table that turns out to change is about any module.
        Records about modules outside the source roots are kept for
        ``_external_lookups``."""
        # Each fact keeps the writers (the symbols whose uses gave it): a
        # name that unbinds is attributed to the code that may change it.
        poison: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )  # module -> literal name -> writers
        # Modules none of whose literal names hold, modules none of whose
        # containers hold, names no module's literal of that name holds,
        # names no module's container of that name holds.
        whole: dict[str, set[str]] = defaultdict(set)
        tables: dict[str, set[str]] = defaultdict(set)
        named: dict[str, set[str]] = defaultdict(set)
        named_tables: dict[str, set[str]] = defaultdict(set)
        anything: set[str] = set()  # the writers that may change any module
        external: set[tuple[str, str, str]] = set()  # (mode, module, writer)

        def changed(module: str, name: str) -> bool:
            return bool(
                anything
                or module in whole
                or name in poison[module]
                or name in named
                or (
                    (module in tables or name in named_tables)
                    and name in self.scopes[module].containers
                )
            )

        def writers_of(module: str, name: str) -> set[str]:
            found = set(anything) | whole.get(module, set()) | named.get(name, set())
            found |= poison[module].get(name, set())
            if name in self.scopes[module].containers:
                found |= tables.get(module, set()) | named_tables.get(name, set())
            return found

        def under(module: str) -> list[str]:
            return [m for m in self.scopes if m == module or m.startswith(module + ".")]

        def size() -> tuple[int, ...]:
            def count(facts: dict[str, set[str]]) -> int:
                return sum(len(v) + 1 for v in facts.values())

            sizes = (count(whole), count(tables), count(named), count(named_tables))
            held = sum(count(v) for v in poison.values())
            return (len(anything), *sizes, held)

        def concrete(module: str, kind: str, target: str, writer: str) -> None:
            """A record about a dotted name (no pattern)."""
            parts = target.split(".")
            if kind == STORE:
                parent, name = parts[:-1], parts[-1]
                places = self._locate_target(module, parent)
                if places is None:
                    external.add(("write", ".".join(parent), writer))
                for place, rest in places or ():
                    if not rest:
                        poison[place][name].add(writer)
                return
            places = self._locate_target(module, parts)
            if places is None:
                if kind == USE:
                    external.add(("escape", target, writer))
                elif kind == DYN:
                    external.add(("below", target, writer))
                return
            for place, rest in places:
                if not rest:
                    if kind == USE:
                        for m in under(place):
                            whole[m].add(writer)
                            self.index.escaped_modules.add(m)
                    elif kind == DYN:
                        tables[place].add(writer)
                        for m in under(place):
                            if m != place:
                                whole[m].add(writer)
                                self.index.escaped_modules.add(m)
                elif len(rest) == 1 and rest[0] in self.scopes[place].containers:
                    poison[place][rest[0]].add(writer)

        records = [(m, r) for m in sorted(self.scopes) for r in self.scopes[m].uses]
        while True:
            before = size()
            for module, record in records:
                kind, target = record.kind, record.target
                writer = self._writer_symbol(module, record.writer)
                lost = [n for n in record.sources if changed(module, n)]
                if lost:
                    # Computed from a table that may change: about any module,
                    # once the table's own writers ran (they are what it is
                    # attributed to). What it does with the table as it is
                    # still counts, below.
                    for name in lost:
                        anything.update(writers_of(module, name))
                    external.add(("escape", ANY, writer))
                if target == ANY:
                    if kind in (USE, DYN):
                        anything.add(writer)
                        external.add(("escape", ANY, writer))
                    continue
                if target.startswith(ANY + "."):
                    # An attribute of a module found at run time: whichever
                    # module's literal of that name (rebound or changed in
                    # place), or a submodule of that name handed on.
                    chain = target.split(".")[1:]
                    if kind == STORE:
                        named[chain[-1]].add(writer)
                        external.add(("write", ANY, writer))
                    elif kind == MUT:
                        named_tables[chain[-1]].add(writer)
                    else:
                        suffix = "." + ".".join(chain)
                        named_tables[chain[-1]].add(writer)
                        for m in self.scopes:
                            if ("." + m).endswith(suffix):
                                if kind == USE:
                                    for u in under(m):
                                        whole[u].add(writer)
                                else:
                                    tables[m].add(writer)
                                    for u in under(m):
                                        if u != m:
                                            whole[u].add(writer)
                        external.add(("suffix", suffix, writer))
                    continue
                if ANY in target:
                    # A module whose name starts with a literal prefix
                    # (``import_module(f"plugins.{name}")``): each in-scope
                    # module with that prefix, and any module outside.
                    prefix, _, rest = target.partition(ANY)
                    for m in sorted(self.scopes):
                        if m.startswith(prefix):
                            concrete(module, kind, m + rest, writer)
                    mode = {USE: "escape", DYN: "escape", STORE: "write"}.get(kind)
                    if mode is not None:
                        external.add((mode, prefix + ANY, writer))
                    continue
                concrete(module, kind, target, writer)
            if size() == before:
                break
        self._use_external = external
        for module, scope in self.scopes.items():
            scope.literal_pristine = dict(scope.literal_names)
            present = {literal_base(k) for k in scope.literal_names}
            lost = {n: writers_of(module, n) for n in present if changed(module, n)}
            # What was read out of a table that changes is not what it was.
            while True:
                more = {
                    n: set().union(*(lost[t] for t in taken if t in lost))
                    for n, taken in scope.literal_sources.items()
                    if n not in lost and lost.keys() & set(taken)
                }
                if not more:
                    break
                lost.update(more)
            scope.literal_writers = {}
            for name, by in lost.items():
                for key in literal_keys(name):
                    if key in scope.literal_names:
                        if scope.literal_names[key] is not None:
                            scope.literal_writers[key] = frozenset(by)
                        scope.literal_names[key] = None

    def _locate_target(
        self, module: str, parts: list[str]
    ) -> list[tuple[str, tuple[str, ...]]] | None:
        """Where a dotted name a record of ``module`` gives lands: (module,
        the rest of the name inside it) for each in-scope module it may
        reach, following submodules and re-exports; None when it is outside
        the source roots. ``@NAME`` is a name ``module`` may have from a star
        import."""
        if parts and parts[0].startswith("@"):
            scope = self.scopes.get(module)
            found: list[tuple[str, tuple[str, ...]]] = []
            rest = (parts[0][1:], *parts[1:])
            for star in scope.star_imports if scope is not None else ():
                if star in self.scopes:
                    found += self._locate(star, rest, set())
            return found
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self.scopes:
                return self._locate(prefix, tuple(parts[i:]), set())
        return None

    def _locate(
        self, module: str, rest: tuple[str, ...], seen: set[tuple[str, tuple[str, ...]]]
    ) -> list[tuple[str, tuple[str, ...]]]:
        if (module, rest) in seen:
            return []
        seen.add((module, rest))
        if not rest:
            return [(module, rest)]
        scope = self.scopes[module]
        name = rest[0]
        found: list[tuple[str, tuple[str, ...]]] = []
        if f"{module}.{name}" in self.scopes:
            found += self._locate(f"{module}.{name}", rest[1:], seen)
        if name in scope.imports:
            for binding in [scope.imports[name], *scope.alt_imports.get(name, ())]:
                origin = binding.module
                if binding.attr is not None:
                    origin = f"{binding.module}.{binding.attr}"
                parts = [*origin.split("."), *rest[1:]]
                for i in range(len(parts), 0, -1):
                    prefix = ".".join(parts[:i])
                    if prefix in self.scopes:
                        found += self._locate(prefix, tuple(parts[i:]), seen)
                        break
        if not found and name not in scope.bindings and name not in scope.members:
            for star in scope.star_imports:
                if star in self.scopes:
                    found += self._locate(star, rest, seen)
        return found or [(module, rest)]

    def _writer_symbol(self, module: str, path: tuple[str, ...]) -> str:
        """The symbol code at ``path`` (enclosing def and class names) in
        ``module`` belongs to: the innermost one that is a symbol."""
        if path:
            head = self._member_id(module, path[0])
            for i in range(len(path), 0, -1):
                candidate = ".".join([head, *path[1:i]])
                if candidate in self.index.symbols:
                    return candidate
        return module

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

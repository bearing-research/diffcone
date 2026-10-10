"""The indexer: parses a snapshot's modules (pass 1, cacheable per module),
then resolves references into edges (pass 2). The work is layered, each
layer a subclass of the one below: state, symbols (pass 1), resolver
(pass 2), dynamics (bounds after pass 2), and here the pipeline."""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace

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
from diffcone.indexer.uses import (
    ANY,
    AS,
    CALL,
    DYN,
    HANDLE,
    HELD,
    INSTALL,
    LOADER,
    MUT,
    SCOPED,
    STORE,
    SWAP,
    USE,
    UseRecord,
)
from diffcone.indexer.writes import apply_writes
from diffcone.model import (
    ANY_MODULE,
    EXTERNAL_WRITTEN,
    GRAPH_HANDLE,
    GRAPH_MODULES,
    INSTALLED,
    INSTALLED_ANYWHERE,
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


@dataclass
class _Reach:
    """What is reachable as an attribute of a module (and its submodules)
    beyond them, through what their imports bind (``Indexer._attribute_reach``):
    modules, modules whose names a star import brings (their tables), literal
    tables of other modules, symbols, and names outside the source roots."""

    modules: set[str] = field(default_factory=set)
    stars: set[str] = field(default_factory=set)
    tables: set[tuple[str, str]] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    outside: set[str] = field(default_factory=set)


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
        self._module_reach()
        self._install_edges()
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
        A module handed on hands on what its imports reach
        (``_attribute_reach``); what is installed in ``sys.modules``, held
        only by its own module, or bounded by its own module's calls counts
        as the record kinds say (``uses.py``). Records about modules outside
        the source roots are kept for ``_external_lookups``."""
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
            return self._under(module)

        def hand_on(place: str, writer: str, *, dyn: bool = False, escape: bool = True) -> None:
            """A module handed on (``dyn``: any attribute of it handed on):
            it (``dyn``: its tables) and its submodules may be written, and so
            may everything reachable as an attribute of one of them: what
            their imports bind and star imports bring, transitively."""
            if dyn:
                tables[place].add(writer)
            for m in under(place):
                if dyn and m == place:
                    continue
                whole[m].add(writer)
                if escape:
                    self.index.escaped_modules.add(m)
            reach = self._attribute_reach(place)
            for m in reach.modules:
                whole[m].add(writer)
            for m in reach.stars:
                tables[m].add(writer)
            for m, name in reach.tables:
                poison[m][name].add(writer)
            for outside in reach.outside:
                external.add(("escape", outside, writer))

        def size() -> tuple[int, ...]:
            def count(facts: dict[str, set[str]]) -> int:
                return sum(len(v) + 1 for v in facts.values())

            sizes = (count(whole), count(tables), count(named), count(named_tables))
            held = sum(count(v) for v in poison.values())
            return (len(anything), *sizes, held)

        def concrete(module: str, kind: str, target: str, writer: str) -> None:
            """A record about a dotted name (no pattern)."""
            parts = target.split(".")
            if "__globals__" in parts:
                # ``f.__globals__``: the namespace of the module ``f`` is
                # defined in (re-exports followed), or, outside the source
                # roots, of the module it is read off.
                at = parts.index("__globals__")
                after = parts[at + 1 :]
                found = self._locate_target(module, parts[:at])
                owners = (
                    [place for place, _ in found]
                    if found is not None
                    else [".".join(parts[: max(at - 1, 1)])]
                )
                for owner in owners:
                    concrete(module, kind, ".".join([owner, *after]), writer)
                return
            if kind in (STORE, SCOPED):
                # A store undone after the test (SCOPED) is seen only during
                # the test that made it, which depends on what it stores: no
                # write onto an external module (audit round 3, W26), but a
                # literal it rebinds is not the literal it was.
                parent, name = parts[:-1], parts[-1]
                places = self._locate_target(module, parent)
                if places is None and kind == STORE:
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
                    if kind in (USE, DYN):
                        hand_on(place, writer, dyn=kind == DYN)
                elif len(rest) == 1 and rest[0] in self.scopes[place].containers:
                    poison[place][rest[0]].add(writer)

        self._reach_cache = {}
        installs: set[tuple[str, str]] = set()  # (module, writer): ``_install_edges``
        installs_anywhere: set[tuple[str, str]] = set()
        records = [(m, r) for m in sorted(self.scopes) for r in self.scopes[m].uses]
        # A handle installed under a name literals give (an AS record): what
        # names that module names the handle's module too.
        installed_as: dict[str, set[str]] = defaultdict(set)
        for _, r in records:
            if r.kind == AS:
                installed_as[r.target].update(n[1:] for n in r.sources if n.startswith(">"))
        if installed_as:
            redirected: list[tuple[str, UseRecord]] = []
            for m, r in records:
                if r.kind in (AS, INSTALL, SWAP):  # about the name itself
                    continue
                for name, modules in installed_as.items():
                    if r.target == name or r.target.startswith(name + "."):
                        rest = r.target[len(name) :]
                        redirected += [
                            (m, UseRecord(r.kind, t + rest, r.sources, r.writer))
                            for t in sorted(modules)
                        ]
            records += redirected
        # Private functions other code calls or holds (by a CALL record, an
        # import of the name, a use of it as a value, a star import of its
        # module): a bound taken from the calls in its own module (a LOADER
        # record) does not hold for them.
        reached: set[str] = {
            r.target for _, r in records if r.kind in (CALL, USE, DYN, MUT, STORE, SCOPED)
        }
        for scope in self.scopes.values():
            for binding in [
                *scope.imports.values(),
                *(b for bs in scope.alt_imports.values() for b in bs),
            ]:
                if binding.attr is not None:
                    reached.add(f"{binding.module}.{binding.attr}")
        starred = {star for scope in self.scopes.values() for star in scope.star_imports}
        # Every dotted prefix of what other code names (``mod._ser.x`` names
        # ``mod._ser``).
        named_prefixes = {
            ".".join(parts[:i])
            for target in reached
            for parts in [target.split(".")]
            for i in range(1, len(parts) + 1)
        }

        def held_elsewhere(module: str, holder: str) -> bool:
            # A module-level name or private function other code can reach:
            # named through the module or imported, or the module itself
            # held (handed on, any attribute of it handed on, star imported).
            return (
                f"{module}.{holder}" in named_prefixes
                or module in starred
                or module in whole
                or module in tables
            )

        while True:
            before = size()
            for module, record in records:
                kind, target, sources = record.kind, record.target, record.sources
                writer = self._writer_symbol(module, record.writer)
                if kind == HANDLE:
                    continue  # what a value may be, not a use (_attribute_modules)
                if kind == HELD:
                    # Held where only its module's code reads it, unless other
                    # code can reach the holder: then handed on.
                    holders = [n[1:] for n in sources if n.startswith("=")]
                    if not any(held_elsewhere(module, h) for h in holders):
                        continue
                    kind = USE
                    sources = tuple(n for n in sources if not n.startswith("="))
                lost = [n for n in sources if changed(module, n)]
                if lost:
                    # Computed from a table that may change: about any module,
                    # once the table's own writers ran (they are what it is
                    # attributed to). What it does with the table as it is
                    # still counts, below.
                    for name in lost:
                        anything.update(writers_of(module, name))
                    external.add(("escape", ANY, writer))
                if kind in (CALL, AS):
                    continue
                if kind == LOADER:
                    owner = target.rpartition(".")[0]
                    if held_elsewhere(owner, target.rpartition(".")[2]):
                        # Called elsewhere, or its module held by other code:
                        # what it loads may be any module.
                        anything.add(writer)
                        external.add(("escape", ANY, writer))
                    continue
                if kind in (INSTALL, SWAP):
                    # An object installed in ``sys.modules``: a later import
                    # of that name gets it, while the module's own code keeps
                    # its own namespace. A lookup on an external module of
                    # that name may find the project's objects; an in-scope
                    # module of that name counts as handed on (as a module
                    # made with ``types.ModuleType`` and installed there
                    # does); installed for good, its importers get what the
                    # installing code put there (``_install_edges``).
                    if target.endswith(ANY):
                        external.add(("escape", target, writer))
                        prefix = target[: -len(ANY)]
                        for m in sorted(self.scopes) if prefix else ():
                            if m.startswith(prefix):
                                concrete(module, USE, m, writer)
                        if kind == INSTALL:
                            # Installed for good under a name nothing bounds
                            # (or only by a prefix): an import of any module
                            # it may name gets it (audit round 3, W25).
                            installs_anywhere.update(
                                (m, writer) for m in self.scopes if m.startswith(prefix)
                            )
                        continue
                    concrete(module, USE, target, writer)
                    found = self._locate_target(module, target.split("."))
                    if kind == INSTALL and found:
                        installs.update((place, writer) for place, rest in found if not rest)
                    continue
                if target == ANY:
                    # A module found at run time handed on, or an object from
                    # anywhere (an unpickled one, a ``gc`` list's element)
                    # changed in place: it may be any module or table.
                    if kind in (USE, DYN, MUT):
                        anything.add(writer)
                        external.add(("escape", ANY, writer))
                    continue
                if target.startswith(ANY + "."):
                    # An attribute of a module found at run time: whichever
                    # module's literal of that name (rebound or changed in
                    # place), or a submodule of that name handed on.
                    chain = target.split(".")[1:]
                    if kind in (STORE, SCOPED):
                        named[chain[-1]].add(writer)
                        if kind == STORE:
                            external.add(("write", ANY, writer))
                    elif kind == MUT:
                        named_tables[chain[-1]].add(writer)
                    else:
                        suffix = "." + ".".join(chain)
                        named_tables[chain[-1]].add(writer)
                        for m in self.scopes:
                            if ("." + m).endswith(suffix):
                                hand_on(m, writer, dyn=kind != USE, escape=False)
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
        self._installed = installs
        self._installed_anywhere = installs_anywhere - installs
        self._attribute_modules(records, changed)
        self._module_writers(records, anything)
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

    def _attribute_modules(
        self, records: list[tuple[str, UseRecord]], changed: Callable[[str, str], bool]
    ) -> None:
        """What a value of unknown type may be, for the name matching of an
        attribute read off one (``SourceIndex.attribute_modules``, audit
        round 3, W20): an attribute read reaches a module-level name only
        through the module object, and code holds a module object only
        through an import statement (then the read resolves), by having it
        handed on (the escaped modules), or by a handle (``HANDLE``
        records). Each such module counts with its submodules and what its
        imports bind (``_attribute_reach``). A module named at run time by a
        name nothing bounds may be any module: handed on, for every read
        (``ANY_MODULE``); held, for the reads of the module holding it
        (``any_module_readers``)."""
        places: set[str] = set(self.index.escaped_modules)
        readers: set[str] = set()
        for module, record in records:
            if record.kind != HANDLE:
                continue
            target = record.target
            if target == ANY or any(changed(module, n) for n in record.sources):
                readers.add(module)
                continue
            names = [target]
            if ANY in target:
                prefix, _, rest = target.partition(ANY)
                names = [m + rest for m in sorted(self.scopes) if m.startswith(prefix)]
            for name in names:
                for place, rest in self._locate_target(module, name.split(".")) or ():
                    if not rest:
                        places.add(place)
        found: set[str] = set()
        if any(mode == "escape" and target == ANY for mode, target, _ in self._use_external):
            found.add(ANY_MODULE)
        for place in sorted(places):
            found.update(self._under(place))
            reach = self._attribute_reach(place)
            found |= reach.modules | reach.stars | reach.symbols
        self.index.attribute_modules = found
        self.index.any_module_readers = readers

    def _module_writers(self, records: list[tuple[str, UseRecord]], anything: set[str]) -> None:
        """What may put objects on each in-scope module for later code
        (``SourceIndex.module_writers``, audit round 3, W24): a store of one
        of its attributes (not one undone after the test, SCOPED), the module
        (or a module above it) handed on, which whoever gets it may write,
        and an object installed for good under its name; under a run-time
        name nothing bounds, any module, as for the code that may change
        the literal table a name was computed from (``anything``)."""
        writers: dict[str, set[str]] = defaultdict(set)
        writers[ANY_MODULE] |= anything
        for module, record in records:
            kind, target = record.kind, record.target
            if kind not in (STORE, USE, DYN, HELD, INSTALL):
                continue
            writer = self._writer_symbol(module, record.writer)
            if target == ANY or target.startswith(ANY + "."):
                writers[ANY_MODULE].add(writer)
                continue
            names = [target]
            if ANY in target:
                prefix, _, rest = target.partition(ANY)
                names = [m + rest for m in sorted(self.scopes) if m.startswith(prefix)]
            for name in names:
                parts = name.split(".")
                if kind == STORE:
                    parts = parts[:-1]
                for place, rest in self._locate_target(module, parts) or ():
                    if rest:
                        continue
                    for m in [place] if kind in (STORE, INSTALL) else self._under(place):
                        writers[m].add(writer)
        self.index.module_writers = {
            m: tuple(sorted(w - {m})) for m, w in sorted(writers.items()) if w - {m}
        }

    def _under(self, module: str) -> list[str]:
        """``module`` and its submodules that are in scope."""
        if not hasattr(self, "_under_map"):
            found: dict[str, list[str]] = defaultdict(list)
            for m in sorted(self.scopes):
                parts = m.split(".")
                for i in range(1, len(parts) + 1):
                    found[".".join(parts[:i])].append(m)
            self._under_map = found
        return self._under_map.get(module, [])

    def _attribute_reach(self, place: str) -> _Reach:
        """What code holding module ``place`` can reach as an attribute chain
        off it beyond ``place`` and its submodules (audit round 3, W16): what
        the imports of each module on the way bind (``import pkg.core`` binds
        ``pkg``, every imported submodule with it), a star import's names,
        transitively."""
        cached = self._reach_cache.get(place)
        if cached is not None:
            return cached
        reach = _Reach()
        own = set(self._under(place))
        seen: set[str] = set()
        stack = sorted(own)
        while stack:
            module = stack.pop()
            if module in seen:
                continue
            seen.add(module)
            scope = self.scopes[module]
            origins: list[str] = []
            for name in sorted(set(scope.imports) | set(scope.alt_imports)):
                bindings = [scope.imports[name]] if name in scope.imports else []
                for binding in [*bindings, *scope.alt_imports.get(name, ())]:
                    origin = binding.module
                    if binding.attr is not None:
                        origin = f"{binding.module}.{binding.attr}"
                    origins.append(origin)
            for origin in origins:
                found = self._locate_target(module, origin.split("."))
                if found is None:
                    # A namespace package holds its imported submodules.
                    found = [(m, ()) for m in self._under(origin) if m != origin]
                    if not found:
                        reach.outside.add(origin)
                        continue
                for target, rest in found:
                    if not rest:
                        for m in self._under(target):
                            if m not in own:
                                reach.modules.add(m)
                                stack.append(m)
                        continue
                    symbol = ".".join([target, *rest])
                    if symbol in self.index.symbols:
                        reach.symbols.add(symbol)
                    if len(rest) == 1 and rest[0] in self.scopes[target].containers:
                        reach.tables.add((target, rest[0]))
            for star in scope.star_imports:
                if star in self.scopes:
                    if star not in own:
                        reach.stars.add(star)
                    stack.append(star)
                else:
                    reach.outside.add(star)
        self._reach_cache[place] = reach
        return reach

    def _install_edges(self) -> None:
        """An object installed in ``sys.modules`` for good under an in-scope
        module's name (``sys.modules["pkg.core"] = fake``): a later import of
        it gets what the installing code put there, so the module depends on
        that code (an edge from the module: impact on it reaches its
        importers). Under a name nothing bounds, every in-scope module
        depends on it, or every one the name's literal prefix allows (audit
        round 3, W25; evidence mode does not follow these edges: the record
        shows what an installed object ran)."""
        for module, writer in sorted(self._installed):
            if writer != module:
                self.index.edges.add(Edge(module, writer, REFERENCES, INSTALLED))
        for module, writer in sorted(self._installed_anywhere):
            if writer != module:
                self.index.edges.add(Edge(module, writer, REFERENCES, INSTALLED_ANYWHERE))

    def _module_reach(self) -> None:
        """For each module referenced as a value, what is reachable as an
        attribute chain off it beyond its own members (audit round 3, W16):
        the modules and symbols its imports bind (``SourceIndex.
        module_reach``; the planner adds them to what the referrers of an
        escaped module depend on, as W9 does its members)."""
        for edge in self.index.edges:
            if edge.kind != REFERENCES or edge.target not in self.scopes:
                continue
            if edge.target in self.index.module_reach:
                continue
            reach = self._attribute_reach(edge.target)
            found = reach.modules | reach.stars | reach.symbols
            if found:
                self.index.module_reach[edge.target] = tuple(sorted(found))

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
        paths = {scope.path for scope in self.scopes.values()}
        material = [
            sorted(self.snapshot.source_roots),
            [[m, self.scopes[m].env_digest] for m in sorted(self.scopes)],
            [[c, cs.bases, cs.complete] for c, cs in sorted(self.class_scopes.items())],
            # A loader of a path with a literal prefix reads which ``.py``
            # files no module name maps to (references._file_loader).
            sorted(
                p for p in self.snapshot.files if p.endswith((".py", ".pyw")) and p not in paths
            ),
        ]
        return _digest(json.dumps(material))


def build_index(snapshot: Snapshot, module_cache=None) -> SourceIndex:
    return Indexer(snapshot, module_cache=module_cache).build()

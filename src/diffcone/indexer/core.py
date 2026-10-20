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
    symbol_from_dict,
)
from diffcone.indexer.lazy import decide as decide_lazy_constants
from diffcone.indexer.literals import EXTERNAL_DEPENDENCY, literal_base, literal_keys
from diffcone.indexer.references import RESULT, SETITEM, instance_root
from diffcone.indexer.scopes import ClassScope, ImportBinding, ModuleScope
from diffcone.indexer.scripts import apply_scripts
from diffcone.indexer.syntax import TOO_DEEP, _digest, decode_source
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
    MUTATED_BY,
    REBINDS,
    REGISTERS,
    WRITES,
    Edge,
    EdgeKind,
    SourceIndex,
    SymbolKind,
    UnresolvedKind,
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
            except RecursionError:
                # Too deeply nested for the recursive AST walks: the module is
                # left out as an unparsable one is, which degrades the plan.
                self._drop_module(scope)
                continue
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
        for module in self.index.modules:
            parts = module.split(".")
            for i in range(1, len(parts) + 1):
                self._module_prefixes.add(".".join(parts[:i]))
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
                except RecursionError:
                    # Its symbols stay, with nothing known of what they use:
                    # the error degrades the plan, and nothing is cached.
                    self._too_deep(scope.path, scope.name)
                    continue
                finally:
                    out, self.out = self.out, self._global
                if key is not None:
                    new_resolved[key] = _output_to_dict(out)
            self._global.merge(out)
        try:
            apply_writes(self)
            self._result_writes()
            self._external_lookups()
            self._table_imports()
            self._resolve_param_dynamics()
            apply_scripts(self)  # after the script parameters are bound
            self._use_mutation_edges()
            self._value_writes()
            self._registrations()
            self._write_only_references()
            self._module_reach()
            self._install_edges()
        except RecursionError:
            self.out = self._global
            self._too_deep("", "")
            undecided = self._global.value_writes | self._global.receiver_writes
            undecided |= {(c, w) for c, w, _ in self._global.instance_writes}
            for state, writer in sorted(undecided):  # every one, undecided
                self.index.edges.add(Edge(state, writer, EdgeKind.REFERENCES, MUTATED_BY))
        self._lazy_constants()
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
        # Handed to code the analysis does not follow into (a library, a
        # receiver of unknown type, ``**x``: references._handed_instances).
        for kind, held in self._global.handed_instances:
            if kind == "class":
                self.index.escaped_classes.add(held)
            else:
                self.index.escaped_classes.update(returns.get(held, ()))
        if cache is not None and (new_facts or new_resolved):
            cache.store(new_facts, new_resolved, fingerprint, list(keys.values()))
        return self.index

    def _lazy_constants(self) -> None:
        """Decide which pass-1 lazy-constant candidates are lazy constants
        (``SourceIndex.lazy_constants``, diffcone.indexer.lazy) over the
        whole index: computed from cached facts and edges alone, so a cold
        and a warm build agree. A variable some code other than its
        candidate writer writes (a ``mutated_by`` edge to other code,
        anything registered into it, a ``rebinds``/``writes`` reference from
        other code) is none."""
        found: list[tuple[str, str, str, str, tuple[str, tuple[str, ...], tuple[str, ...]]]] = []
        for module, scope in sorted(self.scopes.items()):
            for name, candidate in sorted(scope.lazy_candidates.items()):
                var, writer = scope.variables.get(name), scope.members.get(candidate[0])
                if var is None or writer is None:
                    continue
                var_symbol, writer_symbol = (
                    self.index.symbols.get(var),
                    self.index.symbols.get(writer),
                )
                if (
                    var_symbol is None
                    or var_symbol.kind != SymbolKind.VARIABLE
                    or writer_symbol is None
                    or writer_symbol.kind != SymbolKind.FUNCTION
                ):
                    continue
                found.append((module, name, var, writer, candidate))
        if not found:
            return
        writer_of = {var: writer for _, _, var, writer, _ in found}
        other_writes: set[str] = set()
        for edge in self.index.edges:
            writer = writer_of.get(edge.source)
            if writer is not None and edge.kind != EdgeKind.DEFINED_IN:
                if not (edge.detail == MUTATED_BY and edge.target == writer):
                    other_writes.add(edge.source)
            writer = writer_of.get(edge.target)
            if (
                writer is not None
                and edge.source != writer
                and edge.kind == EdgeKind.REFERENCES
                and edge.detail in (WRITES, REBINDS, SETITEM)
            ):
                other_writes.add(edge.target)
        bindings = {m: s.bindings | set(s.imports) for m, s in self.scopes.items()}
        self.index.lazy_constants = decide_lazy_constants(
            found, self.index.modules, bindings, other_writes
        )

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
            s.kind == SymbolKind.METHOD and s.name == "__del__" for s in self.index.symbols.values()
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

        # What the project imports by name (an ``import`` statement anywhere,
        # the module a ``from`` import reads from, a module-level ``from os
        # import path``, a literal ``import_module``/``__import__``). Code
        # handed something below a looked-up module may change what the
        # lookup finds only when that is a module, and one is a module only
        # when imported: by the project, under one of these names. Anything
        # else (``dt.datetime``, ``dt.timezone.utc``) is an object of the
        # library, and a library changing an object it is handed is not
        # followed (indexer/writes.py). The lookups themselves are recorded
        # on the whole chain looked on (``getattr(os.path, n)``: ``os.path``).
        imported = {ref.module for ref in self._global.external}
        for scope in self.scopes.values():
            for name, binding in scope.imports.items():
                for b in (binding, *scope.alt_imports.get(name, ())):
                    imported.add(b.dotted)
            imported.update(
                r.target for r in scope.uses if r.kind == HANDLE and ANY not in r.target
            )

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
                # may every module reachable from it as an attribute; a
                # module below it (``imported``) counts as a write below does.
                | {
                    w
                    for e, w in escaped
                    if e == module
                    or matches(e, module)
                    or module.startswith(e + ".")
                    or (e.startswith(module + ".") and e in imported)
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
                self.out.unresolved.add(
                    UnresolvedReference(symbol, UnresolvedKind.DYNAMIC, "", detail)
                )
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
            if e.detail == MUTATED_BY:
                writers[e.target].add(e.source)
        self.index.decorator_symbols = {
            decorator for _, decorator, _ in self._global.decorations if decorator
        }
        for decorated, decorator, receiver in sorted(self._global.decorations):
            if decorator:
                symbol = self.index.symbols.get(decorator)
                if symbol is not None and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                    self.index.decorations.add((decorated, decorator))
            if receiver:
                self.index.edges.add(Edge(receiver, decorated, EdgeKind.REFERENCES, REGISTERS))
            for variable in sorted(writers.get(decorator, ())):
                self.index.edges.add(Edge(variable, decorated, EdgeKind.REFERENCES, REGISTERS))
        for cls, bases in sorted(self.index.class_bases.items()):
            stack, seen = list(bases), set(bases)
            while stack:
                base = stack.pop()
                if f"{base}.__init_subclass__" in self.index.symbols:
                    self.index.edges.add(Edge(base, cls, EdgeKind.REFERENCES, REGISTERS))
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
        except RecursionError:
            self._too_deep(path, module)
            return None

    def _too_deep(self, path: str, module: str) -> None:
        """Record that a module (or, with no path, the analysis across
        modules) nests too deeply for the recursive walks of its syntax tree:
        an analysis error, so the plan selects every target."""
        self._error(path, f"{TOO_DEEP}: {path or 'the analysis across modules'}")
        if module:
            self.index.failed_modules.add(module)

    def _drop_module(self, scope: ModuleScope) -> None:
        """Undo what pass 1 recorded of a module it could not finish, and
        leave the module out as an unparsable one is."""
        for symbol in self._added_symbols:
            if self.index.symbols.get(symbol.id) is symbol:
                del self.index.symbols[symbol.id]
        for cscope in self._added_classes:
            self.class_scopes.pop(cscope.id, None)
        del self.scopes[scope.name]
        self.index.modules.discard(scope.name)
        self._too_deep(scope.path, scope.name)

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
        immutables = {k: tuple(v) for k, v in record["immutables"].items()}
        literal_sources = {k: tuple(v) for k, v in record["literal_sources"].items()}
        lazy = {k: (str(w), tuple(n), tuple(d)) for k, (w, n, d) in record["lazy"].items()}
        env_digest = str(record["env"])
        symbols = [symbol_from_dict(data) for data in record["symbols"]]
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
                match_args=tuple(data["match_args"]) if data["match_args"] is not None else None,
                stateless=tuple(data["stateless"]) if data["stateless"] is not None else None,
            )
        edges = {Edge(s, t, EdgeKind(k), d) for s, t, k, d in record["edges"]}
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
        scope.immutables = immutables
        scope.literal_sources = literal_sources
        scope.lazy_candidates = lazy
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
        # Variable (or ``*.NAME``: every variable of the name) -> the code
        # that rebinds it through a module object however obtained, and the
        # code that changes what it holds in place (``_use_mutations``; a
        # store whose target a module found at run time may hold counts as
        # a rebinding).
        mutated: dict[str, set[str]] = defaultdict(set)
        changed_in_place: dict[str, set[str]] = defaultdict(set)
        # The code changing an object nothing names in place (an item stored
        # into an unpickled object, a ``gc`` list's element): it may be any
        # container variable.
        any_container: set[str] = set()

        def variable(place: str, name: str) -> str | None:
            found = self.index.symbols.get(f"{place}.{name}")
            return found.id if found is not None and found.kind == SymbolKind.VARIABLE else None

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

        def hand_on(place: str, writer: str, *, dyn: bool = False, escape: bool = True) -> None:
            """A module handed on (``dyn``: any attribute of it handed on):
            it (``dyn``: its tables) and its submodules may be written, and so
            may everything reachable as an attribute of one of them: what
            their imports bind and star imports bring, transitively."""
            if dyn:
                tables[place].add(writer)
            for m in self._under(place):
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
                    # The variable rebound, or an attribute of what it holds
                    # (not for a store undone after the test: later code
                    # does not see it, W26).
                    var = variable(place, (*rest, name)[0])
                    if var is not None and kind == STORE:
                        (changed_in_place if rest else mutated)[var].add(writer)
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
                    continue
                if len(rest) == 1 and rest[0] in self.scopes[place].containers:
                    poison[place][rest[0]].add(writer)
                # What a variable holds changed in place (an item, a mutator,
                # an item changed on the way). Handed on, it is followed into
                # the callee in pass 2 (indexer/writes.py).
                var = variable(place, rest[0])
                if var is not None and kind == MUT:
                    changed_in_place[var].add(writer)

        self._reach_cache = {}
        installs: set[tuple[str, str]] = set()  # (module, writer): ``_install_edges``
        installs_anywhere: set[tuple[str, str]] = set()
        records = self._redirect_installed(
            self._outside_stars([(m, r) for m in sorted(self.scopes) for r in self.scopes[m].uses])
        )
        named_prefixes = self._named_prefixes(records)
        starred = {star for scope in self.scopes.values() for star in scope.star_imports}

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
                    if kind in (MUT, STORE):
                        # Whichever module's variable of the name it names.
                        parts = target.split(".")
                        found = self._locate_target(module, parts)
                        names = (
                            {rest[0] for _, rest in found if rest}
                            if found is not None
                            else set(parts[1:])
                        )
                        for name in names:
                            held = mutated if kind == STORE else changed_in_place
                            held[f"{ANY}.{name}"].add(writer)
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
                    if kind == MUT:
                        any_container.add(writer)
                    continue
                if target.startswith(ANY + "."):
                    # An attribute of a module found at run time: whichever
                    # module's literal of that name (rebound or changed in
                    # place), or a submodule of that name handed on.
                    chain = target.split(".")[1:]
                    # Whichever module's variable of the first name holds
                    # what changed (``obj.OBJ.x = v`` changes ``OBJ``).
                    if kind == STORE and len(chain) == 1:
                        mutated[ANY + "." + chain[0]].add(writer)
                    elif kind in (MUT, STORE):
                        changed_in_place[ANY + "." + chain[0]].add(writer)
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
        self._use_mutations = self._mutations(mutated, set())
        self._use_value_mutations = self._mutations(changed_in_place, any_container)
        self._written_modules = set(whole)
        self._installed = installs
        self._installed_anywhere = installs_anywhere - installs
        self._attribute_modules(records, changed)
        self._module_writers(records, anything)
        self._unbind_literals(changed, writers_of)

    def _mutations(
        self, mutated: dict[str, set[str]], any_container: set[str]
    ) -> dict[str, set[str]]:
        """For each module-level variable, the in-scope code the use records
        say changes what it holds or rebinds it through a module object pass
        2 does not resolve to its module (a handle bounded to known modules:
        ``import_module("pkg.reg").TABLE[k] = v``, ``sys.modules[...]``, a
        function-local or star import); through a module found at run time
        (``import_module(name).TABLE[k] = v``, an unpickled object's
        attribute), whichever module's variable of the name written; and an
        item stored into, or a mutator called on, an object nothing names (an
        unpickled object, a ``gc`` list's element): any container variable.
        Code a module or a variable is handed to is followed in pass 2
        (``indexer/writes.py``); a library writing into one is not."""
        found: dict[str, set[str]] = {}
        for symbol in self.index.symbols.values():
            if symbol.kind != SymbolKind.VARIABLE:
                continue
            module, name = symbol.module, symbol.name
            by = set(mutated.get(symbol.id, ())) | mutated.get(f"{ANY}.{name}", set())
            scope = self.scopes.get(module)
            if symbol.builtin_container or (scope is not None and name in scope.containers):
                by |= any_container
            by.discard(symbol.id)
            if by:
                found[symbol.id] = by
        return found

    def _result_writes(self) -> None:
        """A write in place into what a call returns (``registry()[k] = v``,
        ``r = get_cache(); r.update(d)``, ``Config.instance().mode = m``)
        changes the state the callee returns, directly or through what it
        calls (``state_returns``): a ``mutated_by`` edge from each such
        module variable, class or function to the writer (audit round 6,
        T7). Code at a module's top level writing its own module's state is
        part of that module's hash, as in pass 2."""
        returned: dict[str, set[str]] = defaultdict(set)
        for function, root in self._global.state_returns:
            returned[function].add(root)
        closed: dict[str, set[str]] = {}

        def state_of(function: str) -> set[str]:
            found = closed.get(function)
            if found is None:
                found = closed[function] = set()
                seen, stack = {function}, [function]
                while stack:
                    for root in returned.get(stack.pop(), ()):
                        if not root.startswith(RESULT):
                            found.add(root)
                        elif (callee := root[len(RESULT) :]) not in seen:
                            seen.add(callee)
                            stack.append(callee)
            return found

        for writer, callee, module in sorted(self._global.result_writes):
            for root in sorted(state_of(callee)):
                made = instance_root(root)
                if made is not None:
                    # An instance the callee made (``return Store()``), or
                    # part of one: a write into it may be one into its
                    # class's state (T22).
                    how, cls = made
                    symbol = self.index.symbols.get(cls)
                    if symbol is not None and symbol.module != module:
                        how = how if how.startswith(".") else ""
                        self._global.instance_writes.add((cls, writer, how))
                    continue
                symbol = self.index.symbols.get(root)
                if symbol is None or root == writer or symbol.module == module:
                    continue
                self._global.value_writes.add((root, writer))

    def _value_writes(self) -> None:
        """The ``mutated_by`` edges of changes in place of what a module
        variable, class or function holds (``value_writes``), but none from a
        variable that cannot change in place (``_immutable_variables``): its
        readers read what they read before, whatever the writer does (audit
        round 6, T20)."""
        immutable = self._immutable_variables()
        classes = self._instance_classes()
        holding = self._classes_holding_state(immutable)
        for state, writer in sorted(self._global.receiver_writes - self._global.value_writes):
            if state not in immutable:
                self.index.edges.add(Edge(state, writer, EdgeKind.REFERENCES, MUTATED_BY))
        for state, writer in sorted(self._global.value_writes):
            if state in immutable:
                continue
            self.index.edges.add(Edge(state, writer, EdgeKind.REFERENCES, MUTATED_BY))
            # A write in place into an instance (``STORE._cache[k] = v``,
            # ``STORE.cache()[k] = v``) may be one into state its class
            # holds: a ``mutated_by`` edge from the class and its in-scope
            # bases when one of them holds any (audit round 6, T21).
            for cls in sorted(classes.get(state, ())):
                mro = self._mro(cls)
                if any(c in holding for c in mro):
                    for c in mro:
                        if c != writer:
                            self.index.edges.add(Edge(c, writer, EdgeKind.REFERENCES, MUTATED_BY))
        self._instance_writes(holding)

    def _instance_writes(self, holding: set[str]) -> None:
        """A write into an instance of an in-scope class the writer made
        (``s = Store(); s._items.append(x)``, ``new = cls.__new__(cls);
        new._cache[k] = v``), or into part of one, may be one into state
        its class holds: a ``mutated_by`` edge from the class and its
        in-scope bases when one of them holds any, as for a module variable
        holding an instance (T21). An attribute bound on the instance
        itself (``new._key = None``) changes that object alone when nothing
        but ``object.__new__`` made it (``_allocated_fresh``); otherwise
        (an in-scope or a library ``__new__``, a class decorator or
        metaclass, which may give back an object kept elsewhere) it counts
        as a write in place. A write reached through an attribute
        (``s._items.append(x)``: ``.``-prefixed ``how``) is one into the
        instance's own object when no class of the family binds or defines
        the attribute (``_own_attribute``) (audit round 6, T22)."""
        if not self._global.instance_writes:
            return
        written = {e.source for e in self.index.edges if e.detail == MUTATED_BY}
        written |= {state for state, _ in self._global.value_writes}
        written |= {state for state, _ in self._global.receiver_writes}
        own: dict[tuple[str, str], bool] = {}
        for cls, writer, how in sorted(self._global.instance_writes):
            if cls not in self.class_scopes:
                continue
            if how:
                if (cls, how) not in own:
                    if how.startswith("."):
                        own[(cls, how)] = self._own_attribute(cls, how[1:], written)
                    else:
                        own[(cls, how)] = self._allocated_fresh(cls, how)
                if own[(cls, how)]:
                    continue
            mro = self._mro(cls)
            if any(c in holding for c in mro):
                for c in mro:
                    if c != writer:
                        self.index.edges.add(Edge(c, writer, EdgeKind.REFERENCES, MUTATED_BY))

    def _family(self, cls: str) -> set[str]:
        """The in-scope classes an instance of ``cls`` or of an in-scope
        subclass of it may look an attribute up on."""
        family: set[str] = set()
        for c in (cls, *self._descendants.get(cls, ())):
            family.update(self._mro(c))
        return family

    def _own_attribute(self, cls: str, attr: str, written: set[str]) -> bool:
        """Whether ``attr`` of an instance of ``cls`` (or of a subclass) is
        the instance's own: no class of the family binds or defines it in
        its body (a class-level object, or a method or property whose result
        may be one), customises attribute lookup (``__getattr__``,
        ``__getattribute__``), has a base outside the index (``opaque``), or
        is stored onto by code (which may put ``attr`` on the class)."""
        for c in sorted(self._family(cls)):
            cscope = self.class_scopes[c]
            names = cscope.members.keys() | cscope.bindings
            if attr in names or "__getattr__" in names or "__getattribute__" in names:
                return False
            if cscope.opaque or c in written:
                return False
        return True

    def _allocated_fresh(self, cls: str, how: str) -> bool:
        """Whether an instance of ``cls`` (or of a subclass of it) made as
        ``how`` says (references.FRESH: ``call``, ``new``, ``super``) is a
        new object nothing else holds: no class in the MRO of ``cls`` or of
        a subclass defines ``__new__`` (for ``super``, but ``cls`` itself,
        whose own ``__new__`` calls it) or has a base outside the source
        roots that may (``external_base``; a builtin, typing or abc base
        does not), and, for a constructor call, none is decorated or has a
        metaclass (``plain``)."""
        family = self._family(cls)
        if how == "super":
            family.discard(cls)
        for c in sorted(family):
            cscope = self.class_scopes[c]
            if "__new__" in cscope.members or "__new__" in cscope.bindings:
                return False
            if cscope.external_base or (how == "call" and not cscope.plain):
                return False
        return True

    def _instance_classes(self) -> dict[str, set[str]]:
        """Module variable -> the in-scope classes it may hold an instance of
        (or be): what its bindings hold (``instance_bindings``), through
        what the functions called return (``returns``, ``state_returns``),
        other variables, and what such an instance holds under an attribute
        a method binds to an instance or a factory's result
        (``attr_writes``). Computed over the whole index on every build
        from facts a cold and a warm build share (audit round 6, T21)."""
        bindings: dict[str, set[str]] = defaultdict(set)
        for var, origin in self._global.instance_bindings:
            bindings[var].add(origin)
        if not bindings:
            return {}
        returned: dict[str, set[str]] = defaultdict(set)
        for function, root in self._global.state_returns:
            returned[function].add(root)
        returns, symbols = self._global.returns, self.index.symbols
        held: dict[str, set[str]] = defaultdict(set)
        for w in self._global.attr_writes:
            if w.binding is not None and w.binding[0] == "instance":
                held[w.cls].add(f"class:{w.binding[1]}")
            elif w.binding is not None and w.binding[0] == "factory":
                held[w.cls].add(f"call:{w.binding[1]}")
        found: dict[str, set[str]] = {}
        for start in sorted(bindings):
            classes: set[str] = set()
            seen: set[str] = set()
            stack = [f"var:{start}"]
            while stack:
                origin = stack.pop()
                if origin in seen:
                    continue
                seen.add(origin)
                kind, _, ident = origin.partition(":")
                if kind == "class":
                    if ident in self.class_scopes and ident not in classes:
                        classes.add(ident)
                        # What its instances hold under an attribute
                        # (``self.store = Store()``) is reached through them.
                        for c in self._mro(ident):
                            stack.extend(held.get(c, ()))
                elif kind == "var":
                    stack.extend(bindings.get(ident, ()))
                elif kind == "call":
                    classes.update(c for c in returns.get(ident, ()) if c in self.class_scopes)
                    for root in returned.get(ident, ()):
                        if root.startswith(RESULT):
                            stack.append("call:" + root[len(RESULT) :])
                        elif (symbol := symbols.get(root)) is not None:
                            if symbol.kind == SymbolKind.CLASS:
                                stack.append(f"class:{root}")
                            elif symbol.kind == SymbolKind.VARIABLE:
                                stack.append(f"var:{root}")
            if classes:
                found[start] = classes
        return found

    def _classes_holding_state(self, immutable: set[str]) -> set[str]:
        """The in-scope classes that may hold state of their own that can
        change in place: their body binds something that may
        (``ClassScope.stateless``: anything not immutable, or immutable only
        through a standard-library module a project module shadows or a
        module variable that is not immutable), or some code stores onto
        the class (a ``mutated_by`` edge of its own: ``cls._registry =
        {}``, ``Store._cache[k] = v``). A class with an external base may
        hold state the library defines, which only the evidence recorder
        sees (audit round 6, T21)."""
        modules = self.index.modules
        written = {e.source for e in self.index.edges if e.detail == MUTATED_BY}
        written |= {state for state, _ in self._global.value_writes}
        written |= {state for state, _ in self._global.receiver_writes}
        found: set[str] = set()
        for cls, cscope in self.class_scopes.items():
            rests = cscope.stateless
            holds = rests is None or cls in written
            for rest in rests or ():
                if holds:
                    break
                if rest.startswith(EXTERNAL_DEPENDENCY):
                    top = rest[len(EXTERNAL_DEPENDENCY) :]
                    holds = any(m == top or m.startswith(top + ".") for m in modules)
                else:
                    var = cscope.module.variables.get(rest)
                    holds = var is None or var not in immutable
            if holds:
                found.add(cls)
        return found

    def _immutable_variables(self) -> set[str]:
        """The module variables whose value nothing can change in place: the
        one binding of the name in its module is an immutable value
        (``ModuleScope.immutables``) resting only on standard-library modules
        no project module shadows and on such variables, and nothing may bind
        it anew: no code rebinds it (``global``, ``mod.X = v``, ``mod.X +=
        v``: ``rebound``, or a ``mutated_by`` edge of its own: a store through
        the module however obtained, the module handed to code writing it),
        and its module is handed to no code the analysis does not follow
        (``_written_modules``). Computed over the whole index on every build
        from facts and edges a cold and a warm build share."""
        rebound = self._global.rebound
        written = {e.source for e in self.index.edges if e.detail == MUTATED_BY}
        modules = self.index.modules
        candidates: dict[str, tuple[str, tuple[str, ...]]] = {}
        for module, scope in self.scopes.items():
            if module in self._written_modules:
                continue
            for name, rests in scope.immutables.items():
                var = scope.variables.get(name)
                symbol = self.index.symbols.get(var) if var is not None else None
                if symbol is not None and symbol.kind == SymbolKind.VARIABLE:
                    candidates[symbol.id] = (module, rests)
        decided: dict[str, bool] = {}

        def immutable(var: str, path: frozenset[str]) -> bool:
            if var in decided:
                return decided[var]
            found = var in candidates and var not in rebound and var not in written
            if found:
                module, rests = candidates[var]
                for rest in rests:
                    if rest.startswith(EXTERNAL_DEPENDENCY):
                        top = rest[len(EXTERNAL_DEPENDENCY) :]
                        if any(m == top or m.startswith(top + ".") for m in modules):
                            found = False  # a project module of that name
                    else:
                        other = self.scopes[module].variables.get(rest)
                        if other is None or other in path or not immutable(other, path | {var}):
                            found = False
                    if not found:
                        break
            if not path or found:  # a cycle's answer depends on where it was entered
                decided[var] = found
            return found

        return {var for var in sorted(candidates) if immutable(var, frozenset())}

    def _use_mutation_edges(self) -> None:
        """A ``mutated_by`` edge from each variable to the code the use
        records say may change it (``_mutations``), as pass 2 records one
        for a change through a name it resolves."""
        for var, writers in sorted(self._use_mutations.items()):
            for writer in sorted(writers):
                self.index.edges.add(Edge(var, writer, EdgeKind.REFERENCES, MUTATED_BY))
        for var, writers in sorted(self._use_value_mutations.items()):
            for writer in sorted(writers):
                self._global.value_writes.add((var, writer))

    def _outside_stars(self, records: list[tuple[str, UseRecord]]) -> list[tuple[str, UseRecord]]:
        """The records, and each about a name a star import may bind
        (``@NAME``) again about that name of every star-imported module
        outside the source roots (``from os import *`` then ``path.x = 1``
        stores onto ``os.path``); ``_locate_target`` follows the stars in
        the roots."""
        more: list[tuple[str, UseRecord]] = []
        for m, r in records:
            if not r.target.startswith("@"):
                continue
            rest = r.target[1:]
            for star in self.scopes[m].star_imports:
                if star not in self.scopes:
                    more.append((m, UseRecord(r.kind, f"{star}.{rest}", r.sources, r.writer)))
        return records + more if more else records

    @staticmethod
    def _redirect_installed(records: list[tuple[str, UseRecord]]) -> list[tuple[str, UseRecord]]:
        """The records, and for a handle installed under a name literals
        give (an AS record), each record about that name again about every
        module the name may stand for."""
        installed_as: dict[str, set[str]] = defaultdict(set)
        for _, r in records:
            if r.kind == AS:
                installed_as[r.target].update(n[1:] for n in r.sources if n.startswith(">"))
        if not installed_as:
            return records
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
        return records + redirected

    def _named_prefixes(self, records: list[tuple[str, UseRecord]]) -> set[str]:
        """Every dotted prefix of what code names in another module (``mod.
        _ser.x`` names ``mod._ser``): by a CALL record, an import of the
        name, a use of it as a value. A private function so reached is
        called or held by other code: a bound taken from the calls in its own
        module (a LOADER record) does not hold for it."""
        reached: set[str] = {
            r.target for _, r in records if r.kind in (CALL, USE, DYN, MUT, STORE, SCOPED)
        }
        for scope in self.scopes.values():
            for name, binding in scope.imports.items():
                for b in (binding, *scope.alt_imports.get(name, ())):
                    if b.attr is not None:
                        reached.add(b.dotted)
        return {
            ".".join(parts[:i])
            for target in reached
            for parts in [target.split(".")]
            for i in range(1, len(parts) + 1)
        }

    def _unbind_literals(
        self,
        changed: Callable[[str, str], bool],
        writers_of: Callable[[str, str], set[str]],
    ) -> None:
        """Unbind every literal name some use may change, and every name
        whose values were read out of one (literal_sources), keeping what
        the module's own code left (literal_pristine) and the code that
        unbound each (literal_writers)."""
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
        if self._under_map is None:
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
            # ``alt_imports`` holds the other bindings of names ``imports`` has.
            for name in sorted(scope.imports):
                for binding in [scope.imports[name], *scope.alt_imports.get(name, ())]:
                    origins.append(binding.dotted)
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
                self.index.edges.add(Edge(module, writer, EdgeKind.REFERENCES, INSTALLED))
        for module, writer in sorted(self._installed_anywhere):
            if writer != module:
                self.index.edges.add(Edge(module, writer, EdgeKind.REFERENCES, INSTALLED_ANYWHERE))

    def _module_reach(self) -> None:
        """For each module referenced as a value, what is reachable as an
        attribute chain off it beyond its own members (audit round 3, W16):
        the modules and symbols its imports bind (``SourceIndex.
        module_reach``; the planner adds them to what the referrers of an
        escaped module depend on, as W9 does its members)."""
        for edge in self.index.edges:
            if edge.kind != EdgeKind.REFERENCES or edge.target not in self.scopes:
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
                parts = [*binding.dotted.split("."), *rest[1:]]
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
            # An import or a run of a file ``[index] exclude`` keeps out is an
            # unknown dependency (Resolver.excluded_import).
            self.excluded_paths,
        ]
        return _digest(json.dumps(material))


def build_index(snapshot: Snapshot, module_cache=None) -> SourceIndex:
    return Indexer(snapshot, module_cache=module_cache).build()

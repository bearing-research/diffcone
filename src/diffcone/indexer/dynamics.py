"""After pass 2: dynamic references whose name or receiver comes from a
parameter or an instance attribute, bounded by what every call site or
``__init__`` passes."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from diffcone.indexer.definitions import _ATTRIBUTE_HOOKS
from diffcone.indexer.facts import _AttrWrite, _ParamDynamic
from diffcone.indexer.resolver import Resolver
from diffcone.indexer.scopes import Node, Resolved, Unresolved
from diffcone.indexer.syntax import IMPORT_ATTRIBUTION_DEPTH
from diffcone.model import (
    CLASS,
    REFERENCES,
    UNRESOLVED_ATTRIBUTE,
    UNRESOLVED_DYNAMIC,
    Edge,
    UnresolvedReference,
)


class DynamicBounds(Resolver):
    """Bounds on dynamic references, after pass 2."""

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

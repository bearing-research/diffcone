"""After pass 2: dynamic references whose name or receiver comes from a
parameter or an instance attribute (``getattr(x, p)``, ``import_module(p)``,
a script path a subprocess runs), bounded by what every call site or
``__init__`` passes, when every call site is known (_known_callers)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from diffcone.indexer.definitions import _ATTRIBUTE_HOOKS
from diffcone.indexer.facts import _AttrWrite, _CallSite, _FuncParams, _ParamDynamic
from diffcone.indexer.resolver import Resolver
from diffcone.indexer.scopes import Node, Resolved, Unresolved
from diffcone.indexer.scripts import (
    HOLE_NAME,
    code_value,
    format_attribute_names,
    plugin_options,
    script_value,
)
from diffcone.indexer.syntax import IMPORT_ATTRIBUTION_DEPTH
from diffcone.model import (
    Edge,
    EdgeKind,
    SymbolKind,
    UnresolvedKind,
    UnresolvedReference,
)


class DynamicBounds(Resolver):
    """Bounds on dynamic references, after pass 2."""

    def _resolve_param_dynamics(self) -> None:
        """Expand ``getattr(x, p)``, ``import_module(p)`` and a script run
        by path (diffcone.indexer.scripts) where ``p`` is a parameter or an
        instance attribute, using the literal strings every resolved call
        site passes (for an attribute: what ``__init__`` binds it to, see
        _attribute_writes). An import or a script is attributed to each
        caller (_import_per_caller, _scripts_per_caller). A function whose
        callers may be unknown (_known_callers) or that has an unbounded
        call site stays dynamic.

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
                while source is not None and source.kind != SymbolKind.CLASS:
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
                if values is None or pd.kind not in ("getattr", "format"):
                    continue
                if pd.kind == "format":
                    for text in values:
                        unresolved_names.update(format_attribute_names(text))
                    continue
                for name in dict.fromkeys(values):
                    if pd.base is None:
                        # ``attrgetter("a.b")``: each name on the way.
                        unresolved_names.update(n for n in name.split(".") if n)
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
            if pd.kind == "import" and self._import_per_caller(pd, unresolved_names):
                continue
            if pd.kind in ("script", "code", "template") and self._scripts_per_caller(
                pd, unresolved_names
            ):
                continue
            if pd.kind == "runner":
                if values is not None:
                    plugin_options(self, pd.function, values)
                continue  # a pytester argument from elsewhere is not followed
            if (
                values is None
                and pd.kind == "format"
                and pd.self_class
                and self._not_a_string(pd.self_class, pd.param, writes)
            ):
                continue  # ``self.formatter.format(record)``: no template
            if values is None:
                # The name is unbounded. If the *receiver* is one the call
                # sites name, the read is still bounded: it can only be an
                # attribute of those classes, so depend on their members
                # rather than on everything (see _receiver_classes).
                classes = (
                    self._receiver_classes(pd, writes, unresolved_names)
                    if pd.kind == "getattr"
                    else None
                )
                if classes:
                    for member in sorted(self._class_members(classes)):
                        self.out.edges.add(
                            Edge(
                                pd.function,
                                member,
                                EdgeKind.REFERENCES,
                                "attribute read dynamically",
                            )
                        )
                    continue
                self.out.unresolved.add(
                    UnresolvedReference(pd.function, UnresolvedKind.DYNAMIC, "", pd.detail)
                )
                continue
            for name in dict.fromkeys(values):
                if pd.kind == "script":
                    script_value(self, pd.function, name, pd.detail)
                elif pd.kind == "code":
                    code_value(self, pd.function, name, pd.detail, _namespace(pd, 0))
                elif pd.kind == "template":
                    text = (pd.base or [""])[0].replace(HOLE_NAME, name)
                    code_value(self, pd.function, text, pd.detail, _namespace(pd, 1))
                elif pd.kind == "format":
                    for attr in format_attribute_names(name):
                        self.out.unresolved.add(
                            UnresolvedReference(
                                pd.function, UnresolvedKind.ATTRIBUTE, attr, pd.detail
                            )
                        )
                elif pd.kind == "import":
                    if name.startswith("."):
                        self.out.unresolved.add(
                            UnresolvedReference(pd.function, UnresolvedKind.DYNAMIC, "", pd.detail)
                        )
                    else:
                        self._module_import_edge(pd.function, name)
                elif pd.base is None:
                    for part in name.split("."):
                        if part:
                            self.out.unresolved.add(
                                UnresolvedReference(
                                    pd.function,
                                    UnresolvedKind.ATTRIBUTE,
                                    part,
                                    f"getattr(..., {name!r})",
                                )
                            )
                else:
                    chain = ".".join(pd.base + [name])
                    node, rest = self.resolve_chain_names(pd.base + [name], pd.scope)
                    self._record(pd.function, node, chain=chain)
                    self.class_object_read(pd.function, pd.base + [name], pd.scope)
                    for extra in rest:
                        self.out.unresolved.add(
                            UnresolvedReference(pd.function, UnresolvedKind.ATTRIBUTE, extra, chain)
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
                        Edge(ref.source, node.symbol, EdgeKind.REFERENCES, f"self.{ref.attr}")
                    )
                else:
                    self._record(ref.source, node, chain=ref.chain)

    def _known_callers(
        self, function: str, unresolved_names: set[str]
    ) -> tuple[_FuncParams, list[_CallSite]] | None:
        """The parameters of ``function`` and its call sites, when those are
        all the calls there are; None when it may be called from somewhere
        unseen: it is used as a value (escapes), its name occurs as an
        unresolved reference (a call through a receiver of unknown type), an
        unresolved ``super()`` call may reach it, or it is an ``__init__``
        a class may run unseen (_constructor_escapes). Every rule that bounds
        a parameter by what the call sites pass asks this first."""
        info = self.out.func_params.get(function)
        symbol = self.index.symbols.get(function)
        sites = self.out.call_sites.get(function, [])
        if (
            info is None
            or symbol is None
            or not sites
            or function in self.out.escapes
            or symbol.name in unresolved_names
            or self._super_may_reach(symbol)
            or self._constructor_escapes(symbol, unresolved_names)
        ):
            return None
        return info, sites

    def _import_per_caller(self, pd: _ParamDynamic, unresolved_names: set[str]) -> bool:
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
        attributed = self._attributed_imports(pd.function, pd.param, unresolved_names, set())
        if attributed is None:
            return False
        for caller, names in attributed:
            if names is None:
                self.out.unresolved.add(
                    UnresolvedReference(caller, UnresolvedKind.DYNAMIC, "", pd.detail)
                )
                continue
            for name in dict.fromkeys(names):
                self._module_import_edge(caller, name)
        return True

    def _scripts_per_caller(self, pd: _ParamDynamic, unresolved_names: set[str]) -> bool:
        """``run_python(script)`` starting ``[sys.executable, script]``, or
        ``run_source(code)`` executing it: the script path (or code text)
        each call site passes, attributed to that caller as
        ``_import_per_caller`` attributes module names (diffcone.indexer.
        scripts). False when the callers are not known."""
        attributed = self._attributed_imports(pd.function, pd.param, unresolved_names, set())
        if attributed is None:
            return False
        for caller, values in attributed:
            if values is None:
                self.out.unresolved.add(
                    UnresolvedReference(caller, UnresolvedKind.DYNAMIC, "", pd.detail)
                )
                continue
            for value in dict.fromkeys(values):
                if pd.kind == "code":
                    # ``base`` holds the module whose namespace the text runs in.
                    code_value(self, caller, value, pd.detail, _namespace(pd, 0))
                elif pd.kind == "template":
                    # ``base`` holds the text with the one hole the value fills,
                    # then that module.
                    text = (pd.base or [""])[0].replace(HOLE_NAME, value)
                    code_value(self, caller, text, pd.detail, _namespace(pd, 1))
                else:
                    script_value(self, caller, value, pd.detail)
        return True

    def _not_a_string(
        self, class_id: str, attr: str, writes: dict[tuple[str, str], list[_AttrWrite]]
    ) -> bool:
        """Whether every write of ``self.<attr>`` binds an instance of an
        in-scope class or a factory's result (writes.py): then it is no
        ``str`` template whose fields read attributes by name."""
        bound = self._family_writes(class_id, attr, writes, None)
        return bound is not None and all(
            binding[0] in ("instance", "factory") for _, binding in bound
        )

    def _attributed_imports(
        self,
        function: str,
        param: str,
        unresolved_names: set[str],
        seen: set[tuple[str, str]],
        depth: int = 0,
    ) -> list[tuple[str, tuple[str, ...] | None]] | None:
        """Per call site of ``function``, who imports (or runs as a script)
        what through ``param``.

        A site that passes its own parameter answers one level further out --
        pandas' ``skip_if_no(name)`` hands its parameter to the importer, and
        its own callers name the module -- so the search follows it, with a
        depth cap and a guard against a cycle. None when the callers cannot
        be known at all (_known_callers)."""
        if (function, param) in seen or depth > IMPORT_ATTRIBUTION_DEPTH:
            return None
        seen.add((function, param))
        known = self._known_callers(function, unresolved_names)
        if known is None:
            return None
        info, sites = known
        found: list[tuple[str, tuple[str, ...] | None]] = []
        for site in sites:
            names = site.value_for(param, info)
            if names is not None and not any(name.startswith(".") for name in names):
                found.append((site.caller, tuple(names)))
                continue
            outer = site.param_for(param, info) if names is None else None
            deeper = (
                self._attributed_imports(site.caller, outer, unresolved_names, seen, depth + 1)
                if outer is not None
                else None
            )
            found.extend(deeper if deeper is not None else [(site.caller, None)])
        return found

    def _receiver_classes(
        self,
        pd: _ParamDynamic,
        writes: dict[tuple[str, str], list[_AttrWrite]],
        unresolved_names: set[str],
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
            return self._passed_classes(pd.function, pd.base[0], unresolved_names)
        if (
            len(pd.base) == 2
            and pd.scope.self_class
            and pd.base[0] == pd.scope.self_name
            and not pd.scope.self_is_class
        ):
            return self._attribute_classes(
                pd.scope.self_class, pd.base[1], writes, unresolved_names
            )
        return None

    def _passed_classes(
        self, function: str, param: str, unresolved_names: set[str]
    ) -> set[str] | None:
        """The classes every resolved call site passes for ``param``; None
        when the function may be called from somewhere unseen
        (_known_callers) or a site says nothing about what it passes."""
        known = self._known_callers(function, unresolved_names)
        if known is None:
            return None
        info, sites = known
        found: set[str] = set()
        for site in sites:
            cls = site.class_for(param, info)
            if cls is None:
                return None
            found.add(cls)
        return found or None

    def _attribute_classes(
        self,
        class_id: str,
        attr: str,
        writes: dict[tuple[str, str], list[_AttrWrite]],
        unresolved_names: set[str],
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
        bound = self._family_writes(class_id, attr, writes, None)
        if bound is None:
            return None
        found: set[str] = set()
        for w, (kind, value) in bound:
            if kind == "symbol":
                symbol = self.index.symbols.get(value)
                if symbol is None or symbol.kind != SymbolKind.CLASS:
                    return None
                found.add(value)
            elif kind == "param":
                passed = self._passed_classes(w.method, value, unresolved_names)
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
        ``param``, or None when some caller may be unseen (_known_callers)
        or unbounded."""
        known = self._known_callers(function, unresolved_names)
        if known is None:
            return None
        info, sites = known
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
        customise attribute access, and every write must bind something
        known (_AttrWrite: a parameter, strings or a symbol in ``__init__``;
        an instance, a third-party value or a factory's result in any
        method, diffcone.indexer.writes). A class that escapes, or whose name occurs
        as an unresolved reference, may have subclasses the index cannot
        see (``class S(Base)`` with ``Base = Foo if X else Bar``), whose
        writes are unknown."""
        return self._family_writes(class_id, attr, writes, unresolved_names)

    def _family_writes(
        self,
        class_id: str,
        attr: str,
        writes: dict[tuple[str, str], list[_AttrWrite]],
        unresolved_names: set[str] | None,
    ) -> list[tuple[_AttrWrite, list[Any]]] | None:
        """The writes of ``self.<attr>`` with their bindings over every class
        an instance of ``class_id`` may be (the MRO of it and of each in-scope
        subclass), or None when one cannot be bounded: a write through a
        receiver of unknown type, a class outside the analysis or with an
        unknown base, a class-level binding of the name, a class customising
        attribute access, a write that binds nothing known. With
        ``unresolved_names`` (_attribute_writes), every class must also be
        plain and seen whole: neither escaping nor named by an unresolved
        reference."""
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
            if unresolved_names is not None and (
                not cscope.plain
                or cid in self.out.escapes
                or self.index.symbols[cid].name in unresolved_names
            ):
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


def _namespace(pd: _ParamDynamic, at: int) -> str | None:
    """The module whose namespace a ``code``/``template`` text runs in,
    stored in ``base`` at ``at`` (absent when the text has a namespace of
    its own)."""
    base = pd.base or []
    return base[at] if len(base) > at else None

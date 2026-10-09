"""What literal strings a name or expression can hold (``getattr(x, NAME)``
with ``NAME`` from a literal table), and which names a scope binds or
mutates in place."""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence

from diffcone.indexer.syntax import DEF_NODES, FUNC_NODES
from diffcone.indexer.uses import scan_names

# Synthetic key in a literal table: what *indexing* the literal bound to NAME
# yields -- a dict display's values, a sequence display's elements -- kept
# beside what iterating it yields (stored under NAME itself). No identifier
# contains a bracket, so the two can never collide.
INDEXED = "[]"
# What indexing an *element* of the literal yields: a table of tuples
# (``{"Config": ("pkg.config", "Config")}``) indexed twice, as a lazy-export
# ``__getattr__`` does with ``target = TABLE.get(name)`` then
# ``import_module(target[0])``.
NESTED = INDEXED + INDEXED


def literal_keys(name: str) -> tuple[str, str, str]:
    """Every table key a binding of ``name`` writes."""
    return name, name + INDEXED, name + NESTED


def literal_base(key: str) -> str:
    """The name a table key belongs to."""
    return key.split("[", 1)[0]


def _constant_string(expr: ast.expr) -> str | None:
    """The string ``expr`` is, when it is written out in the source."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.JoinedStr):
        parts = [v.value for v in expr.values if isinstance(v, ast.Constant)]
        if len(parts) == len(expr.values):
            return "".join(str(part) for part in parts)
    return None


def _literal_strings(expr: ast.expr) -> tuple[str, ...] | None:
    constant = _constant_string(expr)
    if constant is not None:
        return (constant,)
    if isinstance(expr, ast.Dict):
        # Iterating a dict yields its keys; only all-literal string keys count.
        keys: list[str] = []
        for key in expr.keys:
            if key is None or (name := _constant_string(key)) is None:
                return None
            keys.append(name)
        return tuple(keys)
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        out: list[str] = []
        for elt in expr.elts:
            if isinstance(elt, (ast.List, ast.Set, ast.Dict)):
                # Iterating yields a container, which can change in place.
                return None
            values = _literal_strings(elt)
            if values is None:
                return None
            out.extend(values)
        return tuple(out)
    return None


def _string_candidates(
    expr: ast.expr,
    local_literals: dict[str, tuple[str, ...] | None],
    module_literals: dict[str, tuple[str, ...] | None],
) -> tuple[str, ...] | None:
    direct = _literal_strings(expr)
    if direct is not None:
        return direct
    if isinstance(expr, ast.Name):
        if expr.id in local_literals:
            return local_literals[expr.id]
        return module_literals.get(expr.id)
    # ``D.keys()`` over a literal-keyed dict yields its keys. (``D.items()``
    # yields pairs and is handled only for ``for key, value in`` targets.)
    if (receiver := _dict_method_receiver(expr, "keys")) is not None:
        return _string_candidates(receiver, local_literals, module_literals)
    # ``D.values()`` over a dict literal with string values yields them.
    if (receiver := _dict_method_receiver(expr, "values")) is not None:
        return _indexed(receiver, local_literals, module_literals)
    # ``D[key]`` / ``L[i]`` / ``D.get(key)``: one of the literal's values (a
    # slice is not one).
    if (receiver := _item_receiver(expr)) is not None:
        return _indexed(receiver, local_literals, module_literals)
    return None


def _item_receiver(expr: ast.expr) -> ast.expr | None:
    """``D`` when ``expr`` takes one item of ``D``: ``D[key]`` (not a slice),
    or ``D.get(key)`` / ``D.get(key, None)``, whose ``None`` is no string."""
    if isinstance(expr, ast.Subscript) and not isinstance(expr.slice, ast.Slice):
        return expr.value
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "get"
        and not expr.keywords
        and 1 <= len(expr.args) <= 2
        and (
            len(expr.args) == 1
            or (isinstance(expr.args[1], ast.Constant) and expr.args[1].value is None)
        )
    ):
        return expr.func.value
    return None


def _constant_strings(exprs: Sequence[ast.expr | None]) -> tuple[str, ...] | None:
    """The strings ``exprs`` are, or None when any of them is not written out
    as one (a ``*``/``**`` unpacking, whose element is None, included)."""
    out: list[str] = []
    for expr in exprs:
        if expr is None or (string := _constant_string(expr)) is None:
            return None
        out.append(string)
    return tuple(out)


def _indexed(
    expr: ast.expr,
    local_literals: dict[str, tuple[str, ...] | None],
    module_literals: dict[str, tuple[str, ...] | None],
) -> tuple[str, ...] | None:
    """Every string indexing ``expr`` may yield, or None when unbounded: a
    dict display's values, a sequence display's elements. A name is looked up
    under its synthetic ``INDEXED`` key, which every binding of that name
    writes, so a local shadowing a module-level table is unbounded rather
    than that table's contents."""
    if isinstance(expr, ast.Dict):
        # ``{**other}`` has a None key and hides what it contributes.
        if any(key is None for key in expr.keys):
            return None
        return _constant_strings(expr.values)
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        return _constant_strings(expr.elts)
    if isinstance(expr, ast.Name):
        key = expr.id + INDEXED
        if key in local_literals:
            return local_literals[key]
        return module_literals.get(key)
    # ``TABLE[key][i]``: an element of one of the table's values.
    if (receiver := _item_receiver(expr)) is not None:
        return _nested(receiver, local_literals, module_literals)
    return None


def _nested(
    expr: ast.expr,
    local_literals: dict[str, tuple[str, ...] | None],
    module_literals: dict[str, tuple[str, ...] | None],
) -> tuple[str, ...] | None:
    """Every string indexing an element of ``expr`` may yield, or None when
    unbounded: a dict display whose values (or a sequence display whose
    elements) are all tuple displays of string literals, flattened. Which
    position a string holds is not kept, so ``target[0]`` may be any of them:
    a superset."""
    if isinstance(expr, ast.Dict):
        if any(key is None for key in expr.keys):
            return None
        items: list[ast.expr] = list(expr.values)
    elif isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        items = list(expr.elts)
    elif isinstance(expr, ast.Name):
        key = expr.id + NESTED
        if key in local_literals:
            return local_literals[key]
        return module_literals.get(key)
    else:
        return None
    out: list[str] = []
    for item in items:
        # Tuples only: a list or set entry can be changed in place through
        # an alias no analysis follows (``_TABLE.get(k)[:] = [...]``).
        if not isinstance(item, ast.Tuple):
            return None
        strings = _constant_strings(item.elts)
        if strings is None:
            return None
        out.extend(strings)
    return tuple(dict.fromkeys(out))


def _dict_method_receiver(expr: ast.expr, method: str) -> ast.expr | None:
    """``D`` when ``expr`` is ``D.<method>()`` with no arguments."""
    if (
        isinstance(expr, ast.Call)
        and not expr.args
        and not expr.keywords
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == method
    ):
        return expr.func.value
    return None


def _live(value: ast.expr) -> bool:
    """Whether binding ``value`` binds a container, or a live view of one,
    rather than a string or a tuple of them taken out of it."""
    if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Name)):
        return True
    return any(_dict_method_receiver(value, m) is not None for m in ("keys", "values", "items"))


def _collect_literal_bindings(
    node: ast.AST,
    module_literals: dict[str, tuple[str, ...] | None],
    *,
    sources: dict[str, set[str]] | None = None,
) -> dict[str, tuple[str, ...] | None]:
    """Names bound in ``node``'s scope to string literals, tuples of them, or
    loop variables over such tuples. A name with any other binding maps to
    None (unbounded); nested scopes are not entered. A name bound to a dict
    or sequence display also gets what indexing it yields under
    ``name + INDEXED``.

    A container stays bounded only while every use of it, here or in a
    nested scope, is a read (diffcone.indexer.uses): one that is mutated,
    handed on or aliased anywhere in the scope is unbounded throughout it,
    and so is everything when the scope reaches its namespace
    (``globals()``, ``exec`` of code built at run time). With ``sources``,
    each name also collects the names its values were taken from, so a
    container another module turns out to change can unbound what was read
    out of it (Indexer._apply_uses)."""
    uses = scan_names(node)
    leaks = uses.names
    found: dict[str, tuple[str, ...] | None] = {}
    origins: dict[str, set[str]] = sources if sources is not None else {}

    def taken_from(name: str, expr: ast.expr) -> None:
        if sources is None:
            return
        deps = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name) and n.id in found} - {name}
        for dep in list(deps):
            deps |= origins.get(dep, set())
        if deps:
            origins.setdefault(name, set()).update(deps)

    def merge(name: str, values: tuple[str, ...] | None) -> None:
        known = found.get(name)
        if known is not None and values is not None:
            found[name] = tuple(dict.fromkeys(known + values))
        else:
            found[name] = None if (name in found and found[name] is None) else values

    def bind(
        name: str,
        values: tuple[str, ...] | None,
        indexed: tuple[str, ...] | None = None,
        nested: tuple[str, ...] | None = None,
    ) -> None:
        merge(name, values)
        merge(name + INDEXED, indexed)
        merge(name + NESTED, nested)

    def unbind(name: str) -> None:
        for key in literal_keys(name):
            found[key] = None

    # Name stores the forms below bind; every other store of a name
    # (``+=``, walrus, ``with ... as``, unpacking, ``except ... as``, an
    # import, a match capture, ``global``/``nonlocal``) leaves it unbounded.
    handled: set[int] = set()

    # Source order matters: ``names = {...}`` must be seen before the loop
    # that iterates it, so children are pushed reversed onto the LIFO stack.
    stack: list[ast.AST] = list(reversed(list(ast.iter_child_nodes(node))))
    while stack:
        n = stack.pop()
        if isinstance(n, NESTED_SCOPES):
            # Not entered, but it may rebind names of this scope: ``nonlocal``
            # or ``global`` declarations, a walrus inside a comprehension.
            for inner in ast.walk(n):
                if isinstance(inner, (ast.Global, ast.Nonlocal)):
                    for name in inner.names:
                        unbind(name)
                elif isinstance(inner, ast.NamedExpr) and isinstance(inner.target, ast.Name):
                    unbind(inner.target.id)
            continue
        if isinstance(n, ast.Assign):
            values = _string_candidates(n.value, found, module_literals)
            items = _indexed(n.value, found, module_literals)
            nested = _nested(n.value, found, module_literals)
            for target in n.targets:
                if isinstance(target, ast.Name):
                    handled.add(id(target))
                    taken_from(target.id, n.value)
                    if target.id in leaks and _live(n.value):
                        unbind(target.id)
                    else:
                        bind(target.id, values, items, nested)
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            if isinstance(n.target, ast.Name):
                handled.add(id(n.target))
                taken_from(n.target.id, n.value)
                if n.target.id in leaks and _live(n.value):
                    unbind(n.target.id)
                else:
                    bind(
                        n.target.id,
                        _string_candidates(n.value, found, module_literals),
                        _indexed(n.value, found, module_literals),
                        _nested(n.value, found, module_literals),
                    )
        elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Name):
            handled.add(id(n.target))
            taken_from(n.target.id, n.iter)
            bind(n.target.id, _string_candidates(n.iter, found, module_literals))
        elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Tuple):
            # ``for key, value in D.items()``: over a dict display both the
            # keys and the values are bounded, anything else is not.
            elts = n.target.elts
            keys = items = None
            receiver = _dict_method_receiver(n.iter, "items")
            if receiver is not None and len(elts) == 2:
                keys = _string_candidates(receiver, found, module_literals)
                items = _indexed(receiver, found, module_literals)
            for i, elt in enumerate(elts):
                if isinstance(elt, ast.Name):
                    handled.add(id(elt))
                    taken_from(elt.id, n.iter)
                    bind(elt.id, keys if i == 0 else items)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            if id(n) not in handled:
                unbind(n.id)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Del):
            # ``del name`` binds no new value (a later read fails): it only
            # leaves a name that had no binding yet unbounded.
            if n.id not in found:
                unbind(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            unbind(n.name)
        elif isinstance(n, ast.alias):
            unbind(n.asname or n.name.split(".")[0])
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            unbind(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            unbind(n.rest)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            for name in n.names:
                unbind(name)
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    if uses.namespace:
        for key in found:
            found[key] = None
    return found


COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


NESTED_SCOPES = DEF_NODES + COMPREHENSIONS + (ast.Lambda,)


def _module_containers(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound (outside any nested scope) to a dict, list
    or set display, or to something that may be one (a name, a view)."""
    names: set[str] = set()
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, NESTED_SCOPES):
            continue
        if isinstance(node, ast.Assign) and _live(node.value):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and _live(node.value)
        ):
            names.add(node.target.id)
        stack.extend(ast.iter_child_nodes(node))
    return frozenset(names)


def _collect_store_names(stmt: ast.AST) -> set[str]:
    """Names bound by a statement in *its own* scope (nested scopes excluded)."""
    names: set[str] = set()
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        if isinstance(node, NESTED_SCOPES) and node is not stmt:
            if isinstance(node, DEF_NODES):
                names.add(node.name)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return names


class _LocalBindings(ast.NodeVisitor):
    """Collect the names a function, lambda or class body binds in its own
    scope. Nested functions, lambdas and comprehensions get their own scope;
    only their names (for defs) are bound here. ``global`` and ``nonlocal``
    names are excluded.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.globals: set[str] = set()

    def collect(self, node: ast.AST) -> set[str]:
        if isinstance(node, FUNC_NODES + (ast.Lambda,)):
            self.visit(node.args)
        body = getattr(node, "body", [])
        for stmt in body if isinstance(body, list) else [body]:
            self.visit(stmt)
        return self.names - self.globals

    def visit_arg(self, node: ast.arg) -> None:
        self.names.add(node.arg)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _visit_comprehension(self, node: ast.AST) -> None:
        return

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.names.add(node.name)

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.globals.update(node.names)  # the enclosing function's, not local

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)


def make_evaluator(
    module_literals: dict[str, tuple[str, ...] | None],
) -> Callable[[ast.expr, ast.AST, frozenset[str]], tuple[tuple[str, ...] | None, tuple[str, ...]]]:
    """What pass 1 knows of a string expression (see uses.Evaluate): its
    candidates from its scope's literal bindings and the module's, and the
    module-level literal names the scope reads, any of which may still turn
    out to be changed by another module."""
    tables: dict[int, tuple[dict[str, tuple[str, ...] | None], tuple[str, ...]]] = {}

    def evaluate(
        expr: ast.expr, scope: ast.AST, shadowed: frozenset[str]
    ) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        if id(scope) not in tables:
            local: dict[str, tuple[str, ...] | None] = {}
            if not isinstance(scope, ast.Module):
                for name in _LocalBindings().collect(scope):
                    for key in literal_keys(name):
                        local[key] = None
                local.update(_collect_literal_bindings(scope, module_literals))
            # A function's locals may be taken from any module-level literal
            # it reads; at module level only the expression's own names count.
            tables[id(scope)] = (local, () if isinstance(scope, ast.Module) else _reads(scope))
        local, read = tables[id(scope)]
        if isinstance(scope, ast.Module):
            read = _reads(expr)
        if shadowed:
            local = dict(local)
            for name in shadowed:
                for key in literal_keys(name):
                    local[key] = None
        return _string_candidates(expr, local, module_literals), read

    def _reads(node: ast.AST) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    n.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name) and module_literals.get(n.id) is not None
                }
            )
        )

    return evaluate

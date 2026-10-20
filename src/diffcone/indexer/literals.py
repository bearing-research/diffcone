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


def parameter_names(node: ast.AST | None) -> set[str]:
    """The parameters of a function or lambda (none for anything else)."""
    if not isinstance(node, FUNC_NODES + (ast.Lambda,)):
        return set()
    args = node.args
    every = args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]
    return {a.arg for a in every if a is not None}


def own_literal_bindings(
    node: ast.AST | None,
    bound: set[str] | frozenset[str],
    module_literals: dict[str, tuple[str, ...] | None],
) -> dict[str, tuple[str, ...] | None]:
    """The literal table of the scope ``node`` opens (a function or lambda;
    None for a scope without a body of its own to read, such as a class body
    or a comprehension), whose own bindings are ``bound``.

    Every name the scope binds shadows the module's literal of that name,
    so it is unbounded unless an assignment in the scope says what it holds.
    A parameter stays unbounded whatever the body assigns it: the caller
    passes anything, and a literal assigned on one branch says nothing about
    the other."""
    table: dict[str, tuple[str, ...] | None] = {}
    names = set(bound)
    if isinstance(node, FUNC_NODES + (ast.Lambda,)):
        names |= _LocalBindings().collect(node)
    for name in names:
        for key in literal_keys(name):
            table[key] = None
    if node is not None:
        params = parameter_names(node)
        for key, values in _collect_literal_bindings(node, module_literals).items():
            if literal_base(key) not in params:
                table[key] = values
    return table


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
                local = own_literal_bindings(
                    scope, _LocalBindings().collect(scope), module_literals
                )
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


# --------------------------------------------------------------------------- immutable values
# A module variable whose one binding is a value nothing can change in place
# (audit round 6, T20): a write "into" it, or into what its methods return,
# changes nothing its readers read. ``module_immutables`` is pass 1's record;
# whether such a variable is immutable is decided after pass 2, once every
# module's rebindings are known (Indexer._immutable_variables).

# Standard-library calls whose result nothing can change in place, whatever
# they are handed: a compiled pattern, a string, a number, a path.
IMMUTABLE_RESULTS = frozenset(
    {
        "re.compile",
        "textwrap.dedent",
        "inspect.cleandoc",
        "datetime.timedelta",
        "decimal.Decimal",
        "fractions.Fraction",
        "pathlib.Path",
        "pathlib.PurePath",
        "pathlib.PurePosixPath",
        "pathlib.PureWindowsPath",
        "pathlib.PosixPath",
        "pathlib.WindowsPath",
        "uuid.UUID",
        "os.path.join",
        "os.path.dirname",
        "os.path.basename",
        "os.path.abspath",
        "os.path.realpath",
        "os.path.normpath",
        "os.path.expanduser",
    }
)
# Calls whose result is immutable when what they are handed is (a
# ``datetime`` keeps the ``tzinfo`` object it is given, a ``tuple`` its
# items).
IMMUTABLE_OF_IMMUTABLE = frozenset(
    {"datetime.date", "datetime.datetime", "datetime.time", "datetime.timezone"}
)
IMMUTABLE_BUILTINS = frozenset(
    {"int", "float", "complex", "str", "bytes", "bool", "tuple", "frozenset", "range"}
)
# Methods of a string, bytes, number, tuple or path that give such a value
# again (``", ".join(...)``, ``"{}".format(...)``, ``p.with_suffix(...)``),
# and attributes of one that are one (``p.parent``, ``pattern.pattern``).
IMMUTABLE_METHODS = frozenset(
    {
        "capitalize",
        "casefold",
        "center",
        "count",
        "decode",
        "encode",
        "endswith",
        "expandtabs",
        "find",
        "format",
        "index",
        "join",
        "ljust",
        "lower",
        "lstrip",
        "removeprefix",
        "removesuffix",
        "replace",
        "rfind",
        "rindex",
        "rjust",
        "rstrip",
        "startswith",
        "strip",
        "swapcase",
        "title",
        "upper",
        "zfill",
        "absolute",
        "expanduser",
        "joinpath",
        "relative_to",
        "resolve",
        "with_name",
        "with_stem",
        "with_suffix",
    }
)
IMMUTABLE_ATTRIBUTES = frozenset({"parent", "name", "stem", "suffix", "pattern"})
# Builtins that bind names of a namespace by a string (``globals()["X"] =
# v``, ``exec(code)``, ``vars()``): a module naming one may bind any of its
# names anew.
_NAMESPACE_WRITERS = frozenset({"globals", "vars", "locals", "exec", "eval"})
# What an immutable value rests on besides the module's own names: an
# imported standard-library module (``module:re``), which no module of the
# project may shadow.
EXTERNAL_DEPENDENCY = "module:"


def module_immutables(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """The module-level names bound exactly once in the module (nothing else
    binds, deletes or declares them ``global`` anywhere in it) by an
    assignment of a value that cannot change in place: a string, bytes,
    number, ``None`` or ``bool`` literal, an f-string, an operator,
    comparison, condition or subscript of such values, a tuple of them, a
    call of ``IMMUTABLE_RESULTS``, of ``IMMUTABLE_OF_IMMUTABLE`` or of an
    immutable builtin on them, one of the methods or attributes above, or
    another such name. Name -> what that rests on: the module's names it
    reads (immutable themselves only if they are, once every module is
    known) and ``module:<m>`` per standard-library module it calls into.
    Nothing in a module that names its namespace (``globals``, ``vars``,
    ``exec``, ``__dict__``, ``sys.modules``) or star-imports: any of its
    names may be bound anew there."""
    counts: dict[str, int] = {}
    imports: dict[str, str] = {}
    values: dict[str, ast.expr] = {}
    declared: set[str] = set()
    modules_reads: set[str] = set()  # names ``x`` of ``x.modules``
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _NAMESPACE_WRITERS:
            return {}
        if isinstance(node, ast.Attribute):
            if node.attr in ("__dict__", "__globals__", "f_globals"):
                return {}
            if node.attr == "modules" and isinstance(node.value, ast.Name):
                modules_reads.add(node.value.id)
        elif isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                return {}
            if node.module == "sys" and any(a.name == "modules" for a in node.names):
                return {}
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            # A walrus in a comprehension binds the enclosing scope's name.
            declared.add(node.target.id)

    def bind(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (*DEF_NODES, ast.ClassDef)):
            bind(node.name)
            # Its decorators, defaults, bases and annotations run here.
            body = {id(stmt) for stmt in node.body}
            stack.extend(c for c in ast.iter_child_nodes(node) if id(c) not in body)
            continue
        if isinstance(node, (ast.Lambda, *COMPREHENSIONS)):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                bind(name)
                imports[name] = alias.name if alias.asname else name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                bind(name)
                if node.level == 0 and node.module:
                    imports[name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bind(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bind(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                values[node.target.id] = node.value
        stack.extend(ast.iter_child_nodes(node))
    if any(imports.get(name, name) == "sys" for name in modules_reads):
        return {}  # ``sys.modules[__name__].X = v``
    once = {name for name, n in counts.items() if n == 1 and name not in declared}
    found: dict[str, tuple[str, ...]] = {}
    for name, value in values.items():
        if name not in once:
            continue
        rests: set[str] = set()
        if _ImmutableValue(imports, once, counts, rests).check(value):
            found[name] = tuple(sorted(rests))
    return dict(sorted(found.items()))


class _ImmutableValue:
    """Whether a module-level expression is a value nothing can change in
    place, given that the module's names and the modules it adds to
    ``rests`` are what they are bound to (``module_immutables``)."""

    def __init__(
        self, imports: dict[str, str], once: set[str], counts: dict[str, int], rests: set[str]
    ) -> None:
        self.imports, self.once, self.counts, self.rests = imports, once, counts, rests

    def check(self, expr: ast.expr | None) -> bool:
        if expr is None:
            return False
        if isinstance(expr, (ast.Constant, ast.JoinedStr)):
            return True  # an f-string is a ``str`` whatever it interpolates
        if isinstance(expr, ast.Name):
            if expr.id in ("__name__", "__file__", "__package__"):
                return expr.id not in self.counts
            if expr.id in self.once and expr.id not in self.imports:
                self.rests.add(expr.id)
                return True
            return False
        if isinstance(expr, ast.Tuple):
            return all(self.check(e) for e in expr.elts)
        if isinstance(expr, ast.Starred):
            return self.check(expr.value)  # the items of an immutable value
        if isinstance(expr, ast.BinOp):
            return self.check(expr.left) and self.check(expr.right)
        if isinstance(expr, ast.UnaryOp):
            return isinstance(expr.op, ast.Not) or self.check(expr.operand)
        if isinstance(expr, ast.BoolOp):
            return all(self.check(v) for v in expr.values)
        if isinstance(expr, ast.Compare):
            if all(isinstance(op, (ast.In, ast.NotIn, ast.Is, ast.IsNot)) for op in expr.ops):
                return True  # always a ``bool``
            return self.check(expr.left) and all(self.check(c) for c in expr.comparators)
        if isinstance(expr, ast.IfExp):
            return self.check(expr.body) and self.check(expr.orelse)
        if isinstance(expr, ast.Subscript):
            return self.check(expr.value)
        if isinstance(expr, ast.Attribute):
            return expr.attr in IMMUTABLE_ATTRIBUTES and self.check(expr.value)
        if isinstance(expr, ast.Call):
            return self._call(expr)
        return False

    def _call(self, expr: ast.Call) -> bool:
        if any(k.arg is None for k in expr.keywords):
            return False  # ``**kwargs``
        args = [*expr.args, *(k.value for k in expr.keywords)]
        func = expr.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in IMMUTABLE_METHODS
            and self.check(func.value)
            and all(self.check(a) for a in args)
        ):
            return True
        named = self._canonical(func)
        if named is None:
            return False
        dotted, module = named
        if dotted in IMMUTABLE_RESULTS:
            self.rests.add(EXTERNAL_DEPENDENCY + module)
            return True
        if dotted in IMMUTABLE_OF_IMMUTABLE or (not module and dotted in IMMUTABLE_BUILTINS):
            if module:
                self.rests.add(EXTERNAL_DEPENDENCY + module)
            # ``frozenset({"a", "b"})``, ``tuple(["a"])``: a display of
            # immutable items is copied.
            return all(
                all(self.check(e) for e in a.elts)
                if isinstance(a, (ast.List, ast.Set))
                else self.check(a)
                for a in args
            )
        return False

    def _canonical(self, func: ast.expr) -> tuple[str, str] | None:
        """The dotted name a called function is (``re.compile`` after
        ``import re``, ``compile`` after ``from re import compile``) and the
        module it is imported from, when only that import binds the name;
        a builtin's name and "" when nothing binds it; None otherwise."""
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.insert(0, func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        head = func.id
        if head in self.imports:
            if head not in self.once:
                return None
            target = self.imports[head]
            return ".".join([target, *parts]), target.split(".")[0]
        if not parts and head not in self.counts and head in IMMUTABLE_BUILTINS:
            return head, ""
        return None

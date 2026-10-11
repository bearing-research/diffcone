"""Facts about definitions the indexer hashes and classifies: line spans,
decorators, class attributes, variable statements, inert definitions."""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable

from diffcone.indexer.literals import COMPREHENSIONS, _collect_store_names
from diffcone.indexer.scopes import ModuleScope, VariableStatement, _absolute_module
from diffcone.indexer.syntax import (
    BUILTIN_NAMES,
    DEF_NODES,
    FUNC_NODES,
    _digest,
    _flatten_chain,
    iter_scope_statements,
)
from diffcone.model import CLASS_STATEMENT, OPAQUE_ATTRIBUTE


def _canonical_imports(scope: ModuleScope) -> set[str]:
    """One string per imported binding, independent of statement grouping or
    order: adding a name to ``from m import (a, b)`` adds one entry."""
    out: set[str] = set()
    for node in scope.import_nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update(_import_entries(scope, node))
    return out


def _import_layout(scope: ModuleScope) -> tuple[str, ...]:
    """Each module-level import binding, in source order, prefixed with the
    blocks it sits in (``if <test>``, ``try``, ``except``, ``else``, ...):
    what runs at import depends on where an import is, not only on which
    names are bound."""
    assert scope.tree is not None
    out: list[str] = []

    def walk(body: list[ast.stmt], context: str) -> None:
        # Ordinary statements before an import in its block: an import moved
        # across ``os.environ[...] = ...`` or ``sys.path.insert`` changes what
        # it runs with. Counted, not hashed: editing them is a body change.
        before = 0
        for stmt in body:
            if isinstance(stmt, DEF_NODES):
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for entry in _import_entries(scope, stmt):
                    out.append(f"{context}#{before}|{entry}")
                continue
            before += 1
            label = type(stmt).__name__
            if isinstance(stmt, (ast.If, ast.While)):
                label += f"({ast.unparse(stmt.test)})"
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                label += f"({', '.join(ast.unparse(i) for i in stmt.items)})"
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                label += f"({ast.unparse(stmt.target)} in {ast.unparse(stmt.iter)})"
            elif isinstance(stmt, ast.Match):
                label += f"({ast.unparse(stmt.subject)})"
            for attr in ("body", "orelse", "finalbody"):
                child = getattr(stmt, attr, None)
                if isinstance(child, list) and child:
                    walk(child, f"{context}/{label}.{attr}")
            for i, handler in enumerate(getattr(stmt, "handlers", []) or []):
                walk(handler.body, f"{context}/{label}.except{i}")
            for i, case in enumerate(getattr(stmt, "cases", []) or []):
                walk(case.body, f"{context}/{label}.case{i}")

    walk(scope.tree.body, "")
    return tuple(out)


def _import_entries(scope: ModuleScope, node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [f"import {a.name}" + (f" as {a.asname}" if a.asname else "") for a in node.names]
    base = _absolute_module(scope, node.module, node.level)
    return [
        f"from {base} import {a.name}" + (f" as {a.asname}" if a.asname else "") for a in node.names
    ]


# statement_layout entry prefix of a ``def`` whose statement only binds a
# name: swapping two adjacent ones changes nothing that runs.
INERT_DEF = "def~"


def statement_layout(
    body: list[ast.stmt], scope: ModuleScope, shadowed: Collection[str], *, plain: bool
) -> tuple[str, ...]:
    """A module or class body as the sequence of its statements, each an
    entry of its kind and the names it binds (``assign:A``, ``def:f``,
    ``import pkg.plug``, ``expr``, ``if``), blocks descended into in order
    after the entry of the compound statement. Docstrings, bare strings and ``pass`` bind
    and run nothing and are left out, as is the body of ``if
    TYPE_CHECKING:``. A ``def`` that only binds a name (inert decorators,
    literal defaults, no annotation evaluated at import; ``plain``: in a
    class, only when the class has no decorator or keyword) is an
    ``INERT_DEF`` entry: the classifier lets adjacent ones trade places.
    The hashes keep no order between definitions and other statements
    (``X = 1`` moved below ``@deco(X) def f``, ``def f`` moved below a
    ``try: from pkg.fast import f`` fallback, two ``@register`` defs
    swapped), so the classifier compares these (audit round 5, R6)."""
    out: list[str] = []
    add = out.append

    def walk(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, FUNC_NODES):
                inert = (
                    plain
                    and _inert_def(stmt, scope, shadowed)
                    and (_future_annotations(scope) or not _has_annotations(stmt))
                )
                add(f"{INERT_DEF if inert else 'def'}:{stmt.name}")
            elif isinstance(stmt, ast.ClassDef):
                add(f"class:{stmt.name}")
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for entry in _import_entries(scope, stmt):
                    add(entry)
            elif isinstance(stmt, (ast.Pass, ast.Global, ast.Nonlocal)):
                continue
            elif (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue
            elif isinstance(stmt, ast.If) and "TYPE_CHECKING" in ast.unparse(stmt.test):
                add("if")
                walk(stmt.orelse)
            else:
                if isinstance(stmt, (ast.For, ast.AsyncFor)):
                    bound: set[str] = _collect_store_names(stmt.target)
                elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                    bound = set()
                    for item in stmt.items:
                        if item.optional_vars is not None:
                            bound |= _collect_store_names(item.optional_vars)
                elif isinstance(stmt, (ast.If, ast.While, ast.Try, ast.Match)) or type(
                    stmt
                ).__name__ in ("TryStar",):
                    bound = set()
                else:
                    bound = _collect_store_names(stmt)
                kind = type(stmt).__name__.lower()
                if kind in ("assign", "annassign", "augassign", "typealias"):
                    kind = "assign"
                elif kind in ("asyncfor", "asyncwith", "trystar"):
                    kind = kind.removeprefix("async").removesuffix("star")
                add(f"{kind}:{','.join(sorted(bound))}" if bound else kind)
                for attr in ("body", "orelse", "finalbody"):
                    child = getattr(stmt, attr, None)
                    if isinstance(child, list) and child:
                        walk(child)
                for handler in getattr(stmt, "handlers", []) or []:
                    add(f"except:{handler.name}" if handler.name else "except")
                    walk(handler.body)
                for case in getattr(stmt, "cases", []) or []:
                    names = sorted(_collect_store_names(case.pattern))
                    add(f"case:{','.join(names)}" if names else "case")
                    walk(case.body)

    walk(body)
    return tuple(out)


def _class_attributes(node: ast.ClassDef) -> dict[str, str]:
    """Each name a class body binds by plain assignment, with a hash of the
    statements binding it; every other non-definition statement (a loop, a
    call, a ``del``, a conditional binding) is hashed under OPAQUE_ATTRIBUTE,
    and the class statement itself (bases, keywords, decorators) under
    CLASS_STATEMENT.
    The docstring is not an attribute here: it has its own hash."""
    parts: dict[str, list[str]] = defaultdict(list)
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    for stmt in body:
        if isinstance(stmt, DEF_NODES):
            continue
        dumped = ast.dump(stmt)
        names: list[str] = []
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                elements = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                plain = [e.id for e in elements if isinstance(e, ast.Name)]
                if len(plain) != len(elements):
                    names = []
                    break
                names += plain
        for name in names or [OPAQUE_ATTRIBUTE]:
            parts[name].append(dumped)
    parts[CLASS_STATEMENT] = [
        ast.dump(n) for n in [*node.bases, *node.keywords, *node.decorator_list]
    ]
    return {name: _digest("\n".join(dumps)) for name, dumps in sorted(parts.items())}


def _start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    """First line of a definition including its decorators, which belong to
    the definition (they are part of its definition hash)."""
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _variable_statements(scope: ModuleScope, body: list[ast.stmt]) -> dict[str, VariableStatement]:
    """Top-level ``NAME = <expr>`` / ``NAME: T = <expr>`` statements whose name
    is bound exactly once in the module: candidates for variable symbols.
    Names bound any other way too (in a loop, by unpacking, inside a block)
    stay on the module symbol."""
    assert scope.tree is not None
    simple: dict[str, list[VariableStatement]] = defaultdict(list)
    for stmt in body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target = stmt.target
        else:
            continue
        if isinstance(target, ast.Name):
            simple[target.id].append(stmt)
    simple_stmts = {id(s) for stmts in simple.values() for s in stmts}
    other_bound: set[str] = set()
    for stmt in iter_scope_statements(scope.tree.body):
        if isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
            continue
        if id(stmt) in simple_stmts:
            continue
        other_bound |= _collect_store_names(stmt)
    return {
        name: stmts[0]
        for name, stmts in simple.items()
        if len(stmts) == 1 and name not in other_bound and name not in scope.imports
    }


def _end_line(node: ast.stmt | ast.Module) -> int:
    """Last source line of a definition; a Module has no position of its own."""
    if isinstance(node, ast.Module):
        return node.body[-1].end_lineno or 1 if node.body else 1
    return node.end_lineno or node.lineno


def _has_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == name:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == name:
            return True
    return False


# Bases outside the source roots that only add structure and never call
# a subclass's methods.
_STRUCTURAL_BASES = frozenset(
    {
        "abc.ABC",
        "typing.Generic",
        "typing.Protocol",
        "typing.NamedTuple",
        "typing.TypedDict",
        "typing_extensions.Generic",
        "typing_extensions.Protocol",
        "typing_extensions.NamedTuple",
        "typing_extensions.TypedDict",
    }
)


# Reached explicitly: construction and class creation record their own edges.
_EXPLICIT_SPECIAL_METHODS = frozenset({"__init__", "__new__", "__init_subclass__"})


def _is_special_method(name: str) -> bool:
    return (
        len(name) > 4
        and name.startswith("__")
        and name.endswith("__")
        and name not in _EXPLICIT_SPECIAL_METHODS
    )


def _annotated_args(args: ast.arguments) -> list[ast.arg]:
    """The parameters that carry an annotation, in signature order."""
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [a for a in params if a is not None and a.annotation is not None]


# Decorators that run no code with the function beyond returning it (or
# recording it in typing's overload registry), by canonical name.
_INERT_DECORATORS = frozenset(
    f"{module}.{name}"
    for module in ("typing", "typing_extensions")
    for name in ("overload", "override", "final")
)


def _is_inert_decorator(
    node: ast.expr,
    scope: ModuleScope,
    shadowed: Collection[str] = (),
    stars: StarBases | None = None,
) -> bool:
    """Only the ``typing``/``typing_extensions`` decorators, resolved through
    the module's imports: a project decorator named ``final`` may do anything,
    and so may a name also bound some other way (see _canonical_names)."""
    names = _canonical_names(node, scope, shadowed, stars)
    return names is not None and names <= _INERT_DECORATORS


def _is_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_literal(k) for k in node.keys) and all(
            _is_literal(v) for v in node.values
        )
    return False


def _inert_def(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: ModuleScope,
    shadowed: Collection[str] = (),
) -> bool:
    """Whether executing the ``def`` runs no code beyond binding the name:
    inert decorators and literal defaults (annotations are checked apart)."""
    defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    return all(_is_inert_decorator(d, scope, shadowed) for d in node.decorator_list) and all(
        _is_literal(d) for d in defaults
    )


# Decorators that take the function and return a wrapper or the function
# itself without calling it: executing a ``def`` under them runs no code of
# the project (``quiet_header``). By canonical name; ``pytest.mark`` and its
# attributes as a prefix; builtins only where the module does not rebind them.
_QUIET_DECORATORS = _INERT_DECORATORS | frozenset(
    {
        "pytest.fixture",
        "pytest.yield_fixture",
        "functools.cache",
        "functools.lru_cache",
        "functools.cached_property",
        "functools.singledispatch",
        "functools.singledispatchmethod",
        "contextlib.contextmanager",
        "contextlib.asynccontextmanager",
        "abc.abstractmethod",
    }
)
_QUIET_PREFIXES = ("pytest.mark.",)
_QUIET_BUILTINS = frozenset({"property", "staticmethod", "classmethod"})
# Calls a quiet decorator's arguments may make: they build a value and call
# nothing back (``pytest.param(1, id="one")``).
_QUIET_CALLS = frozenset({"pytest.param"})
# Class decorators that read the class's namespace and add methods to it,
# calling none of its code (what a class attribute's descriptor runs when
# read is the class body's dependency already).
_PASSIVE_CLASS_DECORATORS = frozenset({"dataclasses.dataclass", "functools.total_ordering"})


# What a module's star imports may bind a name to, as dotted names
# (Resolver._star_bases); by default each star module's name of it.
StarBases = Callable[[str], Iterable[str]]


def _fixture_scope(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """ "function" for a function-scoped fixture (a ``fixture``/
    ``yield_fixture`` decorator, called or not, with no ``scope`` or a
    literal ``scope="function"``), "other" for any other fixture (a wider or
    computed scope), "" for a function that is no fixture."""
    found = ""
    for decorator in node.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts = _flatten_chain(func)
        if not parts or parts[-1] not in ("fixture", "yield_fixture"):
            continue
        found = "function"
        if isinstance(decorator, ast.Call):
            for kw in decorator.keywords:
                if kw.arg is None or (
                    kw.arg == "scope"
                    and not (isinstance(kw.value, ast.Constant) and kw.value.value == "function")
                ):
                    return "other"
    return found


def _canonical_names(
    node: ast.expr,
    scope: ModuleScope,
    shadowed: Collection[str] = (),
    stars: StarBases | None = None,
) -> frozenset[str] | None:
    """Every dotted name a name chain may stand for at module level, through
    the module's imports: the import binding of its head and each other one
    (``try: from typing import final`` / ``except ImportError: from
    typing_extensions import final``), and what each star import may bind
    it to (``stars``; by default the star module's name of it, which is not
    known in pass 1), whatever the order. A builtin nothing imports stands
    for itself. None when the head is also bound some other way (a
    definition or an assignment in the module, or a name ``shadowed`` by the
    class body the chain is evaluated in), or not at all: what it holds is
    not known then."""
    parts = _flatten_chain(node)
    return None if parts is None else _chain_names(parts, scope, shadowed, stars)


def _chain_names(
    parts: list[str],
    scope: ModuleScope,
    shadowed: Collection[str] = (),
    stars: StarBases | None = None,
) -> frozenset[str] | None:
    """_canonical_names of a chain given as its names."""
    head, rest = parts[0], parts[1:]
    if head in shadowed or head in scope.members or head in scope.bindings:
        return None
    binding = scope.imports.get(head)
    if binding is not None:
        bases = {binding.dotted, *(b.dotted for b in scope.alt_imports.get(head, ()))}
    elif head in BUILTIN_NAMES:
        bases = {head}
    else:
        return None
    if stars is not None:
        bases.update(stars(head))
    else:
        bases.update(f"{star}.{head}" for star in scope.star_imports)
    return frozenset(".".join([base, *rest]) for base in bases)


def _canonical(node: ast.expr, scope: ModuleScope, shadowed: Collection[str] = ()) -> str | None:
    """The one dotted name a name chain stands for (_canonical_names), or
    None when it may stand for several or is not known."""
    names = _canonical_names(node, scope, shadowed)
    return next(iter(names)) if names is not None and len(names) == 1 else None


def _quiet_names(names: frozenset[str] | None) -> bool:
    return names is not None and all(
        name in _QUIET_DECORATORS or name in _QUIET_BUILTINS or name.startswith(_QUIET_PREFIXES)
        for name in names
    )


# Modules whose classes and attributes run no code of the project when
# subscripted or read (``typing.Optional[X]``, ``collections.abc.Callable``).
_INERT_TYPE_MODULES = frozenset(
    {"typing", "typing_extensions", "collections", "types", "abc", "builtins"}
)


def _reads_only_builtins(node: ast.Subscript | ast.Attribute, scope: ModuleScope) -> bool:
    """Whether subscripting (``X[...]``) or reading an attribute of
    (``X.attr``) what ``node`` reads off runs no code of the project: a
    builtin (``list[int]``) or a name of ``_INERT_TYPE_MODULES``. A class of
    the project may define ``__class_getitem__`` or have a metaclass with
    ``__getitem__``, a module ``__getattr__``, an attribute a descriptor."""
    names = _canonical_names(node.value, scope)
    return names is not None and all(
        name.split(".")[0] in _INERT_TYPE_MODULES or ("." not in name and name in BUILTIN_NAMES)
        for name in names
    )


def _calls_nothing(node: ast.AST, scope: ModuleScope) -> bool:
    """No call anywhere in ``node`` but the value-building ``_QUIET_CALLS``,
    no subscript or attribute read that may run project code
    (_reads_only_builtins: ``Registry["k"]`` runs a ``__class_getitem__``),
    and nothing that defers code (a lambda or comprehension builds a
    function; an await or yield cannot appear here)."""
    callees: set[int] = set()  # a callee's chain is judged as the call
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            names = _canonical_names(sub.func, scope)
            if names is None or not names <= _QUIET_CALLS:
                return False
            callees.update(id(n) for n in ast.walk(sub.func))
        elif isinstance(sub, (ast.Subscript, ast.Attribute)) and id(sub) not in callees:
            if not _reads_only_builtins(sub, scope):
                return False
        elif isinstance(sub, (ast.Lambda, *COMPREHENSIONS, ast.NamedExpr)):
            return False
    return True


def _passive_decorator(
    node: ast.expr,
    scope: ModuleScope,
    shadowed: Collection[str] = (),
    stars: StarBases | None = None,
) -> bool:
    """A decorator known to keep and call nothing of what it decorates while
    the ``def`` or ``class`` statement runs: a quiet one (it returns the
    function or a wrapper), or one of ``_PASSIVE_CLASS_DECORATORS``. Any
    other may call it then, as the explicit ``deco(f)`` may (see
    Resolver._decorated_at_import)."""
    func = node.func if isinstance(node, ast.Call) else node
    names = _canonical_names(func, scope, shadowed, stars)
    return _quiet_names(names) or (names is not None and names <= _PASSIVE_CLASS_DECORATORS)


# Callees that may be handed the decorated function (or a wrapper of it)
# without calling it.
_PASSIVE_CALLEES = frozenset(
    {
        "functools.wraps",
        "functools.update_wrapper",
        "typing.cast",
        "typing_extensions.cast",
        "setattr",
        "getattr",
        "hasattr",
        "isinstance",
        "callable",
    }
)
# Attributes of a function whose value is a string: a method called on one
# (``f.__doc__.format(...)``) runs nothing of the function.
_STRING_ATTRIBUTES = frozenset({"__doc__", "__name__", "__qualname__", "__module__"})


def _own_nodes(node: ast.FunctionDef | ast.AsyncFunctionDef):
    """(node, parent) for the code a call of ``node`` runs right away: its
    body, nested definitions' decorators, defaults and annotations, not
    their bodies. A lambda or class inside is yielded whole, unwalked."""
    stack: list[tuple[ast.AST, ast.AST]] = [(stmt, node) for stmt in node.body]
    while stack:
        current, parent = stack.pop()
        yield current, parent
        if isinstance(current, (ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(current, FUNC_NODES):
            children: list[ast.AST] = [*current.decorator_list, *current.args.defaults]
            children += [d for d in current.args.kw_defaults if d is not None]
            children += [a.annotation for a in _annotated_args(current.args) if a.annotation]
            if current.returns is not None:
                children.append(current.returns)
        else:
            children = list(ast.iter_child_nodes(current))
        stack.extend((child, current) for child in children)


# How a decorator treats what it decorates (Symbol.passive_decorator):
# calls nothing of it and returns it itself (SAME), or may return something
# else, a wrapper calling it with arguments of its own (WRAPPER).
SAME = "same"
WRAPPER = "wrapper"


def _passive_decorator_def(
    node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ModuleScope, shadowed: Collection[str]
) -> str:
    """SAME or WRAPPER when calling the function with a function as its
    first argument calls nothing of that function and hands it to nothing
    that might, else "": its own code (nested functions' bodies run later)
    only returns it, reads or sets its attributes, compares it by identity,
    or passes it to ``_PASSIVE_CALLEES``; the same holds for every nested
    function, which it may return (a wrapper). ``set_module`` setting
    ``__module__`` is SAME, a decorator returning a ``functools.wraps``
    wrapper WRAPPER; one that calls it, registers it, or hands it to other
    code is neither."""
    if isinstance(node, ast.AsyncFunctionDef):
        return ""
    params = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
    if not params:
        return ""
    nodes = list(_own_nodes(node))
    tracked = {params[0]}
    for current, _ in nodes:
        if isinstance(current, ast.ClassDef):
            return ""  # a class body runs now
        if isinstance(current, FUNC_NODES):
            tracked.add(current.name)
    local = {*shadowed, *_rebound_names(node), *params}
    parents = {id(current): parent for current, parent in nodes}
    for current, parent in nodes:
        if isinstance(current, ast.Lambda):
            if any(isinstance(n, ast.Name) and n.id in tracked for n in ast.walk(current)):
                return ""
            continue
        if not (isinstance(current, ast.Name) and current.id in tracked):
            continue
        if not isinstance(current.ctx, ast.Load):
            continue
        if not _passive_use(current, parent, parents.get(id(parent)), scope, local):
            return ""
    returns = [current for current, _ in nodes if isinstance(current, ast.Return)]
    rebound = any(
        isinstance(current, ast.Name)
        and current.id == params[0]
        and not isinstance(current.ctx, ast.Load)
        for current, _ in nodes
    )
    same = (
        bool(returns)
        and not rebound
        and all(isinstance(r.value, ast.Name) and r.value.id == params[0] for r in returns)
    )
    return SAME if same else WRAPPER


def _passive_use(
    name: ast.Name,
    parent: ast.AST,
    grandparent: ast.AST | None,
    scope: ModuleScope,
    local: Collection[str],
) -> bool:
    if isinstance(parent, ast.Return):
        return True
    if isinstance(parent, ast.Attribute):
        # ``f.attr`` read or set, not called: a further step only on a string.
        if isinstance(grandparent, ast.Call) and grandparent.func is parent:
            return False
        return not isinstance(grandparent, (ast.Attribute, ast.Subscript)) or (
            parent.attr in _STRING_ATTRIBUTES
        )
    if isinstance(parent, ast.Compare):
        return all(isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops)
    if isinstance(parent, ast.keyword):
        parent = grandparent if isinstance(grandparent, ast.Call) else parent
    if isinstance(parent, ast.Call) and parent.func is not name:
        callee = parent.func
        if isinstance(callee, ast.Call):  # ``functools.wraps(f)(wrapper)``
            callee = callee.func
        names = _canonical_names(callee, scope, local)
        return names is not None and names <= _PASSIVE_CALLEES
    return False


def _passive_factory_def(
    node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ModuleScope, shadowed: Collection[str]
) -> str:
    """_passive_decorator_def of what calling the function returns, when
    every value it returns is a decorator its own body defines
    (``set_module(...)`` returning ``decorator``), else "": the least
    passive of them. Its own code runs with the arguments of the decorator
    expression, which the module refers to anyway."""
    if isinstance(node, ast.AsyncFunctionDef):
        return ""
    nested: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = defaultdict(list)
    returns: list[ast.Return] = []
    for current, _ in _own_nodes(node):
        if isinstance(current, FUNC_NODES):
            nested[current.name].append(current)
        elif isinstance(current, ast.Return):
            returns.append(current)
    local = {*shadowed, *_rebound_names(node)}
    if not returns:
        return ""
    found: set[str] = set()
    for r in returns:
        if not (isinstance(r.value, ast.Name) and r.value.id in nested):
            return ""
        found.update(_passive_decorator_def(d, scope, local) for d in nested[r.value.id])
    return _least_passive(found)


def _least_passive(kinds: Collection[str]) -> str:
    """Of several _passive_decorator_def results: "" if any is, else
    WRAPPER if any is, else SAME ("" for none)."""
    if not kinds or "" in kinds:
        return ""
    return WRAPPER if WRAPPER in kinds else SAME


_DISPATCHERS = frozenset({"functools.singledispatch", "functools.singledispatchmethod"})


def _dispatcher_def(
    node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ModuleScope, shadowed: Collection[str]
) -> bool:
    """Whether the outermost decorator makes the function a
    ``functools.singledispatch`` (or ``singledispatchmethod``) dispatcher."""
    if not node.decorator_list:
        return False
    names = _canonical_names(node.decorator_list[0], scope, shadowed)
    return names is not None and names <= _DISPATCHERS


_PROPERTY_ACCESSORS = frozenset({"setter", "getter", "deleter"})


def _property_accessors(
    body: list[ast.stmt],
    scope: ModuleScope,
    shadowed: Collection[str],
    bound: Collection[str],
    stars: StarBases | None = None,
) -> frozenset[int]:
    """The decorators ``@x.setter`` (``.getter``, ``.deleter``) of a class
    body, by ``id``, whose ``x`` is the property the body's previous ``def x``
    made (``@property``, or such an accessor in turn) and that nothing else
    in the body binds (``bound``): the accessor returns a copy of the
    property holding the function and calls nothing."""
    properties: set[str] = set()
    found: set[int] = set()
    for stmt in iter_scope_statements(body):
        if not isinstance(stmt, FUNC_NODES):
            continue
        for dec in stmt.decorator_list:
            parts = _flatten_chain(dec)
            if (
                parts is not None
                and len(parts) == 2
                and parts[0] in properties
                and parts[1] in _PROPERTY_ACCESSORS
            ):
                found.add(id(dec))
        outermost = stmt.decorator_list[0] if stmt.decorator_list else None
        if (
            outermost is not None
            and stmt.name not in bound
            and (
                id(outermost) in found
                or _canonical_names(outermost, scope, shadowed, stars) == {"property"}
            )
        ):
            properties.add(stmt.name)
        else:
            properties.discard(stmt.name)
    return frozenset(found)


def _quiet_def(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: ModuleScope,
    shadowed: Collection[str] = (),
) -> bool:
    """Whether executing the ``def`` runs no code of the project: every
    decorator a quiet one (with arguments that call nothing), literal
    defaults, annotations that call nothing. The function's body does not
    run then, so nothing it calls runs at import."""
    for decorator in node.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        if not _quiet_names(_canonical_names(func, scope, shadowed)):
            return False
        if isinstance(decorator, ast.Call) and not all(
            _calls_nothing(a, scope) for a in (*decorator.args, *decorator.keywords)
        ):
            return False
    defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    if not all(_is_literal(d) for d in defaults):
        return False
    annotations = [a.annotation for a in _annotated_args(node.args)]
    if node.returns is not None:
        annotations.append(node.returns)
    return all(_calls_nothing(a, scope) for a in annotations if a is not None)


def _has_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return node.returns is not None or any(
        isinstance(a, ast.arg) and a.annotation is not None for a in ast.walk(node.args)
    )


def _future_annotations(scope: ModuleScope) -> bool:
    binding = scope.imports.get("annotations")
    return binding is not None and binding.module == "__future__"


def _is_staticmethod(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return _has_decorator(node, "staticmethod")


# Defining one of these changes what ``self.<attr>`` reads or writes.
_ATTRIBUTE_HOOKS = frozenset({"__setattr__", "__delattr__", "__getattr__", "__getattribute__"})


def _rebound_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every name the function's body (nested scopes included, which only
    over-approximates) binds, deletes or imports."""
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and not isinstance(inner.ctx, ast.Load):
            names.add(inner.id)
        elif isinstance(inner, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and inner.name:
            names.add(inner.name)
        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in inner.names}
    return names


# Builtin container types by constructor (canonical name) and by display:
# ``clear``, ``append`` or ``update`` on one behave the same whatever it
# holds (Symbol.builtin_container, edges with the detail ``writes``).
_CONTAINER_CALLS = {
    "dict": "dict",
    "list": "list",
    "set": "set",
    "collections.defaultdict": "dict",
    "collections.OrderedDict": "dict",
    "collections.Counter": "dict",
    "collections.deque": "deque",
    "weakref.WeakValueDictionary": "dict",
    "weakref.WeakKeyDictionary": "dict",
    "weakref.WeakSet": "set",
}
_CONTAINER_NODES: tuple[tuple[type, str], ...] = (
    (ast.Dict, "dict"),
    (ast.DictComp, "dict"),
    (ast.List, "list"),
    (ast.ListComp, "list"),
    (ast.Set, "set"),
    (ast.SetComp, "set"),
)


def _container_kind(scope: ModuleScope, value: ast.expr) -> str:
    """``dict``, ``list``, ``set`` or ``deque`` when ``value`` builds a builtin
    container of that kind (``{}``, ``set()``, ``collections.defaultdict(
    list)``), else "". A name the module binds itself is not the builtin,
    and one bound several ways must build the same kind each way
    (_canonical_names)."""
    for node_type, kind in _CONTAINER_NODES:
        if isinstance(value, node_type):
            return kind
    if not isinstance(value, ast.Call):
        return ""
    names = _canonical_names(value.func, scope)
    kinds = {_CONTAINER_CALLS.get(name, "") for name in names or ()}
    return kinds.pop() if len(kinds) == 1 else ""

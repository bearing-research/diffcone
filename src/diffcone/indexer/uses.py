"""How code uses the objects it can name, for the narrowings that bound a
lookup by what a literal table or an external module holds.

Such a narrowing holds only while nothing can change the table or the
module. This module decides, from the syntax alone (pass 1, cacheable per
module), which uses are reads the analysis recognises; every other use
counts as a possible change:

* a literal dict, list or set stays bounded only while every use of it reads
  it: an item read (``T[k]``, ``T.get(k)``), a non-mutating method, iteration,
  a comparison, a test, an argument of a builtin that only reads
  (``len(T)``, ``sorted(T)``). Bound to another name, passed to a function,
  returned, used as a default, put in a container, an item or attribute
  assigned, or a mutating method (``T.update``) reached: it may change.
* a module object (named through an import, ``sys.modules[...]``,
  ``import_module(...)`` or ``__import__``) may be read from (``m.x``,
  ``getattr(m, n)``, ``m.f()``); bound to a name, passed (``setattr(m, ...)``
  and ``vars(m)`` included), returned, or reached through ``__dict__``,
  ``__setattr__`` or ``__delattr__``, it may be written to. An attribute
  stored on it (``m.T = v``, ``setattr(m, "T", v)``, ``m.__dict__["T"] =
  v``, a dotted string a ``patch``/``monkeypatch`` call names) rebinds that
  attribute.
* a module named at run time by a name nothing bounds is any module (``*``);
  by a name starting with literal text (``f"plugins.{name}"``, ``"cell_" +
  key``, ``f"{__name__}.{name}"``), any module with that prefix
  (``plugins.*``). A function-local name bound only to modules and only
  read is followed instead of counted as an escape (``mod =
  import_module(name)`` then ``getattr(mod, attr)``); returned or passed
  on, it escapes.
* a module made here (``types.ModuleType(name)``, ``FRESH``) is no module
  other code holds, and nothing done to it is recorded, unless it is
  installed in ``sys.modules`` under a name: then it is the modules that
  name may be.
* ``globals()``, ``vars()``/``locals()`` without arguments, and ``exec``/
  ``eval`` of code that is not a literal reach this module's namespace, and
  code ``exec`` runs can reach what the module's globals can name; given a
  namespace of its own (``exec(code, m.__dict__)``), it reaches that one,
  which counts where it stands. A literal code string is read as the code
  it is.

The records name what they reach by absolute dotted name, as far as the
module's own imports say; the indexer resolves them over every module
between the passes (``Indexer._apply_uses``) and after pass 2 for external
modules (``Indexer._external_lookups``).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field

# Every method of the builtin list, dict and set that changes the container
# in place (and the dunders behind ``+=``, ``|=`` and item assignment). The
# literal tables the analysis bounds are displays of exactly these types, so
# any other attribute of one is a read.
CONTAINER_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "popitem",
        "update",
        "setdefault",
        "add",
        "discard",
        "intersection_update",
        "difference_update",
        "symmetric_difference_update",
        "__setitem__",
        "__delitem__",
        "__iadd__",
        "__imul__",
        "__ior__",
        "__iand__",
        "__isub__",
        "__ixor__",
        "__init__",
    }
)

# Attributes that write an object (or hand out its namespace) whatever it is.
_WRITING_ATTRIBUTES = frozenset({"__setattr__", "__delattr__", "__dict__"})

# Builtins that only read their positional arguments (or iterate them): a
# table or module passed to one is not changed by it.
SAFE_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "ascii",
        "bool",
        "callable",
        "dict",
        "dir",
        "enumerate",
        "filter",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "print",
        "repr",
        "reversed",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "zip",
    }
)

# Calls whose result is a module named by their first argument.
_IMPORT_CALLS = frozenset({"importlib.import_module", "importlib.__import__"})

# Calls whose result is a new, empty module object, no module any code
# imported. (``importlib.util.module_from_spec`` is not one: once its loader
# runs, the module is a copy of whatever module the spec names.)
_FRESH_MODULES = frozenset({"types.ModuleType"})

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

READ, USE, MUT, DYN = "read", "use", "mut", "dyn"
STORE = "store"
ANY = "*"
# A module created here (``types.ModuleType(name)``): no module any other
# code holds, so nothing done to it is recorded, unless it is installed in
# ``sys.modules`` (then it is the modules its name there may be).
FRESH = "+"


@dataclass(frozen=True)
class UseRecord:
    """``kind`` of ``target`` seen in this module: ``use`` (the object may be
    changed: handed on, or its namespace reached), ``mut`` (a container
    mutator reached on it, or an item assigned), ``store`` (the attribute
    ``target`` names is rebound), ``dyn`` (an attribute read by a run-time
    name off it is handed on). ``target`` is an absolute dotted name, ``*``
    (any module) or ``*.NAME``, ``PREFIX*`` or ``PREFIX*.NAME`` (any module
    whose name starts with PREFIX), or ``@NAME`` (a name this module may have
    from a star import). ``sources`` are this module's literal names a
    bounded target was computed from: if one of them is not the literal it
    was, the target is any module. ``writer`` is the def path the use is in."""

    kind: str
    target: str
    sources: tuple[str, ...] = ()
    writer: tuple[str, ...] = ()

    def to_list(self) -> list:
        return [self.kind, self.target, list(self.sources), list(self.writer)]

    @staticmethod
    def from_list(data: list) -> UseRecord:
        kind, target, sources, writer = data
        return UseRecord(str(kind), str(target), tuple(sources), tuple(writer))


@dataclass
class Uses:
    """What a scan found: bare names used other than as reads (a container
    bound to one is not the literal it was), records for what other modules
    hold, and whether this module's own namespace may change wholesale."""

    names: set[str] = field(default_factory=set)
    records: set[UseRecord] = field(default_factory=set)
    namespace: bool = False


def _parents(root: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _bound_names(root: ast.AST) -> set[str]:
    """Every name the code binds anywhere (a builtin of that name is shadowed
    somewhere, so it is not taken for the builtin)."""
    names: set[str] = set()
    for node in ast.walk(root):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
    return names


def classify(node: ast.AST, parents: dict[int, ast.AST], bound: set[str]) -> str:
    """How the value of ``node`` is used where it stands: ``read``, ``mut``
    (an item assigned or deleted, a container mutator reached) or ``use``
    (anything else: it may be changed by whoever gets it)."""
    while True:
        p = parents.get(id(node))
        if p is None or isinstance(p, ast.Expr):
            return READ
        if isinstance(p, ast.BoolOp) or (isinstance(p, ast.IfExp) and node is not p.test):
            node = p  # the operand is the expression's value
            continue
        if isinstance(p, ast.Subscript):
            if node is p.value and not isinstance(p.ctx, ast.Load):
                return MUT
            return READ
        if isinstance(p, ast.Attribute):
            if not isinstance(p.ctx, ast.Load):
                return USE
            if p.attr in CONTAINER_MUTATORS:
                return MUT
            if p.attr in ("__setattr__", "__delattr__"):
                return USE
            if p.attr == "__dict__":
                return READ if classify(p, parents, bound) == READ else USE
            return READ
        if isinstance(p, ast.Call):
            if node is p.func:
                return READ
            if any(a is node for a in p.args) and _builtin(p.func, bound) in SAFE_BUILTINS:
                return READ
            return USE
        if isinstance(p, ast.Starred):
            # ``f(*T)`` hands on the elements, ``[*T]`` copies them.
            outer = parents.get(id(p))
            return READ if isinstance(outer, (ast.Call, ast.List, ast.Tuple, ast.Set)) else USE
        if isinstance(p, ast.Dict):
            # ``{**T}`` copies it; a value stored in a display is handed on.
            for key, value in zip(p.keys, p.values, strict=True):
                if value is node:
                    return READ if key is None else USE
            return READ  # a key: hashed
        if isinstance(p, (ast.Compare, ast.UnaryOp, ast.BinOp, ast.FormattedValue, ast.Await)):
            return READ
        if isinstance(p, (ast.For, ast.AsyncFor, ast.comprehension)):
            return READ if node is p.iter or node in getattr(p, "ifs", ()) else USE
        if isinstance(p, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            return READ
        if isinstance(p, ast.withitem):
            return READ if node is p.context_expr else USE
        if isinstance(p, ast.ClassDef) and any(b is node for b in p.bases):
            return READ
        if isinstance(p, ast.Slice):
            return READ
        if isinstance(p, ast.AugAssign) and node is p.target:
            return MUT
        return USE


def _builtin(func: ast.AST, bound: set[str]) -> str | None:
    if isinstance(func, ast.Name) and func.id not in bound:
        return func.id
    return None


def _chain(expr: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return parts[::-1]


def _constant(expr: ast.AST | None) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def _patch_like(parts: list[str]) -> bool:
    """A call naming what it patches by a dotted string first argument."""
    last = parts[-1]
    return (
        (last in ("setattr", "delattr") and len(parts) > 1)
        or last == "patch"
        or parts[-2:] in (["patch", "object"], ["patch", "dict"], ["patch", "multiple"])
    )


def _setattr_like(parts: list[str], builtin: str | None) -> bool:
    """A call storing on its first argument the attribute its second names."""
    return (
        builtin in ("setattr", "delattr")
        or (parts[-1] in ("setattr", "delattr") and len(parts) > 1)
        or parts[-2:] == ["patch", "object"]
    )


# Evaluates a string expression in its scope (the innermost function, lambda
# or class around it, or the module), with the names bound between the two
# (comprehension variables, an enclosing function's locals) unbounded: the
# strings it may be (None when unbounded) and the module-level literal names
# that told (literals.make_evaluator).
Evaluate = Callable[
    [ast.expr, ast.AST, frozenset[str]], tuple[tuple[str, ...] | None, tuple[str, ...]]
]


def _no_evaluate(
    expr: ast.expr, scope: ast.AST, shadowed: frozenset[str]
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    return _constant_tuple(expr), ()


def _constant_tuple(expr: ast.expr) -> tuple[str, ...] | None:
    value = _constant(expr)
    return (value,) if value is not None else None


class _Scanner:
    def __init__(
        self,
        root: ast.AST,
        *,
        module: str,
        is_package: bool,
        evaluate: Evaluate,
        records: bool,
        imports: dict[str, set[str]] | None = None,
        bound: set[str] | None = None,
        prefix: tuple[str, ...] = (),
    ) -> None:
        self.root = root
        self.module = module
        self.is_package = is_package
        self.evaluate = evaluate
        self.want_records = records
        self.parents = _parents(root)
        self.bound = bound if bound is not None else _bound_names(root)
        self.imports = imports if imports is not None else self._import_map(root)
        self.stars = any(
            isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
            for n in ast.walk(root)
        )
        self.prefix = prefix
        self.out = Uses()
        self.aliases: dict[tuple[int, str], tuple[set[str], tuple[str, ...]]] = {}
        self.aliased_values: set[int] = set()

    # ------------------------------------------------------------ names

    def _import_map(self, root: ast.AST) -> dict[str, set[str]]:
        """Name -> the absolute dotted names an import binds it to, from every
        import anywhere in the code (a function-local one included)."""
        from diffcone.indexer.scopes import resolve_relative_module

        found: dict[str, set[str]] = {}
        for node in ast.walk(root):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        found.setdefault(alias.asname, set()).add(alias.name)
                    else:
                        head = alias.name.split(".")[0]
                        found.setdefault(head, set()).add(head)
            elif isinstance(node, ast.ImportFrom):
                base = resolve_relative_module(
                    self.module, self.is_package, node.module, node.level
                )
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    target = f"{base}.{alias.name}" if base else alias.name
                    found.setdefault(alias.asname or alias.name, set()).add(target)
        return found

    def _canonical(self, expr: ast.AST) -> set[str]:
        """The dotted names a callee or value chain may be: through the
        imports, or a builtin's own name."""
        parts = _chain(expr)
        if parts is None:
            return set()
        head, rest = parts[0], parts[1:]
        if head in self.imports:
            return {".".join([t, *rest]) for t in self.imports[head]}
        if head not in self.bound:
            return {".".join(parts)}
        return set()

    def _scope_of(self, node: ast.AST) -> ast.AST:
        p = self.parents.get(id(node))
        while p is not None and not isinstance(p, _SCOPES):
            p = self.parents.get(id(p))
        return p if p is not None else self.root

    def _evaluate(self, expr: ast.expr) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        from diffcone.indexer.literals import _collect_store_names, _LocalBindings

        scope = self._scope_of(expr)
        shadowed: set[str] = set()
        p = self.parents.get(id(expr))
        while p is not None and p is not scope:
            if isinstance(p, ast.comprehension):
                shadowed |= _collect_store_names(p.target)
            p = self.parents.get(id(p))
        outer = self.parents.get(id(scope)) if scope is not self.root else None
        while outer is not None:
            if isinstance(outer, _SCOPES):
                shadowed |= _LocalBindings().collect(outer)
            elif isinstance(outer, ast.comprehension):
                shadowed |= _collect_store_names(outer.target)
            outer = self.parents.get(id(outer))
        return self.evaluate(expr, scope, frozenset(shadowed))

    def _writer(self, node: ast.AST) -> tuple[str, ...]:
        path: list[str] = []
        p = self.parents.get(id(node))
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                path.append(p.name)
            p = self.parents.get(id(p))
        return self.prefix + tuple(reversed(path))

    # ------------------------------------------------------------ module handles

    def _own_name(self) -> str | None:
        return self.module if "__name__" not in self.bound else None

    def _module_name(
        self, expr: ast.expr | None, package: ast.expr | None = None
    ) -> tuple[set[str], tuple[str, ...]]:
        """The modules a run-time name may be: the literals it evaluates to,
        this module for ``__name__``, every module whose name starts with a
        literal prefix (``f"plugins.{name}"``, ``"cell_" + key``,
        ``f"{__name__}.{name}"``: the pattern ``PREFIX*``), or any module.
        ``package`` is ``import_module``'s, for a relative prefix."""
        if expr is None:
            return {ANY}, ()
        if isinstance(expr, ast.Name) and expr.id == "__name__" and "__name__" not in self.bound:
            return {self.module}, ()
        names, sources = self._evaluate(expr)
        if names is None and isinstance(expr, ast.JoinedStr):
            names, sources = self._joined(expr)
        if names is not None and not any(n.startswith(".") or not n for n in names):
            return set(names), sources
        if names is None:
            from diffcone.indexer.scopes import _resolve_relative_name, _string_prefix

            prefix = _string_prefix(expr, self._own_name())
            if prefix is not None and prefix.startswith("."):
                own = self._package_name(package)
                prefix = _resolve_relative_name(prefix, own, prefix=True) if own else None
            if prefix:
                return {prefix + ANY}, ()
        return {ANY}, ()

    def _joined(self, expr: ast.JoinedStr) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        """The strings an f-string may be when each part is bounded
        (``f"{name}.{sub}"`` with both bound to literals), up to a few."""
        texts: list[str] = [""]
        sources: tuple[str, ...] = ()
        for value in expr.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts: tuple[str, ...] | None = (value.value,)
            elif (
                isinstance(value, ast.FormattedValue)
                and value.conversion in (-1, ord("s"))
                and value.format_spec is None
            ):
                parts, more = self._evaluate(value.value)
                sources += more
            else:
                parts = None
            if parts is None or len(texts) * len(parts) > 64:
                return None, ()
            texts = [t + p for t in texts for p in parts]
        return tuple(texts), sources

    def _package_name(self, package: ast.expr | None) -> str | None:
        """``import_module``'s ``package`` when it is this module's
        ``__name__`` or ``__package__``."""
        if isinstance(package, ast.Name) and package.id in ("__name__", "__package__"):
            if package.id in self.bound:
                return None
            if package.id == "__package__" and not self.is_package:
                return self.module.rpartition(".")[0] or None
            return self.module
        return None

    def _handle(self, node: ast.AST) -> tuple[set[str], tuple[str, ...]] | None:
        """The modules an expression yields when it is ``sys.modules[...]``,
        ``sys.modules.get(...)``, ``import_module(...)`` or ``__import__``."""
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if "sys.modules" in self._canonical(node.value) and not isinstance(
                node.slice, ast.Slice
            ):
                return self._module_name(node.slice)
            return None
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        first = node.args[0] if node.args else None
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("get", "pop", "setdefault")
            and "sys.modules" in self._canonical(func.value)
        ):
            return self._module_name(first)
        names = self._canonical(func)
        if names & _IMPORT_CALLS:
            package = node.args[1] if len(node.args) > 1 else None
            for k in node.keywords:
                if k.arg == "package":
                    package = k.value
            literal = _constant(first)
            if literal is not None and literal.startswith(".") and package is not None:
                if isinstance(package, ast.Name) and package.id in ("__name__", "__package__"):
                    from diffcone.indexer.scopes import _resolve_relative_name

                    own = self.module
                    if package.id == "__package__" and not self.is_package:
                        own = own.rpartition(".")[0]
                    resolved = _resolve_relative_name(literal, own)
                    return ({resolved}, ()) if resolved else ({ANY}, ())
                return {ANY}, ()
            return self._module_name(first, package)
        if "__import__" in names:
            found, sources = self._module_name(first)
            fromlist = node.args[3] if len(node.args) > 3 else None
            for k in node.keywords:
                if k.arg == "fromlist":
                    fromlist = k.value
            empty = isinstance(fromlist, (ast.List, ast.Tuple)) and not fromlist.elts
            if fromlist is None or empty:
                found = {n if n == ANY else n.split(".")[0] for n in found}
            return found, sources
        if names & {"inspect.getmodule", "runpy.run_path", "runpy.run_module"}:
            return {ANY}, ()
        if names & _FRESH_MODULES:
            return {FRESH}, ()
        return None

    def _install_keys(self, node: ast.AST) -> list[ast.expr] | None:
        """The names ``node``'s value is installed under in ``sys.modules``
        (``sys.modules[k] = node``, ``sys.modules.setdefault(k, node)``,
        ``monkeypatch.setitem(sys.modules, k, node)``,
        ``sys.modules.update({k: node})``, ``mock.patch.dict(sys.modules,
        {k: node})``), or None when it is not installed there."""
        p = self.parents.get(id(node))
        keys: list[ast.expr] = []
        if isinstance(p, ast.Assign) and p.value is node:
            keys = [
                t.slice
                for t in p.targets
                if isinstance(t, ast.Subscript) and self._is_sys_modules(t.value)
            ]
        elif isinstance(p, ast.Call) and any(a is node for a in p.args):
            func = p.func
            at = next(i for i, a in enumerate(p.args) if a is node)
            if (
                at == 1
                and isinstance(func, ast.Attribute)
                and func.attr in ("setdefault", "__setitem__")
                and self._is_sys_modules(func.value)
            ):
                keys = [p.args[0]]
            elif (
                at == 2
                and (_chain(func) or [""])[-1] == "setitem"
                and self._is_sys_modules(p.args[0])
            ):
                keys = [p.args[1]]
        elif isinstance(p, ast.Dict):
            q = self.parents.get(id(p))
            installs = isinstance(q, ast.Call) and (
                (
                    isinstance(q.func, ast.Attribute)
                    and q.func.attr == "update"
                    and self._is_sys_modules(q.func.value)
                    and p in q.args
                )
                or (
                    (_chain(q.func) or [""])[-1] == "dict"
                    and len(q.args) > 1
                    and self._is_sys_modules(q.args[0])
                    and q.args[1] is p
                )
            )
            if installs:
                keys = [k for k, v in zip(p.keys, p.values, strict=True) if v is node and k]
        return keys or None

    def _is_sys_modules(self, expr: ast.expr) -> bool:
        return "sys.modules" in self._canonical(expr)

    # ------------------------------------------------------------ the walk

    def scan(self) -> Uses:
        self._find_aliases()
        for node in ast.walk(self.root):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    self._name(node)
                continue
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                # ``T += [...]`` extends a list (or set, or dict) in place.
                self._emit_root(node.target, MUT, ())
                continue
            if isinstance(node, ast.Call):
                self._call(node)
            if self.want_records and (handle := self._handle(node)) is not None:
                targets, sources = handle
                self._climb(node, targets, sources, ())
        return self.out

    def _name(self, node: ast.Name) -> None:
        alias = self.aliases.get((id(self._scope_of(node)), node.id))
        if alias is not None:
            targets, sources = alias
            self._climb(node, targets, sources, ())
            return
        if node.id in self.imports:
            if self.want_records:
                self._climb(node, self.imports[node.id], (), ())
            return
        self._climb(node, None, (), ())

    def _call(self, node: ast.Call) -> None:
        builtin = _builtin(node.func, self.bound)
        if builtin in ("globals", "vars", "locals") and not node.args:
            # ``vars()``/``locals()`` in a function or class body is a copy of
            # its own locals; at module level, and ``globals()`` anywhere, it
            # is the module's namespace.
            if builtin == "globals" or self._scope_of(node) is self.root:
                self._namespace(node, None, (), ())
            return
        if builtin in ("exec", "eval"):
            self._exec(node, builtin)
            return
        if not self.want_records:
            return
        parts = _chain(node.func)
        if parts and _patch_like(parts) and node.args:
            target = _constant(node.args[0])
            if target is not None and all(p.isidentifier() for p in target.split(".")):
                writer = self._writer(node)
                if parts[-2:] == ["patch", "multiple"]:
                    for k in node.keywords:
                        if k.arg is not None:
                            self._record(STORE, f"{target}.{k.arg}", (), writer)
                else:
                    self._record(STORE, target, (), writer)
        canonical = self._canonical(node.func)
        if canonical & {"runpy.run_path"}:
            self._record(USE, ANY, (), self._writer(node))

    def _exec(self, node: ast.Call, builtin: str) -> None:
        code = _constant(node.args[0]) if node.args else None
        if code is not None:
            try:
                tree = ast.parse(code, mode="exec" if builtin == "exec" else "eval")
            except SyntaxError:
                tree = None
            if tree is not None:
                inner = _Scanner(
                    tree,
                    module=self.module,
                    is_package=self.is_package,
                    evaluate=_no_evaluate,
                    records=self.want_records,
                    imports={**self.imports, **self._import_map(tree)},
                    bound=self.bound | _bound_names(tree),
                    prefix=self._writer(node),
                ).scan()
                self.out.names |= inner.names
                self.out.records |= inner.records
                self.out.namespace |= inner.namespace
                return
        # Code built at run time: it runs in this module's namespace, and can
        # reach whatever its globals name; read from a file, anything. Given
        # a namespace of its own (``exec(code, m.__dict__)``, ``exec(code,
        # {})``), it runs there instead: that argument is handed on where it
        # stands (a module's ``__dict__`` is that module written to,
        # ``globals()`` this one's namespace), and this module's namespace is
        # not reached.
        from diffcone.indexer.references import _reads_files

        reads_files = bool(node.args) and _reads_files(node.args[0])
        namespace = node.args[1] if len(node.args) > 1 else None
        for k in node.keywords:
            if k.arg == "globals":
                namespace = k.value
        own = namespace is None or (isinstance(namespace, ast.Constant) and namespace.value is None)
        if own:
            self.out.namespace = True
        if not self.want_records:
            return
        writer = self._writer(node)
        if reads_files:
            self._record(USE, ANY, (), writer)
            return
        if not own:
            return
        for targets in self.imports.values():
            for target in targets:
                self._record(USE, target, (), writer)

    def _find_aliases(self) -> None:
        """Function-local names bound once to a module and used nowhere but
        in their own function: their uses are the module's."""
        if not self.want_records:
            return
        for scope in ast.walk(self.root):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stores: dict[str, list[ast.AST]] = {}
            declared: set[str] = set()
            params = {a.arg for a in ast.walk(scope.args) if isinstance(a, ast.arg)}
            nested_loads: set[str] = set()
            stack = list(ast.iter_child_nodes(scope))
            while stack:
                n = stack.pop()
                if isinstance(n, _SCOPES):
                    nested_loads |= {m.id for m in ast.walk(n) if isinstance(m, ast.Name)}
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        stores.setdefault(n.name, []).append(n)
                    continue
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                    stores.setdefault(n.id, []).append(n)
                elif isinstance(n, (ast.Global, ast.Nonlocal)):
                    declared.update(n.names)
                elif isinstance(n, ast.alias):
                    stores.setdefault(n.asname or n.name.split(".")[0], []).append(n)
                elif isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name:
                    stores.setdefault(n.name, []).append(n)
                elif isinstance(n, ast.MatchMapping) and n.rest:
                    stores.setdefault(n.rest, []).append(n)
                stack.extend(ast.iter_child_nodes(n))
            for name, nodes in stores.items():
                if name in declared or name in params or name in nested_loads:
                    continue
                # Every binding of the name assigns it a module (``mod =
                # sys.modules.get(n)`` here, ``mod = sys.modules[n]`` there).
                targets: set[str] = set()
                sources: tuple[str, ...] = ()
                values: list[int] = []
                for target in nodes:
                    assign = self.parents.get(id(target))
                    if not (
                        isinstance(assign, ast.Assign)
                        and len(assign.targets) == 1
                        and assign.targets[0] is target
                    ):
                        break
                    root = self._root_of(assign.value)
                    if root is None:
                        break
                    targets |= root[0]
                    sources += root[1]
                    values.append(id(assign.value))
                else:
                    if FRESH in targets:
                        # A module made here is the modules its name in
                        # ``sys.modules`` may be, wherever it is installed.
                        for load in ast.walk(scope):
                            if (
                                isinstance(load, ast.Name)
                                and load.id == name
                                and isinstance(load.ctx, ast.Load)
                                and (keys := self._install_keys(load)) is not None
                            ):
                                for key in keys:
                                    found, more = self._module_name(key)
                                    targets |= found
                                    sources += more
                    self.aliases[(id(scope), name)] = (targets, sources)
                    self.aliased_values.update(values)

    def _root_of(self, expr: ast.expr) -> tuple[set[str], tuple[str, ...]] | None:
        """The modules (or what they hold) an expression names, when it is a
        name chain from an import, a module handle, or an attribute chain off
        one, with nothing in it that writes."""
        attrs: list[str] = []
        while isinstance(expr, ast.Attribute):
            if expr.attr in CONTAINER_MUTATORS or expr.attr in _WRITING_ATTRIBUTES:
                return None
            attrs.append(expr.attr)
            expr = expr.value
        attrs.reverse()
        if isinstance(expr, ast.Name):
            alias = self.aliases.get((id(self._scope_of(expr)), expr.id))
            if alias is not None:
                base, sources = alias
            elif expr.id in self.imports:
                base, sources = self.imports[expr.id], ()
            else:
                return None
        else:
            handle = self._handle(expr)
            if handle is None:
                return None
            base, sources = handle
        return {_join(t, attrs) for t in base}, sources

    def _climb(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
    ) -> None:
        """Follow an object up its attribute chain to where it is used."""
        while True:
            p = self.parents.get(id(node))
            if isinstance(p, ast.Attribute) and p.value is node:
                attr = p.attr
                if targets is not None and self._is_modules_table(targets, chain):
                    if attr not in ("get", "pop", "setdefault", "keys", "__contains__"):
                        self._emit(p, targets, sources, chain, USE, any_module=True)
                    return
                if not isinstance(p.ctx, ast.Load):
                    self._emit(p, targets, sources, (*chain, attr), STORE)
                    return
                if attr in CONTAINER_MUTATORS:
                    self._emit(node, targets, sources, chain, MUT)
                    return
                if attr in ("__setattr__", "__delattr__"):
                    self._emit(node, targets, sources, chain, USE)
                    return
                if attr == "__dict__":
                    if targets is not None:
                        self._namespace(p, targets, sources, chain)
                    return
                node, chain = p, (*chain, attr)
                continue
            if targets is not None and self._is_modules_table(targets, chain):
                # ``sys.modules`` itself: an item read is a handle (scanned on
                # its own), membership and iteration see names, and installing
                # or removing an entry (``monkeypatch.setitem(sys.modules,
                # "torch", fake)``) hands nothing out; anything else hands out
                # every module.
                installs = (
                    isinstance(p, ast.Call)
                    and bool(p.args)
                    and p.args[0] is node
                    and (_chain(p.func) or [""])[-1] in ("setitem", "delitem", "dict")
                )
                if not installs and not isinstance(
                    p, (ast.Subscript, ast.Compare, ast.For, ast.comprehension)
                ):
                    self._emit(node, targets, sources, chain, USE, any_module=True)
                return
            if targets is not None and self._is_import_function(targets, chain):
                if not (isinstance(p, ast.Call) and p.func is node):
                    self._emit(node, targets, sources, chain, USE, any_module=True)
                return
            if isinstance(p, ast.Call) and p.args and p.args[0] is node:
                builtin = _builtin(p.func, self.bound)
                parts = _chain(p.func) or []
                if builtin == "getattr":
                    names, more = self._evaluate(p.args[1]) if len(p.args) > 1 else (None, ())
                    if names is None:
                        if classify(p, self.parents, self.bound) != READ:
                            self._emit(node, targets, sources, chain, DYN)
                        return
                    for name in names:
                        if name in CONTAINER_MUTATORS or name in _WRITING_ATTRIBUTES:
                            self._emit(node, targets, sources, chain, USE)
                        else:
                            self._climb(p, targets, sources + more, (*chain, name))
                    return
                if parts and _setattr_like(parts, builtin):
                    name_arg = p.args[1] if len(p.args) > 1 else None
                    for k in p.keywords:
                        if k.arg in ("name", "attribute"):
                            name_arg = k.value
                    names, more = self._evaluate(name_arg) if name_arg is not None else (None, ())
                    if names is None:
                        self._emit(node, targets, sources, chain, USE)
                    else:
                        for name in names:
                            self._emit(node, targets, sources + more, (*chain, name), STORE)
                    return
                if builtin == "vars":
                    if targets is not None:
                        self._namespace(p, targets, sources, chain)
                    return
            if id(node) in self.aliased_values:
                return  # followed through the name it is bound to
            if targets is not None and (keys := self._install_keys(node)) is not None:
                # Installed in ``sys.modules``: whoever imports that name
                # later gets this object, whatever it holds.
                for key in keys:
                    found, more = self._module_name(key)
                    self._emit(node, found, more, (), USE)
            kind = classify(node, self.parents, self.bound)
            if kind != READ:
                self._emit(node, targets, sources, chain, kind)
            return

    def _namespace(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
    ) -> None:
        """A module's namespace dict (``m.__dict__``, ``vars(m)``; this
        module's own for ``globals()`` and a bare ``vars()``, with ``targets``
        None): an item read by a known name is that attribute, read where it
        stands; by an unknown name it is any of them; anything else may
        change the namespace."""
        p = self.parents.get(id(node))
        item: ast.AST | None = None
        key: ast.expr | None = None
        if isinstance(p, ast.Subscript) and p.value is node:
            if not isinstance(p.ctx, ast.Load):
                names, more = self._evaluate(p.slice) if targets is not None else (None, ())
                if names is None:
                    self._changes(node, targets, sources, chain, MUT)
                else:
                    # ``m.__dict__["T"] = v`` rebinds ``m.T``, as ``m.T = v`` does.
                    for name in names:
                        self._emit(p, targets, sources + more, (*chain, name), STORE)
                return
            item, key = p, p.slice
        elif isinstance(p, ast.Attribute) and p.value is node:
            call = self.parents.get(id(p))
            if p.attr in ("keys", "__contains__", "__len__", "__iter__"):
                return
            if (
                p.attr in ("get", "__getitem__")
                and isinstance(call, ast.Call)
                and call.func is p
                and call.args
            ):
                item, key = call, call.args[0]
            elif p.attr in ("values", "items", "copy"):
                self._changes(node, targets, sources, chain, DYN)  # hands out every value
                return
            else:
                kind = MUT if p.attr in CONTAINER_MUTATORS else USE
                self._changes(node, targets, sources, chain, kind)
                return
        if item is None or key is None:
            if classify(node, self.parents, self.bound) != READ:
                self._changes(node, targets, sources, chain, USE)
            return
        names, more = self._evaluate(key)
        if names is None:
            if classify(item, self.parents, self.bound) != READ:
                self._changes(node, targets, sources, chain, DYN)
            return
        for name in names:
            if targets is None:
                if classify(item, self.parents, self.bound) != READ:
                    self.out.names.add(name)
                if self.want_records and name in self.imports:
                    self._climb(item, self.imports[name], more, ())
            else:
                self._climb(item, targets, sources + more, (*chain, name))

    def _changes(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
        kind: str,
    ) -> None:
        """A change to a namespace (``kind`` MUT: an item stored or a mutator
        reached), or its values handed out (USE, DYN)."""
        if targets is not None:
            # A module's namespace changing is the module written to.
            self._emit(node, targets, sources, chain, USE if kind == MUT else kind)
            return
        self.out.namespace = True
        if kind != MUT and self.want_records:
            # Whoever holds this module's globals holds what it imported.
            writer = self._writer(node)
            for imported in self.imports.values():
                for target in imported:
                    self._record(USE, target, (), writer)

    @staticmethod
    def _is_modules_table(targets: set[str], chain: tuple[str, ...]) -> bool:
        return any(_join(t, chain) == "sys.modules" for t in targets)

    @staticmethod
    def _is_import_function(targets: set[str], chain: tuple[str, ...]) -> bool:
        return any(_join(t, chain) in _IMPORT_CALLS for t in targets)

    def _emit_root(self, node: ast.AST, kind: str, chain: tuple[str, ...]) -> None:
        if isinstance(node, ast.Name) and node.id in self.imports:
            if self.want_records:
                self._emit(node, self.imports[node.id], (), chain, kind)
            return
        self._emit(node, None, (), chain, kind)

    def _emit(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
        kind: str,
        *,
        any_module: bool = False,
    ) -> None:
        if targets is None:
            # A bare name: this module's own (or a star import's).
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if not isinstance(root, ast.Name):
                return
            if not chain and kind in (USE, MUT, DYN):
                self.out.names.add(root.id)
            if self.want_records and self.stars and root.id not in self.bound:
                self._record(kind, _join("@" + root.id, chain), (), self._writer(node))
            return
        if not self.want_records:
            return
        writer = self._writer(node)
        if any_module:
            self._record(USE, ANY, (), writer)
            return
        for target in sorted(targets):
            if target != FRESH:
                self._record(kind, _join(target, chain), sources, writer)

    def _record(
        self, kind: str, target: str, sources: tuple[str, ...], writer: tuple[str, ...]
    ) -> None:
        if target == ANY:
            sources = ()  # already any module
        self.out.records.add(UseRecord(kind, target, tuple(sorted(set(sources))), writer))


def _join(target: str, chain: tuple[str, ...] | list[str]) -> str:
    return ".".join([target, *chain]) if chain else target


def scan_names(node: ast.AST) -> Uses:
    """Names ``node`` (a module, a function, any scope) uses other than as
    reads, nested scopes included, and whether it reaches its namespace."""
    return _Scanner(node, module="", is_package=False, evaluate=_no_evaluate, records=False).scan()


def scan_module(tree: ast.Module, module: str, is_package: bool, evaluate: Evaluate) -> Uses:
    """A module's records (see UseRecord), with the names of scan_names."""
    return _Scanner(
        tree, module=module, is_package=is_package, evaluate=evaluate, records=True
    ).scan()

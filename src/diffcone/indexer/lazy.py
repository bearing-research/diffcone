"""Lazily initialised constants: module variables whose only writer computes
the same value whenever it first runs.

A module variable ``V`` is a *candidate* of the module-level function ``W``
(pass 1, from the module's syntax alone, cached with its facts) when:

* the module binds ``V`` at module level exactly once, directly in its
  body, to ``None`` (``V = None``, ``V: T = None``);
* every other occurrence of the name in the module is in ``W``'s own scope
  (not a nested function, class, lambda or comprehension): a ``global V``
  statement directly in ``W``'s body, reads of ``V``, and exactly one
  store, a single-target ``V = expr`` directly in the body of an ``if``
  whose test is ``V is None`` or a ``BoolOp(Or)`` of ``X is None`` tests,
  ``V`` among them, over candidates of the same ``W`` (``if _pkg_dir is None
  or _test_dir is None``); no ``del``, augmented assignment, other binding
  (an import, a parameter, a ``for`` target, a ``def``), and no string
  constant spelling the name anywhere in the module (``getattr(mod, "V")``,
  ``__all__``); nor does the module reach a namespace by a name nothing
  bounds (``globals()``, ``vars()``, ``locals()``, ``exec``, ``eval``,
  ``__import__``, a ``__dict__``, ``sys.modules``, ``__globals__``,
  ``f_globals``);
* ``expr`` reads only builtins that compute from their arguments alone
  (``PURE_BUILTINS``), other candidates of ``W``, the module's own
  ``__file__``/``__name__``/``__package__``, and names bound by an import
  (at module level, or in ``W``'s own scope); no parameter or other local of
  ``W``, no lambda, comprehension, walrus, ``await`` or ``yield``. The
  dotted names those imports make it read are recorded for the decision.

The *decision* runs once the index is built (``decide``, giving
``SourceIndex.lazy_constants``), over the whole index, so a cold and a warm
build agree. A candidate is a lazy constant when:

* each dotted name its value reads is an in-scope module itself or its
  ``__file__``/``__name__``/``__package__`` where that module binds no such
  name, or, outside the index, a value fixed for the process or a function
  computing from its arguments alone (``PURE_EXTERNAL``: ``os.sep``,
  ``os.path.dirname``, ``sys.platform``, ``re.compile``; a flag of ``re``;
  any attribute of a fixed value, ``os.sep.join``). Not ``os.environ``,
  ``os.getcwd``, ``os.path.abspath``, a clock: a value read from the
  process's state depends on when it is read;
* no code other than ``W`` writes the variable: no ``mutated_by`` edge to
  other code, nothing registered into it, no ``rebinds``/``writes``/
  ``setitem`` reference from other code (another writer could fill one
  variable of a guard and not the other, and ``W`` would then compute
  differently depending on whether it ran first). A store through a module
  object by a name nothing bounds is not modelled as a write of any
  variable here, as elsewhere;
* every other candidate its guard or value names is one, of the same ``W``.

What it buys (diffcone.planner, diffcone.evidence_plan): ``W`` computes the
same value whoever runs it first and whenever, so ``W``'s own reads of
``V`` cannot tell whether an earlier run filled it. Running ``W`` earlier or
later (code that can run it changed, import-time code or a hook now runs
it) is no write ``W`` sees. Other code reading ``V`` may read it before
``W`` ever ran, so it still sees one: evidence planning follows those
readers as before, and static planning keeps the write unless ``W`` is the
only code depending on ``V``. A change to ``W`` itself is planned as any
change.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping

from diffcone.indexer.scopes import ImportBinding, resolve_relative_module
from diffcone.indexer.syntax import FUNC_NODES

# Builtins whose result depends on their arguments alone (no I/O, no
# process state, no callback of their own choosing).
PURE_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bin",
        "bool",
        "bytes",
        "chr",
        "dict",
        "divmod",
        "float",
        "format",
        "frozenset",
        "hex",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "list",
        "max",
        "min",
        "oct",
        "ord",
        "pow",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)

# A module's own names fixed when it is imported.
MODULE_DUNDERS = frozenset({"__file__", "__name__", "__package__"})

# Values outside the index fixed for the life of the process: they, and any
# attribute of them (a method of a str or a tuple), are pure.
EXTERNAL_VALUES = frozenset(
    {
        "os.altsep",
        "os.curdir",
        "os.devnull",
        "os.extsep",
        "os.linesep",
        "os.name",
        "os.pardir",
        "os.path.sep",
        "os.pathsep",
        "os.sep",
        "sys.base_exec_prefix",
        "sys.base_prefix",
        "sys.byteorder",
        "sys.exec_prefix",
        "sys.executable",
        "sys.hexversion",
        "sys.implementation",
        "sys.maxsize",
        "sys.platform",
        "sys.prefix",
        "sys.version",
        "sys.version_info",
    }
)

# Functions outside the index whose result depends on their arguments
# alone. Not ``os.path.abspath``/``realpath``/``relpath``/``expanduser``:
# they read the working directory, the file system or the environment.
PURE_EXTERNAL = EXTERNAL_VALUES | frozenset(
    {
        "os.path.basename",
        "os.path.commonpath",
        "os.path.commonprefix",
        "os.path.dirname",
        "os.path.join",
        "os.path.normcase",
        "os.path.normpath",
        "os.path.split",
        "os.path.splitext",
        "re.compile",
        "re.escape",
    }
)

# A candidate as pass 1 records it: (writer function name, the candidates
# its guard and value name, the dotted names its value reads through imports).
Candidate = tuple[str, tuple[str, ...], tuple[str, ...]]

_NO_NODES = (
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.NamedExpr,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
)
# Nodes opening a scope of their own.
_NESTED = (
    *FUNC_NODES,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
# Names and attributes that reach a module's namespace by a name nothing
# bounds (``globals()[n] = v``, ``exec(code)``, ``sys.modules[__name__]``,
# ``f.__globals__``): a module using one has no candidates.
_NAMESPACE_NAMES = frozenset({"exec", "eval", "globals", "vars", "locals", "__import__"})
_NAMESPACE_ATTRIBUTES = frozenset({"__dict__", "__globals__", "f_globals", "modules"})


def _own_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Every node of ``func``'s own scope: its body, without what a nested
    scope holds (a nested function's, class's, lambda's or comprehension's
    whole node, header included: an occurrence there is no occurrence in
    ``func``'s own scope)."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(reversed(func.body))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, _NESTED):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return out


def _bound_names(node: ast.AST) -> list[str]:
    """Names a node binds other than through an ``ast.Name`` store."""
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in node.names]
    if isinstance(node, (*FUNC_NODES, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    return []


def _is_none_test(test: ast.expr) -> str | None:
    """``X is None``: the name tested."""
    if (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        return test.left.id
    return None


def _guard_names(test: ast.expr) -> list[str] | None:
    """The names an ``if`` test checks for None (``V is None``, or an ``or``
    of such tests); None for any other test."""
    single = _is_none_test(test)
    if single is not None:
        return [single]
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        names = [_is_none_test(v) for v in test.values]
        if all(n is not None for n in names):
            return [n for n in names if n is not None]
    return None


def _module_none_bindings(tree: ast.Module) -> dict[str, int]:
    """Module-level names bound to ``None`` directly in the module body ->
    how many times the body binds them so (a count above one rejects)."""
    found: dict[str, int] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                continue
            name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is None:
                continue
            name, value = stmt.target.id, stmt.value
        else:
            continue
        if isinstance(value, ast.Constant) and value.value is None:
            found[name] = found.get(name, 0) + 1
    return found


def candidates(
    tree: ast.Module,
    module: str,
    is_package: bool,
    imports: Mapping[str, ImportBinding],
    alt_imports: Mapping[str, object],
    has_star: bool,
) -> dict[str, Candidate]:
    """The lazy-constant candidates of a module (see the module docstring):
    variable name -> (writer, the candidates it names, dotted reads)."""
    nones = _module_none_bindings(tree)
    if not nones:
        return {}
    writers = [
        s
        for s in tree.body
        if isinstance(s, FUNC_NODES)
        and any(isinstance(g, ast.Global) and set(g.names) & set(nones) for g in s.body)
    ]
    if not writers:
        return {}
    # Every occurrence of each name, by where it stands.
    occurrences: dict[str, list[ast.AST]] = {}
    strings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in _NAMESPACE_NAMES:
                return {}
            occurrences.setdefault(node.id, []).append(node)
        elif isinstance(node, ast.Attribute) and node.attr in _NAMESPACE_ATTRIBUTES:
            return {}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
        for name in _bound_names(node):
            occurrences.setdefault(name, []).append(node)
    top_names = {
        id(stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target)
        for stmt in tree.body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign))
    }
    defined_at_top = {s.name for s in tree.body if isinstance(s, (*FUNC_NODES, ast.ClassDef))}
    # Names the module's own top-level code binds other than by an import.
    top_bound: set[str] = set()
    top_ids = {id(stmt) for stmt in tree.body}
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            top_bound.add(node.id)
        elif not isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            if id(node) not in top_ids:  # a top-level def is in ``defined_at_top``
                top_bound.update(_bound_names(node))
        if not isinstance(node, _NESTED):
            stack.extend(ast.iter_child_nodes(node))
    found: dict[str, Candidate] = {}
    for func in writers:
        defs = [s.name for s in tree.body if isinstance(s, (*FUNC_NODES, ast.ClassDef))]
        if defs.count(func.name) != 1 or func.name in top_bound:
            continue
        own = _own_nodes(func)
        own_ids = {id(n) for n in own}
        body_ids = {id(stmt) for stmt in func.body}
        declared = {n for g in func.body if isinstance(g, ast.Global) for n in g.names}
        params = {a.arg for a in ast.walk(func.args) if isinstance(a, ast.arg)}
        local_imports: dict[str, str] = {}
        other_locals: set[str] = set(params)
        for node in own:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        local_imports[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".")[0]
                        local_imports[head] = head
            elif isinstance(node, ast.ImportFrom):
                if any(a.name == "*" for a in node.names):
                    other_locals.add("*")
                base = resolve_relative_module(module, is_package, node.module, node.level)
                for alias in node.names:
                    local_imports[alias.asname or alias.name] = f"{base}.{alias.name}"
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                if node.id not in declared:
                    other_locals.add(node.id)
            elif not isinstance(node, (ast.Global, ast.Import, ast.ImportFrom)):
                other_locals.update(n for n in _bound_names(node) if n not in declared)
        # A name imported and also bound otherwise is not known to hold the
        # module; a name imported twice, not known to hold either.
        import_counts: dict[str, int] = {}
        for node in own:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = (alias.asname or alias.name).split(".")[0]
                    import_counts[name] = import_counts.get(name, 0) + 1
        local_imports = {
            n: d
            for n, d in local_imports.items()
            if n not in other_locals and import_counts.get(n) == 1 and n not in declared
        }
        if "*" in other_locals:
            continue
        mine: dict[str, tuple[list[str], ast.Assign]] = {}
        for name in sorted(declared & set(nones)):
            if nones[name] != 1 or name in strings or name in defined_at_top:
                continue
            stores: list[ast.Name] = []
            ok = True
            for node in occurrences.get(name, ()):
                if isinstance(node, ast.Name) and id(node) in top_names:
                    continue  # the module-level ``V = None``
                if isinstance(node, ast.Global) and id(node) in body_ids:
                    continue
                if not isinstance(node, ast.Name) or id(node) not in own_ids:
                    ok = False
                    break
                if isinstance(node.ctx, ast.Load):
                    continue
                if isinstance(node.ctx, ast.Del):
                    ok = False
                    break
                stores.append(node)
            if not ok or len(stores) != 1 or _top_bound_count(tree, name) != 1:
                continue
            store = _guarded_store(func, stores[0])
            if store is None:
                continue
            assign, guard = store
            if name not in guard:
                continue
            mine[name] = (guard, assign)
        for name, (guard, assign) in mine.items():
            reads = _value_reads(
                assign.value,
                declared=declared,
                local_imports=local_imports,
                other_locals=other_locals,
                imports=imports,
                alt_imports=alt_imports,
                top_bound=top_bound | defined_at_top,
                has_star=has_star,
            )
            if reads is None:
                continue
            named, dotted = reads
            found[name] = (
                func.name,
                tuple(sorted(set(guard) | named - {name})),
                tuple(sorted(dotted)),
            )
    # A candidate whose guard or value names a name that is none (of the
    # same writer) is none, to a fixed point.
    while True:
        bad = {
            name
            for name, (writer, named, _) in found.items()
            if any(found.get(n, ("",))[0] != writer for n in named)
        }
        if not bad:
            return found
        for name in bad:
            del found[name]


def _top_bound_count(tree: ast.Module, name: str) -> int:
    """How many statements directly in the module body bind ``name``."""
    count = 0
    for stmt in tree.body:
        if isinstance(stmt, (*FUNC_NODES, ast.ClassDef)):
            continue
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Name)
                and node.id == name
                and not isinstance(node.ctx, ast.Load)
            ):
                count += 1
            elif name in _bound_names(node) and not isinstance(node, (ast.Global, ast.Nonlocal)):
                count += 1
    return count


def _guarded_store(
    func: ast.FunctionDef | ast.AsyncFunctionDef, target: ast.Name
) -> tuple[ast.Assign, list[str]] | None:
    """The assignment storing ``target`` when it is a single-target ``V =
    expr`` directly in the body of an ``if`` of ``func``'s own scope whose
    test is a None guard, with the names the guard tests."""
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED):
            continue
        if isinstance(node, ast.If):
            guard = _guard_names(node.test)
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and stmt.targets[0] is target
                ):
                    return (stmt, guard) if guard is not None else None
        stack.extend(ast.iter_child_nodes(node))
    return None


def _value_reads(
    value: ast.expr,
    *,
    declared: set[str],
    local_imports: Mapping[str, str],
    other_locals: set[str],
    imports: Mapping[str, ImportBinding],
    alt_imports: Mapping[str, object],
    top_bound: set[str],
    has_star: bool,
) -> tuple[set[str], set[str]] | None:
    """(the declared globals ``value`` reads, the dotted names it reads
    through imports), or None when it reads anything else."""
    named: set[str] = set()
    dotted: set[str] = set()
    chained: set[int] = set()  # Name nodes read as the root of a chain
    for node in ast.walk(value):
        if isinstance(node, _NO_NODES):
            return None
        if isinstance(node, ast.Attribute) and id(node) not in chained:
            chain: list[str] = []
            current: ast.expr = node
            while isinstance(current, ast.Attribute):
                chain.append(current.attr)
                chained.add(id(current))
                current = current.value
            if not isinstance(current, ast.Name):
                continue  # an attribute of a value: the value is checked
            chained.add(id(current))
            root = _name_read(
                current.id, declared, local_imports, other_locals, imports, alt_imports, top_bound
            )
            if root is None or (root == "" and has_star):
                return None
            if root.startswith("="):
                named.add(root[1:])  # an attribute of another candidate's value
            elif root == "":
                if current.id not in PURE_BUILTINS and current.id not in MODULE_DUNDERS:
                    return None  # an attribute of a builtin (``str.join``)
            else:
                dotted.add(".".join([root, *reversed(chain)]))
        elif isinstance(node, ast.Name) and id(node) not in chained:
            root = _name_read(
                node.id, declared, local_imports, other_locals, imports, alt_imports, top_bound
            )
            if root is None:
                return None
            if root.startswith("="):
                named.add(root[1:])
            elif root == "":
                if has_star or (node.id not in PURE_BUILTINS and node.id not in MODULE_DUNDERS):
                    return None
            else:
                dotted.add(root)
    return named, dotted


def _name_read(
    name: str,
    declared: set[str],
    local_imports: Mapping[str, str],
    other_locals: set[str],
    imports: Mapping[str, ImportBinding],
    alt_imports: Mapping[str, object],
    top_bound: set[str],
) -> str | None:
    """What reading ``name`` in the writer reads: ``=name`` for a declared
    global (a candidate, checked later), the dotted name an import binds,
    ``""`` for a name nothing in the module binds (a builtin or the module's
    own dunder), None for anything else (a parameter, a local, a definition
    or variable of the module)."""
    if name in declared:
        return f"={name}"
    if name in local_imports:
        return local_imports[name]
    if name in other_locals:
        return None
    if name in imports:
        if name in alt_imports or name in top_bound:
            return None
        return imports[name].dotted
    if name in top_bound:
        return None
    return ""


def decide(
    found: Iterable[tuple[str, str, str, str, Candidate]],
    modules: set[str],
    module_bindings: Mapping[str, set[str]],
    other_writes: set[str],
) -> dict[str, str]:
    """Lazy constants: variable id -> writer id, from each module's
    candidates (``found``: (module, name, variable id, writer id,
    candidate)), the in-scope modules, the names each module binds at its
    top level, and the variables some code other than their candidate
    writer writes (``other_writes``)."""
    ok: dict[tuple[str, str], tuple[str, str, Candidate]] = {
        (module, name): (var, writer, candidate)
        for module, name, var, writer, candidate in found
        if var not in other_writes
        and all(_pure_dotted(d, modules, module_bindings) for d in candidate[2])
    }
    while True:
        bad = [
            key
            for key, (_, writer, candidate) in ok.items()
            if any((key[0], n) not in ok or ok[(key[0], n)][1] != writer for n in candidate[1])
        ]
        if not bad:
            break
        for key in bad:
            del ok[key]
    return {var: writer for var, writer, _ in sorted(ok.values())}


def _pure_dotted(dotted: str, modules: set[str], module_bindings: Mapping[str, set[str]]) -> bool:
    """Whether reading ``dotted`` (through an import) reads something fixed:
    an in-scope module, or its ``__file__``/``__name__``/``__package__``
    when it binds no such name; a fixed value or a pure function outside the
    index."""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in modules:
            rest = parts[i:]
            if not rest:
                return True
            return (
                len(rest) == 1
                and rest[0] in MODULE_DUNDERS
                and rest[0] not in module_bindings.get(prefix, set())
            )
    if parts[0] in {m.split(".")[0] for m in modules}:
        return False  # a name under an in-scope package that is no module of it
    if dotted in PURE_EXTERNAL:
        return True
    if len(parts) == 2 and parts[0] == "re" and parts[1].isupper():
        return True  # a flag (``re.IGNORECASE``)
    return any(".".join(parts[:i]) in EXTERNAL_VALUES for i in range(1, len(parts)))

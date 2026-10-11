"""Which stores a test makes are undone when that test ends (audit round 3,
W26).

A store undone after the test that made it can only be seen by code that
runs during that test, and that test already depends on its own code and
on the value it stores. The indexer leans on that in two places: a patch
undone after the test is no write onto an external module for the lookups
on it (``Indexer._external_lookups``), and puts nothing on an in-scope
module for later code (``SourceIndex.module_writers``); and a ``sys.modules``
entry swapped for one test is no install for good. The rule is the same in
both, decided here from the syntax alone, and when in doubt a store is not
undone. A call's store is undone after the test when the call is:

* a patcher (``patch``, ``mock.patch``, ``patch.object``, ``patch.dict``,
  ``patch.multiple``; not one reached through a pytest-mock fixture, which
  lasts as long as that fixture) that is

  - an item of a ``with``/``async with`` statement inside a function whose
    body does not ``yield``/``yield from`` (outside nested definitions):
    the patch ends with the statement. A body that yields stays patched
    across the yield, into whatever runs meanwhile (the test a generator
    fixture serves, every test under a module- or session-scoped one, the
    caller of a ``@contextmanager``). At module or class level the
    statement is import-time code, no test's;
  - a decorator of a function or a class: ``mock.patch`` patches only
    while the decorated function runs (on a generator function, only while
    the call makes the generator, so its body is not patched at all; on a
    class, while each ``test*`` method runs);

* a method of the pytest ``monkeypatch`` fixture (``monkeypatch.setattr``,
  ``setitem``, ``setenv``, ``delattr``, ...) or a patcher of pytest-mock's
  ``mocker`` (``mocker.patch``, ``mocker.patch.object``, ...): a parameter
  of that name of the test (a function named ``test*``) or fixture (one
  decorated with ``fixture``) the call stands in, not of an enclosing
  function, and not rebound there. Both fixtures are function-scoped, so
  what they store is undone when the test ends (a fixture requesting one
  is function-scoped too). A helper's parameter of that name is whatever
  its callers pass, a ``MonkeyPatch()`` nothing undoes among them;
* a method of a name a ``with MonkeyPatch.context() as mp:`` (or
  ``monkeypatch.context()``) binds, in that statement's body inside a
  function, when the body does not yield and nothing else binds the name.

A store of an attribute (``m.X = v``) is undone after the test when the
test (a function named ``test*``) or a function-scoped fixture (decorated
with ``fixture``, no ``scope`` or ``scope="function"``) saves it and puts it
back, in this shape, every statement at the top level of the function body:

* ``orig = m.X`` before the store, ``orig`` bound nowhere else in it;
* the stores, one after another (``m.X = v``; a value only for the first,
  a name or a constant for those after it, so that once one store is made
  nothing before the restore can raise);
* then either a ``try:`` whose ``finally:`` starts by putting each back
  (``m.X = orig``), or, in a fixture, a ``yield`` of a name or a constant
  (or of nothing) followed at once by the restores: pytest resumes a yield
  fixture after its test however the test ends.

Anything else is not undone: ``patch(...).start()`` (with or without a
``stop``), a patcher kept in a name or an attribute, a ``MonkeyPatch()``
made directly, ``MonkeyPatch.context()`` entered other than by ``with``,
``module_mocker`` and the other wider pytest-mock fixtures, any other
receiver of ``setattr``, ``setitem`` and the like, and the builtins
``setattr``/``delattr``.

Concurrency is outside the rule: a thread or a task started elsewhere that
runs while a test's patch is active sees the patch, as it sees anything
else the test does (the isolation assumption evidence mode makes).
"""

from __future__ import annotations

import ast

from diffcone.indexer.syntax import SCOPE_NODES, _flatten_chain, parent_map

# How a call names what it patches (``patch_target_form``).
PATCH = "patch"  # ``patch("pkg.mod.NAME", ...)``: a dotted string
OBJECT = "object"  # ``patch.object(obj, "NAME", ...)``
DICT = "dict"  # ``patch.dict(d, ...)``: a dict, or a dotted string naming one
MULTIPLE = "multiple"  # ``patch.multiple(obj, NAME=...)``: an object or a dotted string
SETATTR = "setattr"  # ``x.setattr(obj, "NAME", v)``, ``x.setattr("pkg.mod.NAME", v)``, delattr

# ``patch.multiple``'s own options: every other keyword names an attribute.
MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})

# Function-scoped fixtures whose stores pytest undoes when the test ends,
# and the attribute a store goes through (None: any method).
_TEST_FIXTURES: dict[str, str | None] = {"monkeypatch": None, "mocker": "patch"}

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def patch_target_form(parts: list[str]) -> str | None:
    """How a call (by its callee chain) names the target it patches:
    ``PATCH``, ``OBJECT``, ``DICT``, ``MULTIPLE`` (``patch``, ``x.patch``,
    ``x.patch.object``/``dict``/``multiple``) or ``SETATTR`` (a method
    ``x.setattr``/``x.delattr``, as on ``monkeypatch``); None for anything
    else, the builtins ``setattr``/``delattr`` included. A dotted string
    first argument names the target in every form but ``OBJECT``."""
    last = parts[-1]
    if last == "patch":
        return PATCH
    if len(parts) > 1 and parts[-2] == "patch" and last in (OBJECT, DICT, MULTIPLE):
        return last
    if len(parts) > 1 and last in ("setattr", "delattr"):
        return SETATTR
    return None


def callee_parts(func: ast.expr) -> list[str]:
    """A callee's dotted chain, with ``""`` for a head that is no name
    (``MonkeyPatch().setattr`` is ``["", "setattr"]``): what
    ``patch_target_form`` reads when the receiver is a value."""
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    parts.append(func.id if isinstance(func, ast.Name) else "")
    return parts[::-1]


def patched_names(node: ast.Call) -> list[str]:
    """The attribute names a ``patch.multiple`` call patches: its keywords
    other than the options."""
    return [k.arg for k in node.keywords if k.arg is not None and k.arg not in MULTIPLE_OPTIONS]


def dict_patch_keys(node: ast.Call) -> list[str] | None:
    """The keys a ``patch.dict`` call sets: its keywords and the keys of a
    dict display given as its values. None when they are not all known
    (values from elsewhere, ``**kwargs``, a key that is no identifier, a
    ``clear`` other than a literal false)."""
    values = node.args[1] if len(node.args) > 1 else None
    clear = node.args[2] if len(node.args) > 2 else None
    keys: list[str] = []
    for k in node.keywords:
        if k.arg is None:
            return None
        if k.arg == "values":
            values = k.value
        elif k.arg == "clear":
            clear = k.value
        else:
            keys.append(k.arg)
    if clear is not None and not (isinstance(clear, ast.Constant) and not clear.value):
        return None
    if isinstance(values, ast.Dict):
        for key in values.keys:
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            if not key.value.isidentifier():
                return None
            keys.append(key.value)
    elif values is not None and not (isinstance(values, (ast.Tuple, ast.List)) and not values.elts):
        return None
    return keys


def suspends(body: list[ast.stmt]) -> bool:
    """Whether ``body`` may hand control out and come back (``yield``,
    ``yield from``), nested definitions left out."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(node))
    return False


def _patcher(parts: list[str]) -> bool:
    """``patch``, ``x.patch``, ``x.patch.object``/``dict``/``multiple``, not
    through a pytest-mock fixture (``module_mocker.patch``)."""
    form = patch_target_form(parts)
    if form == PATCH:
        head = parts[:-1]
    elif form in (OBJECT, DICT, MULTIPLE):
        head = parts[:-2]
    else:
        return False
    return not any(p.endswith("mocker") for p in head)


def _scope(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    """The function, lambda or class whose body ``node`` is in (a def's
    decorators, defaults and annotations are in the scope around it), or
    None at module level."""
    child, p = node, parents.get(id(node))
    while p is not None:
        if isinstance(p, (*_FUNCTIONS, ast.ClassDef)) and any(child is s for s in p.body):
            return p
        if isinstance(p, ast.Lambda) and child is p.body:
            return p
        child, p = p, parents.get(id(p))
    return None


def _bindings(scope: ast.AST, name: str) -> list[ast.AST]:
    """The nodes that bind ``name`` in ``scope``'s own code (nested
    definitions' bodies left out, comprehensions' targets counted)."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load):
            found.append(n)
        elif isinstance(n, ast.alias) and (n.asname or n.name.split(".")[0]) == name:
            found.append(n)
        elif isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            found.append(n)
        elif isinstance(n, (*_FUNCTIONS, ast.ClassDef)) and n.name == name:
            found.append(n)
        elif isinstance(n, ast.ExceptHandler) and n.name == name:
            found.append(n)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name == name:
            found.append(n)
        elif isinstance(n, ast.MatchMapping) and n.rest == name:
            found.append(n)
        if not isinstance(n, SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(n))
    return found


def _requests_fixtures(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A function pytest calls with fixtures: a test (named ``test*``, as
    pytest's default ``python_functions`` has it) or a fixture (decorated
    with ``fixture``/``pytest.fixture``, called or not)."""
    if function.name.startswith("test"):
        return True
    for decorator in function.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts = _flatten_chain(func)
        if parts and parts[-1] == "fixture":
            return True
    return False


def _fixture_parameter(node: ast.Call, name: str, parents: dict[int, ast.AST]) -> bool:
    """``name`` is a parameter of the test or fixture ``node`` stands in,
    which nothing there rebinds (a helper's parameter of that name is
    whatever its callers pass)."""
    scope = _scope(node, parents)
    if not isinstance(scope, _FUNCTIONS) or not _requests_fixtures(scope):
        return False
    args = scope.args
    params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    return name in params and not _bindings(scope, name)


def _monkeypatch_context(expr: ast.expr) -> bool:
    """``MonkeyPatch.context()``, ``pytest.MonkeyPatch.context()``,
    ``monkeypatch.context()``: a MonkeyPatch undone when the ``with`` ends."""
    if not isinstance(expr, ast.Call) or expr.args or expr.keywords:
        return False
    parts = _flatten_chain(expr.func)
    return (
        bool(parts)
        and len(parts) >= 2
        and parts[-2:]
        in (
            ["MonkeyPatch", "context"],
            ["monkeypatch", "context"],
        )
    )


def _context_bound(node: ast.Call, name: str, parents: dict[int, ast.AST]) -> bool:
    """``name`` is what a ``with MonkeyPatch.context() as name:`` around
    ``node`` binds, in a function, with a body that does not yield and no
    other binding of the name there."""
    scope = _scope(node, parents)
    if not isinstance(scope, _FUNCTIONS):
        return False
    child, p = node, parents.get(id(node))
    while p is not None and p is not scope:
        if isinstance(p, (ast.With, ast.AsyncWith)) and any(child is s for s in p.body):
            for item in p.items:
                var = item.optional_vars
                if isinstance(var, ast.Name) and var.id == name:
                    return (
                        _monkeypatch_context(item.context_expr)
                        and not suspends(p.body)
                        and _bindings(scope, name) == [var]
                    )
        child, p = p, parents.get(id(p))
    return False


def _undone(
    node: ast.Call, parts: list[str], parents: dict[int, ast.AST], contexts: set[str]
) -> bool:
    head = parts[0]
    if len(parts) >= 2 and head in _TEST_FIXTURES:
        through = _TEST_FIXTURES[head]
        if (through is None and len(parts) == 2) or parts[1] == through:
            return _fixture_parameter(node, head, parents)
        return False
    if len(parts) == 2 and head in contexts and _context_bound(node, head, parents):
        return True
    if not _patcher(parts):
        return False
    p = parents.get(id(node))
    if isinstance(p, ast.withitem) and p.context_expr is node:
        statement = parents.get(id(p))
        return (
            isinstance(statement, (ast.With, ast.AsyncWith))
            and not suspends(statement.body)
            and isinstance(_scope(statement, parents), _FUNCTIONS)
        )
    return isinstance(p, (*_FUNCTIONS, ast.ClassDef)) and any(d is node for d in p.decorator_list)


def _function_fixture(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A fixture pytest sets up and tears down around each test: decorated
    with ``fixture``/``pytest.fixture`` with no ``scope`` or
    ``scope="function"``."""
    for decorator in function.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts = _flatten_chain(func)
        if not parts or parts[-1] != "fixture":
            continue
        if isinstance(decorator, ast.Call):
            for kw in decorator.keywords:
                if kw.arg == "scope" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value == "function"
                ):
                    return False
        return True
    return False


def _inert(expr: ast.expr | None) -> bool:
    """A value whose evaluation cannot raise: nothing, a name, a constant."""
    return expr is None or isinstance(expr, (ast.Name, ast.Constant))


def _attribute_store(stmt: ast.stmt) -> ast.Attribute | None:
    """The target of ``a.b.X = v`` (a dotted name), else None."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if isinstance(target, ast.Attribute) and _flatten_chain(target) is not None:
            return target
    return None


def _restores(stmts: list[ast.stmt], saved: dict[str, str]) -> dict[str, ast.Attribute]:
    """The leading ``m.X = orig`` statements: each dotted name put back, with
    the store target putting it back."""
    found: dict[str, ast.Attribute] = {}
    for stmt in stmts:
        target = _attribute_store(stmt)
        if target is None:
            break
        text = ast.unparse(target)
        value = stmt.value if isinstance(stmt, ast.Assign) else None
        if not (isinstance(value, ast.Name) and saved.get(text) == value.id):
            break
        found[text] = target
    return found


def _restored_stores(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """The ``id`` of each attribute store target in ``function`` that it puts
    back before its test ends (the save/store/restore shape in this module's
    docstring)."""
    fixture = _function_fixture(function)
    if not (fixture or function.name.startswith("test")):
        return set()
    body = function.body
    saved: dict[str, str] = {}  # dotted name -> the local holding its old value
    found: set[int] = set()
    i = 0
    while i < len(body):
        stmt = body[i]
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Attribute)
            and _flatten_chain(stmt.value) is not None
            and len(_bindings(function, stmt.targets[0].id)) == 1
        ):
            saved[ast.unparse(stmt.value)] = stmt.targets[0].id
            i += 1
            continue
        stores: list[ast.Attribute] = []
        j = i
        while j < len(body):
            target = _attribute_store(body[j])
            if target is None or ast.unparse(target) not in saved:
                break
            statement = body[j]
            if stores and isinstance(statement, ast.Assign) and not _inert(statement.value):
                break
            stores.append(target)
            j += 1
        if not stores:
            i += 1
            continue
        texts = {ast.unparse(t) for t in stores}
        after = body[j] if j < len(body) else None
        restored: dict[str, ast.Attribute] = {}
        if isinstance(after, ast.Try) and after.finalbody:
            restored = _restores(after.finalbody, saved)
        elif (
            fixture
            and isinstance(after, ast.Expr)
            and isinstance(after.value, ast.Yield)
            and _inert(after.value.value)
        ):
            restored = _restores(body[j + 1 :], saved)
        if texts <= restored.keys():
            # The restores too: they put back what was there.
            found.update(id(t) for t in (*stores, *restored.values()))
        i = j
    return found


def undone_calls(
    root: ast.AST,
    nodes: list[ast.AST] | None = None,
    parents: dict[int, ast.AST] | None = None,
) -> frozenset[int]:
    """The ``id`` of every call under ``root`` whose store is undone when
    the test that made it ends, and of every attribute store target a test
    or fixture puts back before then (the rules in this module's
    docstring); ``nodes`` and ``parents`` are ``root``'s nodes and parent
    map when the caller has them."""
    nodes = nodes if nodes is not None else list(ast.walk(root))
    # The names a ``with MonkeyPatch.context() as name`` binds somewhere.
    contexts = {
        item.optional_vars.id
        for node in nodes
        if isinstance(node, (ast.With, ast.AsyncWith))
        for item in node.items
        if isinstance(item.optional_vars, ast.Name) and _monkeypatch_context(item.context_expr)
    }
    found: set[int] = set()
    for node in nodes:
        if isinstance(node, _FUNCTIONS):
            found |= _restored_stores(node)
        if not isinstance(node, ast.Call):
            continue
        parts = _flatten_chain(node.func)
        if not parts:
            continue
        if parents is None:
            parents = parent_map(nodes)
        if _undone(node, parts, parents, contexts):
            found.add(id(node))
    return frozenset(found)

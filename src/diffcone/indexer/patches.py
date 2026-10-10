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

# A patcher's method chain after ``patch`` (``patch.object``).
_PATCH_FORMS = (["patch", "object"], ["patch", "dict"], ["patch", "multiple"])

# Function-scoped fixtures whose stores pytest undoes when the test ends,
# and the attribute a store goes through (None: any method).
_TEST_FIXTURES: dict[str, str | None] = {"monkeypatch": None, "mocker": "patch"}

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _chain(expr: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return parts[::-1]


def _parents(nodes: list[ast.AST]) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in nodes:
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def suspends(body: list[ast.stmt]) -> bool:
    """Whether ``body`` may hand control out and come back (``yield``,
    ``yield from``), nested definitions left out."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return False


def _patcher(parts: list[str]) -> bool:
    """``patch``, ``x.patch``, ``x.patch.object``/``dict``/``multiple``, not
    through a pytest-mock fixture (``module_mocker.patch``)."""
    if parts[-1] == "patch":
        head = parts[:-1]
    elif parts[-2:] in _PATCH_FORMS:
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
        if not isinstance(n, _SCOPES):
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
        parts = _chain(func)
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
    parts = _chain(expr.func)
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


def undone_calls(
    root: ast.AST,
    nodes: list[ast.AST] | None = None,
    parents: dict[int, ast.AST] | None = None,
) -> frozenset[int]:
    """The ``id`` of every call under ``root`` whose store is undone when
    the test that made it ends (the rule in this module's docstring);
    ``nodes`` and ``parents`` are ``root``'s nodes and parent map when the
    caller has them."""
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
        if not isinstance(node, ast.Call):
            continue
        parts = _chain(node.func)
        if not parts:
            continue
        if parents is None:
            parents = _parents(nodes)
        if _undone(node, parts, parents, contexts):
            found.add(id(node))
    return frozenset(found)

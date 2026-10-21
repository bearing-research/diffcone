"""Project code a symbol runs by naming it in a string.

A test can run a script or a module of the project as a subprocess
(``[sys.executable, "scripts/gen.py"]``, ``-m pkg.tool``, ``python -c
"import pkg"``), run text as code in its own process (``exec``, ``eval``,
``timeit``, pytest's ``pytester``), run a test file through a test runner,
or load a file by path; no import edge says so. Pass 2 records (cacheable
per module):

* a string that names a ``.py`` file: a literal, or the last literal piece of
  a path built from literals (``os.path.join(ROOT, "scripts", "gen.py")``,
  ``Path(__file__).parent / "fixtures" / "run.py"``, ``"scripts/" +
  "gen.py"``, an f-string), with the directories the literals name, or a
  module's ``__file__`` in a command. Docstrings and other bare string
  statements do not count, nor does a glob pattern (``"*.py"``): what it
  matches is run through a name, which the command rule below sees.
  ``apply_scripts`` matches it, after pass 2, against the files under the
  roots by path suffix (the directory the path starts from is not known): a
  module's file is an import of it (running it runs its import-time code,
  and what that calls); a ``.py`` file no module name maps to
  (``scripts/gen-data.py``) is code the index does not read, so the symbol
  may import anything (an unbounded dynamic reference), and a change to the
  file reaches it (``SourceIndex.script_refs``); a file ``[index] exclude``
  keeps out is not read at all: an unknown dependency
  (``Resolver.excluded_import``), as is ``-m`` of an excluded module;
* ``-m NAME`` (in a string, or as two items of a command list) naming an
  in-scope module: an import of it (and of ``NAME.__main__`` for a
  package);
* ``-c CODE`` whose code is written out (``code_texts``: a literal, a local
  bound once to one, a module-level constant, ``textwrap.dedent`` of one,
  ``compile()`` of one, a literal template filled by ``.format``, ``%`` or
  an f-string with literal values, or with the test's own parameter that
  ``@pytest.mark.parametrize`` gives literal values, one text per value):
  the modules it imports and the names it
  reaches through them, unless it imports or runs code by a name or path
  it computes or changes where its imports look (``sys.path``,
  ``sys.meta_path``, ``sys.modules`` stored into, extended or reassigned;
  ``site.addsitedir``, ``os.chdir``), or a template field nothing literal
  fills is left in it: then it can run anything;
* a test file a test runner is given (``-m pytest tests/x.py``, ``-m
  unittest tests.cases``, ``pytest.main([...])``, ``unittest.main(module=
  ...)``, ``loadTestsFromName``, ``pytester.copy_example``): the file's
  tests, fixtures and conftests run, so the symbol depends on the module
  as a whole (every symbol in it, and for pytest on each ``conftest.py``
  from its directory outward, whole too: ``whole_module``), not only on
  its import-time code. A directory given to ``pytester.copy_example``
  runs every ``.py`` file under it, each so; any other directory, a bare
  word the runner would collect from, ``unittest discover`` or a
  non-literal is unbounded.

A command whose interpreter the analysis finds (``_interpreter_index``: the
first item that is ``sys.executable`` or a ``python``/``python3`` literal,
after launcher words, ``env``, ``VAR=x``, ``uv run``, ``poetry run``,
``timeout 30`` and their options, or a name or call that is no literal when
``-m``, ``-c`` or a ``.py`` path follows it; the arguments of
``asyncio.create_subprocess_exec``, ``os.execl`` and ``pytester.run`` are a
command too; an f-string or a concatenation starting with
``sys.executable``, a string a shell-running call is given that starts with
``python``, each after such prefixes) and whose program is not one of those
(a script path or a ``-m`` name from elsewhere, ``-c`` code built at run
time, code read from standard input, ``*args``) can run any code of the
project: an unbounded dynamic reference. A ``-m`` name that is a parameter
is bounded by the literal names the call sites pass, as an
``import_module`` of one is. ``-m`` of a code runner (``coverage``,
``pytest``, ``runpy``, ...) runs what follows it in turn; a directory given
to one runs whatever it holds.

A module loaded from a file by path (``spec_from_file_location``,
``SourceFileLoader``) is matched the same way, by the reference walker
(``_ReferenceCollector._file_loader``).

Not modelled: a console script the project installs (``["tool", ...]``), a
file outside the source roots, a command handed over whole from elsewhere
with no interpreter in sight, the environment a program is started with
(``PYTHONPATH`` sending its imports elsewhere), a test reading a source file
as text.
"""

from __future__ import annotations

import ast
import re
import shlex
import string
import textwrap
from collections.abc import Callable
from typing import TYPE_CHECKING

from diffcone.indexer.process import STATE_MUTATORS
from diffcone.indexer.scopes import ModuleNode, Resolved
from diffcone.indexer.syntax import _digest, _flatten_chain
from diffcone.model import (
    RUNS_PROGRAM,
    RUNS_SCRIPT,
    Edge,
    EdgeKind,
    SymbolKind,
    UnresolvedKind,
    UnresolvedReference,
)
from diffcone.snapshot import excluded_files, module_name_for

if TYPE_CHECKING:
    from diffcone.indexer.references import _ReferenceCollector
    from diffcone.indexer.resolver import Resolver

# The detail prefixes of the dynamic references this module records,
# RUNS_PROGRAM and RUNS_SCRIPT, live in diffcone.model.

_PY_PATH = re.compile(r"""[^\s'"=:,;()<>|&]*\.pyw?(?![\w])""")
_DASH_M = re.compile(r"(?:^|\s)-m\s*([A-Za-z_][\w.]*)")
_PYTHON = re.compile(r"^(?:.*[/\\])?python(?:\d+(?:\.\d+)*)?(?:\.exe)?$", re.IGNORECASE)
_GLOB = re.compile(r"[*?\[]")
# Path builders whose arguments are joined with separators.
_JOINERS = frozenset(
    {"join", "joinpath", "Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath"}
)
# Wrappers that hand on the path or interpreter they are given.
_WRAPPERS = frozenset(
    {"str", "fspath", "abspath", "realpath", "normpath", "Path", "resolve", "absolute", "quote"}
)
# Interpreter options that take the next item as their argument.
_OPTIONS_WITH_ARGUMENT = frozenset({"-W", "-X", "--check-hash-based-pycs"})
# Options after which nothing runs.
_NOTHING_RUNS = frozenset({"--version", "-V", "-VV", "-h", "--help", "-?"})
# Modules that run the program named after them.
CODE_RUNNERS = frozenset(
    {
        "coverage",
        "pytest",
        "py.test",
        "runpy",
        "unittest",
        "doctest",
        "cProfile",
        "profile",
        "pdb",
        "trace",
        "timeit",
        "IPython",
        "pyinstrument",
        "memray",
        "scalene",
    }
)
# Runners that run the tests of the files and modules they are given.
TEST_RUNNERS = frozenset({"pytest", "py.test", "unittest", "doctest"})
# Runners whose ``-m NAME`` runs a module (the test runners' ``-m`` is a
# marker expression or a flag of their own).
_MODULE_RUNNING_RUNNERS = frozenset(
    {
        "coverage",
        "runpy",
        "cProfile",
        "profile",
        "pdb",
        "trace",
        "pyinstrument",
        "memray",
        "scalene",
    }
)
# Short options of the test runners that take the next item as their
# argument (pytest's, ``unittest``'s and ``doctest``'s together).
_RUNNER_OPTIONS_WITH_ARGUMENT = frozenset(
    {"-k", "-m", "-p", "-o", "-W", "-c", "-n", "-r", "-t", "-C"}
)
# Long options of the code runners and their common plugins that take an
# argument as the next word (``--tb short``). After any other ``--long``
# option a bare word is read as an argument of the runner's own, never
# taken for the option's: ``--verbose docs`` collects ``docs``.
_RUNNER_LONG_OPTIONS_WITH_ARGUMENT = frozenset(
    {
        # pytest
        "--tb",
        "--show-capture",
        "--rootdir",
        "--basetemp",
        "--confcutdir",
        "--config-file",
        "--override-ini",
        "--junitxml",
        "--junit-xml",
        "--junit-prefix",
        "--durations",
        "--durations-min",
        "--maxfail",
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--capture",
        "--color",
        "--code-highlight",
        "--import-mode",
        "--pastebin",
        "--pdbcls",
        "--doctest-glob",
        "--doctest-report",
        "--log-level",
        "--log-format",
        "--log-date-format",
        "--log-cli-level",
        "--log-cli-format",
        "--log-cli-date-format",
        "--log-file",
        "--log-file-mode",
        "--log-file-level",
        "--log-file-format",
        "--log-file-date-format",
        "--log-auto-indent",
        "--log-disable",
        "--report-log",
        # pytest-xdist, pytest-cov, pytest-timeout, pytest-randomly and others
        "--dist",
        "--numprocesses",
        "--maxprocesses",
        "--max-worker-restart",
        "--tx",
        "--cov",
        "--cov-report",
        "--cov-config",
        "--cov-fail-under",
        "--cov-context",
        "--timeout",
        "--timeout-method",
        "--randomly-seed",
        "--random-order-seed",
        "--random-order-bucket",
        "--count",
        "--repeat-scope",
        "--reruns",
        "--reruns-delay",
        "--only-rerun",
        "--asyncio-mode",
        "--hypothesis-profile",
        "--hypothesis-seed",
        "--html",
        "--json-report-file",
        # unittest
        "--pattern",
        "--start-directory",
        "--top-level-directory",
        # coverage
        "--rcfile",
        "--source",
        "--include",
        "--omit",
        "--data-file",
        "--context",
        "--concurrency",
    }
)
# ``coverage`` subcommands other than ``run`` run no code of the project.
_COVERAGE_INERT = frozenset(
    {"report", "xml", "html", "json", "lcov", "combine", "erase", "debug", "annotate"}
)
# Calls whose argument is code text: ``textwrap.dedent("""...""")``.
_TEXT_WRAPPERS = frozenset({"dedent", "cleandoc", "strip", "lstrip", "rstrip"})
# Words that may precede the interpreter in a command: launchers that run
# the program after their own arguments (``env X=1 python``, ``uv run
# python``, ``timeout 30 python``), and what they are given.
_LAUNCHERS = frozenset(
    {
        "env",
        "nice",
        "timeout",
        "nohup",
        "stdbuf",
        "taskset",
        "time",
        "sudo",
        "doas",
        "xvfb-run",
        "mpirun",
        "mpiexec",
        "srun",
        "uv",
        "poetry",
        "pipenv",
        "conda",
        "hatch",
        "pdm",
        "rye",
        "tox",
        "pyenv",
        "run",
        "exec",
        "--",
    }
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
# Calls that start a process from their positional arguments rather than a
# list (``create_subprocess_exec(sys.executable, "-m", "x")``, ``os.execl``,
# ``os.spawnl(mode, path, ...)``, ``pytester.run(...)``).
VARARGS_PROCESS_CALLS = frozenset(
    {
        "create_subprocess_exec",
        "execl",
        "execlp",
        "execle",
        "execlpe",
        "spawnl",
        "spawnlp",
        "spawnle",
        "spawnlpe",
        "run",
    }
)
# What stands for a template field nothing literal fills (``code_texts``).
HOLE_NAME = "__diffcone_hole__"
# The detail of a symbol running a file's tests through a runner.
RUNS_TESTS = "run as tests"


# --------------------------------------------------------------------------- pass 2


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _normalise(path: str) -> str | None:
    """A path as forward-slash pieces from the last ``..`` on (what lies
    above it is not known); None when no file name is left."""
    pieces = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in pieces:
        pieces = pieces[len(pieces) - pieces[::-1].index("..") :]
    if not pieces or pieces[-1] in (".py", ".pyw"):
        return None
    return "/".join(pieces)


def _prefix(collector: _ReferenceCollector, node: ast.Constant) -> str:
    """The literal directories a path built around ``node`` puts before it
    (``Path(ROOT) / "scripts" / "gen.py"`` -> ``scripts/``)."""
    parent = collector._parent(node)
    pieces: list[str] = []
    if isinstance(parent, ast.BinOp) and parent.right is node:
        left = parent.left
        if isinstance(parent.op, ast.Add):
            return _string(left) or ""
        if isinstance(parent.op, ast.Div):
            while isinstance(left, ast.BinOp) and isinstance(left.op, ast.Div):
                piece = _string(left.right)
                if piece is None:
                    return "/".join(pieces)
                pieces.insert(0, piece)
                left = left.left
            if (piece := _string(left)) is not None:
                pieces.insert(0, piece)
            elif isinstance(left, ast.Call) and (path := _call_path(left)) is not None:
                pieces.insert(0, path)
    elif isinstance(parent, ast.Call) and node in parent.args:
        name = parent.func.attr if isinstance(parent.func, ast.Attribute) else ""
        if isinstance(parent.func, ast.Name):
            name = parent.func.id
        if name in _JOINERS:
            for arg in reversed(parent.args[: parent.args.index(node)]):
                piece = _string(arg)
                if piece is None:
                    break
                pieces.insert(0, piece)
    return "/".join(pieces) + "/" if pieces else ""


def _call_path(call: ast.Call) -> str | None:
    """``Path("scripts")``: the literal path a constructor is given."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in _JOINERS:
        return None
    pieces = [_string(a) for a in call.args]
    if not pieces or any(p is None for p in pieces):
        return None
    return "/".join(p for p in pieces if p is not None)


def observe_string(collector: _ReferenceCollector, node: ast.Constant) -> None:
    """A string that names a ``.py`` file or a ``-m`` module."""
    value = node.value
    if not isinstance(value, str) or isinstance(collector._parent(node), ast.Expr):
        return
    if ".py" in value:
        whole = value.strip()
        for token in _PY_PATH.findall(value):
            if _GLOB.search(token):
                continue  # what a pattern matches is run through a name: see observe_command
            text = (_prefix(collector, node) + token) if token == whole else token
            path = _normalise(text)
            if path is not None:
                collector.indexer.out.script_paths.add((collector.source, path))
    if "-m" in value:
        for name in _DASH_M.findall(value):
            _module_run(collector.indexer, collector.source, name.rstrip("."))


def _module_run(indexer: Resolver, source: str, name: str) -> bool:
    """``python -m name``: an import of an in-scope module (and its
    ``__main__`` for a package). False when no module of ours is named."""
    parts = [p for p in name.split(".") if p]
    if not parts or not all(p.isidentifier() for p in parts):
        return False
    if excluded_files(indexer.excluded_modules, ".".join(parts)):
        # A module ``[index] exclude`` keeps out: run unread (audit round 6, T2).
        indexer._module_import_edge(source, ".".join(parts))
        return True
    node = indexer.resolve_dotted(parts)
    if not isinstance(node, ModuleNode) or node.module != ".".join(parts):
        return False
    indexer._module_import_edge(source, node.module)
    main = f"{node.module}.__main__"
    if main in indexer.scopes:
        indexer._module_import_edge(source, main)
    return True


def module_symbols(indexer: Resolver, module: str) -> list[str]:
    """Every symbol defined in ``module``: its members, and the members of
    its classes, nested ones included."""
    scope = indexer.scopes.get(module)
    if scope is None:
        return []
    found: list[str] = []
    stack = sorted(scope.members.values())
    while stack:
        symbol = stack.pop()
        if symbol in found:
            continue
        found.append(symbol)
        cscope = indexer.class_scopes.get(symbol)
        if cscope is not None:
            stack.extend(sorted(cscope.members.values()))
    return found


def whole_module(indexer: Resolver, source: str, module: str, detail: str = RUNS_TESTS) -> bool:
    """``source`` runs ``module`` as a whole (a test runner collects it, a
    pytest plugin is loaded into a session): an import of it, and a
    reference to every symbol in it. False when the module is not in
    scope."""
    if module not in indexer.scopes:
        if not excluded_files(indexer.excluded_modules, module):
            return False
        # Excluded by ``[index] exclude``: run unread (audit round 6, T2).
        indexer._module_import_edge(source, module)
        return True
    indexer._module_import_edge(source, module)
    for symbol in module_symbols(indexer, module):
        if symbol != source:
            indexer.out.edges.add(Edge(source, symbol, EdgeKind.REFERENCES, detail))
    return True


def _unwrap(expr: ast.expr) -> ast.expr:
    """``str(x)``, ``os.fspath(x)``, ``Path(x).resolve()``: ``x``."""
    while isinstance(expr, ast.Call):
        func = expr.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _WRAPPERS:
            break
        if expr.args and len(expr.args) == 1:
            expr = expr.args[0]
        elif not expr.args and isinstance(func, ast.Attribute):
            expr = func.value
        else:
            break
    return expr


def _is_interpreter(collector: _ReferenceCollector, expr: ast.expr) -> bool:
    expr = _unwrap(expr)
    text = _string(expr)
    if text is not None:
        first = text.split()[0] if text.split() else ""
        return bool(_PYTHON.match(first))
    parts = _flatten_chain(expr)
    return parts is not None and "sys.executable" in collector._canonical_names(parts)


def _launcher_word(text: str) -> bool:
    """A word that may stand before the interpreter: a launcher (by its
    last path piece), one of its options, ``VAR=value``, a number
    (``timeout 30``)."""
    word = text.replace("\\", "/").rpartition("/")[2]
    return (
        word in _LAUNCHERS
        or text.startswith("-")
        or _ASSIGNMENT.match(text) is not None
        or _NUMBER.match(text) is not None
    )


def _runs_python(text: str | None) -> bool:
    """Whether a literal that follows an item says the item is the
    interpreter: ``-m``, ``-c`` (joined or not) or a script path."""
    if text is None:
        return False
    text = text.rstrip()
    return (
        text in ("-m", "-c")
        or ((text.startswith("-m") or text.startswith("-c")) and len(text) > 2)
        or text.endswith((".py", ".pyw"))
    )


def _interpreter_index(collector: _ReferenceCollector, items: list[ast.expr]) -> int | None:
    """Where the interpreter stands in a command, if it does: the first
    item that is one, after launcher words, or a value that is no literal
    (``os.environ.get("PYTHON", sys.executable)``, a launcher's path) when
    ``-m``, ``-c`` or a script path follows it."""
    for i, item in enumerate(items):
        if isinstance(item, ast.Starred):
            continue  # what it expands to is not known; the interpreter may follow
        if _is_interpreter(collector, item):
            return i
        text = _string(_unwrap(item))
        following = items[i + 1] if i + 1 < len(items) else None
        if text is None:
            if _runs_python(_string(following)):
                return i
            if following is None or (
                (next_text := _string(_unwrap(following))) is not None
                and not _launcher_word(next_text)
            ):
                return None
            continue  # a launcher held in a name, maybe: what follows decides
        if not _launcher_word(text):
            return None
    return None


def _names_py_file(expr: ast.expr) -> bool:
    """A path whose last literal piece names a ``.py`` file (recorded by
    observe_string when the walk reaches it)."""
    last: ast.AST = expr
    while True:
        if isinstance(last, ast.BinOp):
            last = last.right
        elif isinstance(last, ast.Call) and last.args:
            last = last.args[-1]
        elif isinstance(last, ast.JoinedStr) and last.values:
            last = last.values[-1]
        else:
            break
    text = _string(last)
    return text is not None and text.rstrip().endswith((".py", ".pyw"))


def _module_file(collector: _ReferenceCollector, expr: ast.expr) -> bool:
    """``mod.__file__`` (``__file__``: this module): the file of a module the
    code holds, run as a script. Running it runs its import-time code, which
    holding it (an import, static or by name) already depends on; an
    in-scope module named here is an import of it. True when ``expr`` is
    one."""
    if (
        isinstance(expr, ast.Name)
        and expr.id == "__file__"
        and not collector._is_shadowed("__file__")
    ):
        collector.indexer.out.script_paths.add((collector.source, collector.scope.module.path))
        return True
    parts = _flatten_chain(expr)
    if parts is None or len(parts) < 2 or parts[-1] != "__file__":
        return False
    # Each module the name may be: on a name bound several ways (an import
    # in ``try`` and another in ``except``), any of them is the one run.
    indexer = collector.indexer
    node = indexer.resolve_chain(parts[:-1], collector.scope)
    modules: list[str] = []
    if isinstance(node, Resolved):
        symbol = indexer.index.symbols.get(node.symbol)
        if symbol is not None and symbol.kind == SymbolKind.MODULE and not node.detail:
            modules.append(symbol.id)
        modules += [*node.also, *node.alternatives]
    elif isinstance(node, ModuleNode):
        modules += [node.module, *node.alternatives]
    for module in modules:
        scope = indexer.scopes.get(module)
        if scope is not None:
            indexer.out.script_paths.add((collector.source, scope.path))
    return True


def _last_piece(expr: ast.expr) -> ast.expr:
    """The last piece of a path built with ``/``, ``+`` or a joining call."""
    while True:
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Div, ast.Add)):
            expr = expr.right
        elif (
            isinstance(expr, ast.Call)
            and expr.args
            and (expr.func.attr if isinstance(expr.func, ast.Attribute) else "")
            in ("join", "joinpath")
        ):
            expr = expr.args[-1]
        else:
            return expr


def _script_known(collector: _ReferenceCollector, expr: ast.expr, depth: int = 0) -> bool:
    """A script path the analysis can name: a ``.py`` literal (or the last
    literal piece of a path), a module's ``__file__``, a local bound once to
    one of those, or a parameter (or a path ending in one), bounded by the
    literal paths the call sites pass (``run_python("scripts/gen.py")``)."""
    expr = _unwrap(expr)
    if _module_file(collector, expr) or _names_py_file(expr):
        return True
    last = _unwrap(_last_piece(expr))
    if isinstance(last, ast.Name):
        if collector._param_dynamic(last, "script", None, f"{RUNS_PROGRAM}: an import of anything"):
            return True
        source = collector._local_source(last.id)
        if source is not None and depth < 3:
            return _script_known(collector, source, depth + 1)
    return False


def script_value(indexer: Resolver, source: str, value: str, detail: str) -> None:
    """A literal a call site passes for a script parameter (_script_known)."""
    path = _normalise(value) if value.rstrip().endswith((".py", ".pyw")) else None
    if path is None:
        indexer.out.unresolved.add(UnresolvedReference(source, UnresolvedKind.DYNAMIC, "", detail))
    else:
        indexer.out.script_paths.add((source, path))


def code_value(
    indexer: Resolver, source: str, value: str, detail: str, module: str | None = None
) -> None:
    """A literal a call site passes for a parameter whose text is run as
    code (``exec(code)`` with ``code`` a parameter): what it imports and
    reaches (reading ``module``'s namespace when it runs there), or an
    unbounded dynamic reference (``code_edges``)."""
    if not code_edges(indexer, source, value, module):
        indexer.out.unresolved.add(UnresolvedReference(source, UnresolvedKind.DYNAMIC, "", detail))


def unwrap_text(collector: _ReferenceCollector, expr: ast.expr) -> ast.expr:
    """``textwrap.dedent(t)``, ``t.strip()``, ``compile(t, ...)``, a local
    bound once: the text expression behind them."""
    seen = 0
    while seen < 6:
        seen += 1
        if isinstance(expr, ast.Call):
            func = expr.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _TEXT_WRAPPERS:
                inner = expr.args[0] if expr.args else None
                if inner is None and isinstance(func, ast.Attribute):
                    inner = func.value
                if inner is None:
                    return expr
                expr = inner
                continue
            if (
                isinstance(func, ast.Name)
                and func.id == "compile"
                and expr.args
                and not collector._is_shadowed("compile")
            ):
                expr = expr.args[0]
                continue
            return expr
        if isinstance(expr, ast.Name) and expr.id not in collector.scope.params:
            source = collector._local_source(expr.id)
            if source is not None:
                expr = source
                continue
        return expr
    return expr


def _single(values: tuple[str, ...] | None) -> str | None:
    return values[0] if values is not None and len(values) == 1 else None


def _convert(value: object, conversion: str | None) -> str:
    if conversion == "r":
        return repr(value)
    if conversion == "a":
        return ascii(value)
    return str(value)


_PERCENT = re.compile(r"%(?:\((\w+)\))?([-#0 +]*\d*(?:\.\d+)?)([a-zA-Z%])")
# How many texts a template's parametrized fields may make (code_texts).
_MAX_PARAMETRIZED_TEXTS = 64


def code_texts(
    collector: _ReferenceCollector, expr: ast.expr, holes: list[ast.expr] | None = None
) -> tuple[str, ...] | None:
    """The code text ``expr`` is, when it is written out: a literal, a
    local bound once to one, a module-level constant, ``textwrap.dedent``,
    ``strip`` or ``compile`` of one, a concatenation of such, an f-string or
    a literal template's ``.format``/``%`` whose fields literal values fill
    (``HOLE_NAME`` stands in for a field nothing literal fills, and the
    expression behind it is appended to ``holes``: the code that runs is
    not known there). None when the text is not written out.

    A field the test's own parameter fills, which ``@pytest.mark.parametrize``
    on the test gives literal values (``Scope.param_values``), is no hole:
    the text is each one the values make, filled in as Python would format
    them (``str``, ``repr`` for ``!r`` and ``%r``, a literal format spec);
    likewise a text such a parameter is, or that it is a piece of
    (``"import " + mod``). Any field left a hole leaves the template as it
    was."""
    own: list[ast.expr] = []
    texts = _code_texts(collector, expr, own, {})
    values = collector.scope.param_values
    if values:
        filled = _parametrized_texts(collector, expr, texts, own, values)
        if filled is not None:
            return filled
    if holes is not None:
        holes.extend(own)
    return texts


def _parametrized_texts(
    collector: _ReferenceCollector,
    expr: ast.expr,
    texts: tuple[str, ...] | None,
    holes: list[ast.expr],
    values: dict[str, tuple[object, ...]],
) -> tuple[str, ...] | None:
    """``code_texts`` with each hole a parametrized parameter fills filled
    by each of its values; None when some hole is anything else."""
    if texts is None:
        # Not written out without the values (the parameter is the text, or
        # a piece of a concatenation): try the parameters it names.
        written = ast.walk(unwrap_text(collector, expr))
        names = list(dict.fromkeys(n.id for n in written if isinstance(n, ast.Name)))
        names = [name for name in names if name in values]
    else:
        names = list(dict.fromkeys(h.id if isinstance(h, ast.Name) else "" for h in holes))
    if not names or any(name not in values or name in collector.scope.rebound for name in names):
        return None
    combinations: list[dict[str, object]] = [{}]
    for name in names:
        combinations = [{**c, name: v} for c in combinations for v in values[name]]
        if len(combinations) > _MAX_PARAMETRIZED_TEXTS:
            return None
    out: list[str] = []
    for bound in combinations:
        left: list[ast.expr] = []
        found = _code_texts(collector, expr, left, bound)
        if found is None or left or any(HOLE_NAME in text for text in found):
            return None
        out.extend(found)
    return tuple(dict.fromkeys(out))


def _code_texts(
    collector: _ReferenceCollector,
    expr: ast.expr,
    holes: list[ast.expr],
    bound: dict[str, object],
) -> tuple[str, ...] | None:
    """code_texts, with the parameters ``bound`` names standing for the
    value it gives each."""
    expr = unwrap_text(collector, expr)
    if isinstance(expr, ast.Name) and expr.id in bound:
        value = bound[expr.id]
        return (value,) if isinstance(value, str) else None
    direct = collector.scope.string_candidates(expr)
    if direct is not None:
        return direct

    def piece(
        value: ast.expr | None, conversion: str | None = None, spec: str | None = None
    ) -> str:
        found: object
        if isinstance(value, ast.Name) and value.id in bound:
            found = bound[value.id]
        else:
            found = (
                _single(_code_texts(collector, value, holes, bound)) if value is not None else None
            )
            if found is None:
                if value is not None:
                    holes.append(value)
                return HOLE_NAME
        text = _convert(found, conversion)
        if not spec:
            return text
        try:
            return format(text if conversion else found, spec)
        except (ValueError, TypeError):
            return HOLE_NAME

    if isinstance(expr, ast.JoinedStr):
        pieces: list[str] = []
        for value in expr.values:
            if isinstance(value, ast.Constant):
                pieces.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                conversion = chr(value.conversion) if value.conversion != -1 else None
                spec = value.format_spec
                if spec is None:
                    pieces.append(piece(value.value, conversion))
                elif isinstance(spec, ast.JoinedStr) and all(
                    isinstance(v, ast.Constant) for v in spec.values
                ):
                    written = "".join(
                        str(v.value) for v in spec.values if isinstance(v, ast.Constant)
                    )
                    pieces.append(piece(value.value, conversion, written))
                elif isinstance(value.value, ast.Name) and value.value.id in bound:
                    pieces.append(HOLE_NAME)  # a computed spec: not known how it formats
                else:
                    pieces.append(piece(value.value))
            else:
                pieces.append(piece(None))
        return ("".join(pieces),)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _single(_code_texts(collector, expr.left, holes, bound))
        right = _single(_code_texts(collector, expr.right, holes, bound))
        if left is None or right is None:
            return None
        return (left + right,)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod):
        template = _single(_code_texts(collector, expr.left, holes, bound))
        if template is None:
            return None
        if isinstance(expr.right, ast.Name) and not isinstance(bound.get(expr.right.id, ""), str):
            return None  # ``"%s" % values``: a tuple is the arguments, not one
        values: list[ast.expr] = (
            list(expr.right.elts) if isinstance(expr.right, ast.Tuple) else [expr.right]
        )
        named: dict[str, ast.expr] = {}
        if isinstance(expr.right, ast.Dict):
            for key, value in zip(expr.right.keys, expr.right.values, strict=True):
                name = _string(key)
                if name is not None:
                    named[name] = value
        position = 0

        def fill(match: re.Match[str]) -> str:
            nonlocal position
            name, spec, kind = match.group(1), match.group(2), match.group(3)
            if kind == "%":
                return "%"
            if kind not in "sra" or spec:
                return HOLE_NAME
            if name is not None:
                value = named.get(name)
            else:
                value = values[position] if position < len(values) else None
                position += 1
            return piece(value, kind if kind != "s" else None)

        return (_PERCENT.sub(fill, template),)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "format"
    ):
        template = _single(_code_texts(collector, expr.func.value, holes, bound))
        if template is None:
            return None
        return (_fill_template(template, expr, piece),)
    return None


def _fill_template(
    template: str, call: ast.Call, piece: Callable[[ast.expr | None, str | None], str]
) -> str:
    """``template.format(...)`` with each field replaced by the one literal
    the argument is, or ``HOLE_NAME``."""
    positional = [a for a in call.args if not isinstance(a, ast.Starred)]
    keywords = {k.arg: k.value for k in call.keywords if k.arg is not None}
    out: list[str] = []
    auto = 0
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return HOLE_NAME
    for literal, field, spec, conversion in parsed:
        out.append(literal)
        if field is None:
            continue
        value: ast.expr | None = None
        if field == "":
            value = positional[auto] if auto < len(positional) else None
            auto += 1
        elif field.isdigit():
            value = positional[int(field)] if int(field) < len(positional) else None
        elif field.isidentifier():
            value = keywords.get(field)
        out.append(HOLE_NAME if spec else piece(value, conversion))
    return "".join(out)


def _run_code(collector: _ReferenceCollector, code: str) -> bool:
    """``python -c code``: what it imports, and the names it reaches through
    those imports. False when that cannot be bounded (see code_edges)."""
    return code_edges(collector.indexer, collector.source, code)


# Calls in a program that import or run code by a name or path the program
# computes, or that redirect where an import looks (``site.addsitedir``,
# ``os.chdir``: ``-c`` code imports from the working directory).
_DYNAMIC_IN_CODE = frozenset(
    {
        "import_module",
        "__import__",
        "exec",
        "eval",
        "run_path",
        "run_module",
        "spec_from_file_location",
        "SourceFileLoader",
        "addsitedir",
        "chdir",
    }
)
# The import system's state: changed, it redirects what an import finds.
_IMPORT_STATE = frozenset(
    {"sys.path", "sys.meta_path", "sys.path_hooks", "sys.path_importer_cache", "sys.modules"}
)


class _ImportState:
    """What in a program is the import system's state (``_IMPORT_STATE``):
    a dotted name through the program's own imports (``import sys as s``,
    ``from sys import path``), a name bound to one (``p = sys.path``), and
    any ``<x>.path``; with ``loose``, a bare ``path`` too (an item of it
    stored, a mutating method on it), as it may be ``sys.path`` bound
    otherwise."""

    def __init__(self, tree: ast.AST, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.held = {n for n, full in aliases.items() if full in _IMPORT_STATE}
        while True:
            more = {
                t.id
                for node in ast.walk(tree)
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                and node.value is not None
                and not isinstance(node.value, ast.Subscript)  # an item, or a copy
                and self.names(node.value, loose=False)
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                for t in ast.walk(target)
                if isinstance(t, ast.Name)
            } - self.held
            if not more:
                break
            self.held |= more

    def names(self, expr: ast.expr, *, loose: bool) -> bool:
        while isinstance(expr, ast.Subscript):
            expr = expr.value
        parts = _flatten_chain(expr)
        if parts is None:
            return False
        if len(parts) == 1:
            return parts[0] in self.held or (loose and parts[0] == "path")
        if parts[-1] == "path":
            return True
        head = self.aliases.get(parts[0], parts[0])
        return ".".join([head, *parts[1:]]) in _IMPORT_STATE

    def changed_by(self, node: ast.AST) -> bool:
        """A statement or call that changes the import system's state: an
        item or attribute stored or deleted (``sys.path[:0] = [...]``,
        ``sys.path = [...]``, ``del sys.modules["m"]``), an augmented
        assignment (``sys.path += [...]``), a mutating method
        (``sys.path.insert``, ``sys.meta_path.append``), ``setattr(sys,
        ...)``. Binding a bare name (``path = ...``) changes only the
        name."""
        targets: list[ast.expr] = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            return self.names(node.target, loose=isinstance(node.target, ast.Subscript))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in STATE_MUTATORS:
                return self.names(func.value, loose=True)
            if isinstance(func, ast.Name) and func.id in ("setattr", "delattr") and node.args:
                parts = _flatten_chain(node.args[0])
                return parts is not None and self.aliases.get(parts[0], parts[0]) == "sys"
            return False
        return any(
            isinstance(sub, (ast.Subscript, ast.Attribute)) and self.names(sub, loose=True)
            for target in targets
            for sub in ast.walk(target)
        )


def code_edges(indexer: Resolver, source: str, code: str, module: str | None = None) -> bool:
    """A program ``source`` runs (``-c`` code, ``exec`` text, a script the
    index does not read): an import of each in-scope module it imports, a
    reference to each name it reaches through those imports (``from
    pkg.core import value``; ``value()``, ``__import__("pkg.m").m.f``),
    each name it reads from the namespace it runs in when that is an
    in-scope module's (``module``: ``exec`` text in its module's globals
    reading ``helper()``), and the modules a ``pytest_plugins`` it binds
    names, whole (pytester's conftests). False when the program does not
    parse, holds a template field nothing filled (``HOLE_NAME``), imports
    relatively, or may import, run or look something up by a name or path
    it computes (``import_module(name)``, ``exec``, ``runpy``,
    ``getattr(x, name)``, ``globals()``) or change where its imports look
    (``sys.path``, ``sys.modules``, ``os.chdir``): then it can run
    anything."""
    if HOLE_NAME in code:
        return False
    try:
        # As ``textwrap.dedent`` (unwrapped by ``code_texts``) would leave it.
        tree = ast.parse(textwrap.dedent(code))
    except (SyntaxError, ValueError):
        return False
    aliases: dict[str, str] = {}
    stars: list[str] = []
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _module_run(indexer, source, alias.name)
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return False  # relative to a package the text has no place in
            _module_run(indexer, source, node.module)
            for alias in node.names:
                if alias.name == "*":
                    stars.append(node.module)
                    continue
                full = f"{node.module}.{alias.name}"
                _module_run(indexer, source, full)
                aliases[alias.asname or alias.name] = full
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
    state = _ImportState(tree, aliases)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _flatten_chain(node) == ["sys", "executable"]:
            return False  # it starts a Python program of its own
        if state.changed_by(node):
            return False
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _DYNAMIC_IN_CODE and _imported_module(node) is None:
                return False
            if name in _NAMESPACE_IN_CODE or (
                name in _LOOKUPS_IN_CODE and (len(node.args) < 2 or _string(node.args[1]) is None)
            ):
                return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            parts = _flatten_chain(node)
            if parts is None:
                chain = _imported_chain(node)
                chains = [chain] if chain is not None else []
            elif not parts:
                continue
            elif parts[0] in aliases:
                chains = [aliases[parts[0]].split(".") + parts[1:]]
            elif parts[0] in bound:
                continue
            elif isinstance(node, ast.Name) and stars:
                chains = [[*star.split("."), parts[0]] for star in stars]
            elif module is not None:
                # Read from the namespace the text runs in.
                chains = [[*module.split("."), *parts]]
            else:
                continue
            for chain in chains:
                target = indexer.resolve_dotted(chain)
                if target is not None:
                    indexer._record(source, target, chain=".".join(chain))
    for plugin in _declared_plugins(tree):
        whole_module(indexer, source, plugin, "loaded as a pytest plugin")
    return True


# Calls in a program that reach its namespace, or look names up by a name
# it computes (a literal second argument is a name written out).
_NAMESPACE_IN_CODE = frozenset({"globals", "locals", "vars", "dir"})
_LOOKUPS_IN_CODE = frozenset({"getattr", "setattr", "delattr", "hasattr"})


def _imported_module(call: ast.Call) -> list[str] | None:
    """The module parts a literal ``__import__("a.b")`` (its top package,
    unless a ``fromlist`` is given) or ``import_module("a.b")`` yields; None
    for any other call."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    text = _string(call.args[0]) if call.args else None
    if name not in ("__import__", "import_module") or text is None:
        return None
    parts = text.split(".")
    if not all(p.isidentifier() for p in parts):
        return None
    if name == "import_module":
        if len(call.args) > 1 or any(k.arg == "package" for k in call.keywords):
            return None  # relative to a package the text has no place in
        return parts
    fromlist = call.args[3] if len(call.args) > 3 else None
    for k in call.keywords:
        if k.arg == "fromlist":
            fromlist = k.value
    if fromlist is None or (isinstance(fromlist, (ast.List, ast.Tuple)) and not fromlist.elts):
        return parts[:1]
    return parts


def _imported_chain(node: ast.expr) -> list[str] | None:
    """``__import__("pkg.m").m.f``: the dotted name an attribute chain off a
    literal import call stands for."""
    attrs: list[str] = []
    expr = node
    while isinstance(expr, ast.Attribute):
        attrs.insert(0, expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Call):
        return None
    parts = _imported_module(expr)
    return None if parts is None else [*parts, *attrs]


def _declared_plugins(tree: ast.AST) -> list[str]:
    """The literal module names a ``pytest_plugins`` assignment in ``tree``
    holds."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            continue
        values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
        for value in values:
            text = _string(value)
            if text is not None and text not in found:
                found.append(text)
    return found


def observe_command(collector: _ReferenceCollector, node: ast.List | ast.Tuple) -> None:
    """A command list with the interpreter in it: what program it runs.
    One the analysis cannot name is an unbounded dynamic reference. A list
    the interpreter list is concatenated with (``[sys.executable] + [...]``)
    is part of the command."""
    _observe_items(collector, list(node.elts), node)


def observe_call_arguments(
    collector: _ReferenceCollector, node: ast.Call, parts: list[str]
) -> None:
    """A call that starts a process from its positional arguments
    (``VARARGS_PROCESS_CALLS``): they are the command. ``os.execl``'s
    second argument is the program's own name again, not an argument."""
    if parts[-1] not in VARARGS_PROCESS_CALLS or not node.args:
        return
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple, ast.Constant, ast.JoinedStr, ast.BinOp)):
        return  # a list is a command of its own; a string a shell line
    items = list(node.args[1:] if parts[-1].startswith("spawn") else node.args)
    k = _interpreter_index(collector, items)
    if k is None:
        return
    if k + 1 < len(items) and _is_interpreter(collector, items[k + 1]):
        del items[k + 1]
    _observe_items(collector, items, None, interpreter=k)


def _observe_items(
    collector: _ReferenceCollector,
    items: list[ast.expr],
    node: ast.List | ast.Tuple | None,
    *,
    interpreter: int | None = None,
) -> None:
    k = _interpreter_index(collector, items) if interpreter is None else interpreter
    if k is None:
        return
    if any(_is_interpreter(collector, item) for item in items[k + 1 :]):
        return  # ``(sys.executable, realpath(sys.executable))``: paths, not a command
    parent = collector._parent(node) if node is not None else None
    if (
        node is not None
        and _string(_unwrap(items[k])) is not None
        and not (
            (
                isinstance(parent, ast.Call)
                and (parts := _flatten_chain(parent.func)) is not None
                and parts[-1] in _PROCESS_CALLS
            )
            or any(
                (text := _string(item)) is not None
                and (text.startswith("-") or text.rstrip().endswith((".py", ".pyw")))
                for item in items[k + 1 :]
            )
        )
    ):
        return  # ``["python", "recursion"]``: words, not a command
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add) and parent.left is node:
        if not isinstance(parent.right, (ast.List, ast.Tuple)):
            collector._dynamic(f"{RUNS_PROGRAM}: an import of anything")
            return
        items += parent.right.elts
    if not _program_known(collector, items[k + 1 :]):
        collector._dynamic(f"{RUNS_PROGRAM}: an import of anything")


# Calls (by their last name) that start a process from a command list.
_PROCESS_CALLS = frozenset(
    {
        "run",
        "call",
        "check_call",
        "check_output",
        "Popen",
        "run_process",
        "open_process",
        "create_subprocess_exec",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
        "spawn",
    }
)

# Calls that run a shell command line.
SHELL_CALLS = frozenset(
    {
        "os.system",
        "os.popen",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "asyncio.create_subprocess_shell",
    }
)
_HOLE = "\x00"
_HOLE_WORD = re.compile(f"{_HOLE}(\\d+){_HOLE}")


def _shell_pieces(node: ast.expr) -> list[str | ast.expr] | None:
    """The literal text and the expressions of an f-string or a ``+``
    chain of strings, in order; None for anything else."""
    if isinstance(node, ast.JoinedStr):
        return [
            str(v.value)
            if isinstance(v, ast.Constant)
            else (v.value if isinstance(v, ast.FormattedValue) else v)
            for v in node.values
        ]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left: list[str | ast.expr] = _shell_pieces(node.left) or [node.left]
        right: list[str | ast.expr] = _shell_pieces(node.right) or [node.right]
        return left + right
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return None


def observe_shell(
    collector: _ReferenceCollector, node: ast.Constant | ast.JoinedStr | ast.BinOp
) -> None:
    """A command line with the interpreter in it (an f-string or a ``+``
    chain holding ``sys.executable``, or a string a shell-running call is
    given that holds ``python``, each after ``VAR=x``/launcher words): the
    program it runs, as for a command list."""
    parent = collector._parent(node)
    shell_call = (
        isinstance(parent, ast.Call)
        and bool(parent.args)
        and parent.args[0] is node
        and (parts := _flatten_chain(parent.func)) is not None
        and bool(collector._canonical_names(parts) & SHELL_CALLS)
    )
    if isinstance(node, ast.Constant):
        if not shell_call:
            return
    elif isinstance(node, ast.BinOp):
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add):
            return  # part of a longer chain, seen whole from its top
        if not any(isinstance(p, ast.expr) for p in _shell_pieces(node) or []):
            return  # literal pieces only: a constant, not a command built here
    pieces = _shell_pieces(node)
    if pieces is None:
        return
    holes: list[ast.expr] = []
    text = ""
    for piece in pieces:
        if isinstance(piece, str):
            text += piece
        else:
            holes.append(piece)
            text += f" {_HOLE}{len(holes) - 1}{_HOLE} "
    words = text.split()
    # The interpreter: an expression that is one (``sys.executable``), or
    # in a shell call's string a ``python`` word, after launcher words.
    while words:
        word = words[0]
        if (hole := _HOLE_WORD.fullmatch(word)) is not None:
            expr = holes[int(hole.group(1))]
            if _is_interpreter(collector, expr):
                words.pop(0)
                break
            return
        if _HOLE in word:
            return  # a literal holding NUL, or text joined to an expression
        if shell_call and _PYTHON.match(word):
            words.pop(0)
            break
        if not _launcher_word(word):
            return
        words.pop(0)
    else:
        return
    rest = " ".join(_HOLE if _HOLE in w else w for w in words)
    if not _command_line_known(collector, rest):
        collector._dynamic(f"{RUNS_PROGRAM}: an import of anything")


def _command_line_known(collector: _ReferenceCollector, rest: str) -> bool:
    """The arguments after the interpreter on a command line (``-m``
    names and ``.py`` paths in it were recorded by observe_string)."""
    try:
        words = shlex.split(rest)
    except ValueError:
        return False
    for i, word in enumerate(words):
        if _HOLE in word:
            return False
        if word in _NOTHING_RUNS:
            return True
        if word == "-c" or (word.startswith("-c") and len(word) > 2):
            code = word[2:] if word != "-c" else (words[i + 1] if i + 1 < len(words) else None)
            return code is not None and _HOLE not in code and _run_code(collector, code)
        if word == "-m" or (word.startswith("-m") and len(word) > 2):
            name = word[2:] if word != "-m" else (words[i + 1] if i + 1 < len(words) else "")
            if name.split(".")[0] in CODE_RUNNERS:
                return _runner_words_known(collector, name.split(".")[0], words[i + 2 :])
            if _HOLE in name:
                # ``-m pkg.hooks.{name}``: a module under the literal prefix.
                prefix = name.split(_HOLE)[0]
                if "." not in prefix.rstrip("."):
                    return False
                for module in collector.indexer.modules_with_prefix(prefix):
                    _module_run(collector.indexer, collector.source, module)
                return True
            return bool(name)
        if word.startswith("-"):
            continue
        return word.endswith((".py", ".pyw"))
    return False


def _runner_words_known(collector: _ReferenceCollector, runner: str, words: list[str]) -> bool:
    """What a code runner on a command line is given, as
    ``_runner_args_known`` reads a list."""
    return _runner_args_known(
        collector, [ast.Constant(value=w) for w in words], runner
    ) and not any(_HOLE in w for w in words)


def _program_known(collector: _ReferenceCollector, items: list[ast.expr]) -> bool:
    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, ast.Starred):
            return False
        text = _string(item)
        if text is None:
            return _script_known(collector, item)
        if text in _NOTHING_RUNS:
            return True
        if text in _OPTIONS_WITH_ARGUMENT:
            i += 2
            continue
        if text == "-m" or (text.startswith("-m") and len(text) > 2):
            name_expr = items[i + 1] if text == "-m" and i + 1 < len(items) else None
            names = (
                (text[2:],)
                if text != "-m"
                else (_module_names(collector, name_expr) if name_expr is not None else None)
            )
            if names is None:
                return False
            runners = [n.split(".")[0] for n in names if n.split(".")[0] in CODE_RUNNERS]
            if runners:
                rest = items[i + (2 if text == "-m" else 1) :]
                return all(_runner_args_known(collector, rest, runner) for runner in runners)
            return True
        if text == "-c" or (text.startswith("-c") and len(text) > 2):
            code = items[i + 1] if text == "-c" and i + 1 < len(items) else None
            texts = (
                (text[2:],)
                if text != "-c"
                else (code_texts(collector, code) if code is not None else None)
            )
            return texts is not None and all(_run_code(collector, t) for t in texts)
        if text == "-":
            return False  # the program comes from standard input
        if text.startswith("-"):
            i += 1
            continue
        return text.rstrip().endswith((".py", ".pyw"))  # a script path
    return False  # an interactive interpreter: code from standard input


def _module_names(collector: _ReferenceCollector, expr: ast.expr) -> tuple[str, ...] | None:
    """The modules ``-m <expr>`` may run (each in-scope one recorded)."""
    if (
        isinstance(expr, ast.Name)
        and expr.id == "__name__"
        and not collector._is_shadowed("__name__")
    ):
        names: tuple[str, ...] | None = (collector.scope.module.name,)
    else:
        names = collector.scope.string_candidates(expr)
    if names is None:
        # A parameter: the literal names its call sites pass bound it.
        return (
            ("",)
            if collector._param_dynamic(
                expr, "import", None, f"{RUNS_PROGRAM} (python -m <parameter>)"
            )
            else None
        )
    for name in names:
        _module_run(collector.indexer, collector.source, name)
    return names


def _is_directory(collector: _ReferenceCollector, text: str) -> bool:
    """Whether a literal names a directory under the roots (``tests``,
    ``tests/unit/``), by any path suffix: one holding Python files, or only
    other files (data, doctest text files: ``other_file_directories``)."""
    prefix = text.replace("\\", "/").strip("/")
    if not prefix or _GLOB.search(prefix):
        return True
    snapshot = collector.indexer.snapshot
    return any(
        path.startswith(prefix + "/") or f"/{prefix}/" in f"/{path}"
        for path in [*snapshot.files, *collector.indexer.excluded_paths]
    ) or any(
        directory == prefix or directory.endswith("/" + prefix)
        for directory in other_file_directories(snapshot)
    )


def other_file_directories(snapshot) -> list[str]:
    """Every directory holding a file under the roots that is not Python
    (``Snapshot.other_files``), sorted: what ``_is_directory`` reads of
    them, so the environment fingerprint covers it."""
    found: set[str] = set()
    for path in snapshot.other_files:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            found.add("/".join(parts[:i]))
    return sorted(found)


def test_file(collector: _ReferenceCollector, text: str, runner: str) -> bool:
    """A test runner's positional argument: a ``.py`` path or a node id
    (``tests/x.py::test_a``) is a test file run whole (``apply_scripts``);
    for ``unittest`` a dotted name is a module (or something in one) run
    whole. False for a directory or anything else the runner would collect
    from."""
    text = text.rstrip()
    path = text.split("::", 1)[0]
    if path.endswith((".py", ".pyw")):
        found = _normalise(path)
        if found is None:
            return False
        collector.indexer.out.test_runs.add((collector.source, found, runner))
        return True
    if runner == "unittest" and "/" not in text:
        parts = text.split(".")
        if not all(p.isidentifier() for p in parts):
            return False
        for i in range(len(parts), 0, -1):
            module = ".".join(parts[:i])
            if module in collector.indexer.scopes:
                return whole_module(collector.indexer, collector.source, module)
        return not _is_directory(collector, text)  # a module outside the roots
    return False


def _runner_args_known(collector: _ReferenceCollector, items: list[ast.expr], runner: str) -> bool:
    """What a code runner (``coverage run``, ``pytest``, ``unittest``) is
    given: known when every argument is written out, each ``.py`` path,
    node id or module a test runner runs is a file run whole
    (``test_file``) or, for another runner, a script, and no argument is a
    directory to collect from, ``discover``, or a bare word the runner
    would look for. A test runner given no file or module collects its
    default (``testpaths``, the current directory; ``unittest``'s
    discovery): unbounded."""
    named = False  # a test runner was given a file or module to run
    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, ast.Starred):
            return False
        text = _string(item)
        if text is None:
            if runner in TEST_RUNNERS:
                # ``str(here / "inner" / "check_inner.py")``: the literal
                # pieces name the file (matched by suffix after pass 2).
                path = _string(_unwrap(_last_piece(_unwrap(item))))
                if path is None or not test_file(collector, path, runner):
                    return False  # a path or a word from elsewhere
                named = True
            elif not _script_known(collector, item):
                return False
            i += 1
            continue
        if text == "-m" and runner in _MODULE_RUNNING_RUNNERS and i + 1 < len(items):
            names = _module_names(collector, items[i + 1])
            if names is None:
                return False
            runners = [n.split(".")[0] for n in names if n.split(".")[0] in CODE_RUNNERS]
            if runners:
                return all(_runner_args_known(collector, items[i + 2 :], r) for r in runners)
            i += 2
            continue
        if text in _NOTHING_RUNS:
            return True
        if text in _RUNNER_LONG_OPTIONS_WITH_ARGUMENT and i + 1 < len(items):
            # ``--tb short``: the next word is its argument. After any other
            # ``--long`` option (a flag such as ``--verbose``, or a plugin's
            # option nothing here knows) the next word is read as one of the
            # runner's own arguments: ``--verbose docs`` collects ``docs``,
            # which might hold anything (audit U22).
            i += 2
            continue
        if text.startswith("-"):
            i += 2 if text in _RUNNER_OPTIONS_WITH_ARGUMENT else 1
            continue
        if runner == "coverage" and text in _COVERAGE_INERT:
            return True
        if text == "run" and runner == "coverage":
            i += 1
            continue
        if runner in TEST_RUNNERS:
            if runner == "unittest" and text == "discover":
                return False
            if not test_file(collector, text, runner):
                return False
            named = True
        elif _is_directory(collector, text) or not text.endswith((".py", ".pyw")):
            return False  # a directory: whatever it holds may run
        i += 1
    return named or runner not in TEST_RUNNERS


def runner_list_known(collector: _ReferenceCollector, expr: ast.expr | None, runner: str) -> bool:
    """``pytest.main([...])``: the arguments as a list literal, or a local
    bound once to one; None (``pytest.main()``) runs what ``sys.argv``
    names."""
    seen = 0
    while isinstance(expr, ast.Name) and seen < 4:
        source = collector._local_source(expr.id)
        if source is None:
            break
        expr, seen = source, seen + 1
    if not isinstance(expr, (ast.List, ast.Tuple)):
        return False
    return _runner_args_known(collector, list(expr.elts), runner)


# --------------------------------------------------------------------------- after pass 2


def _matching_paths(by_name: dict[str, list[str]], suffix: str) -> list[str]:
    name = suffix.rpartition("/")[2]
    return [path for path in by_name.get(name, ()) if path == suffix or path.endswith("/" + suffix)]


def _conftest_chain(module_of: dict[str, str], path: str) -> list[str]:
    """The conftest modules pytest loads for a test file: from its
    directory outward."""
    found: list[str] = []
    directory = path.rpartition("/")[0]
    while True:
        candidate = f"{directory}/conftest.py" if directory else "conftest.py"
        if candidate in module_of:
            found.append(module_of[candidate])
        if not directory:
            return found
        directory = directory.rpartition("/")[0]


def apply_scripts(indexer: Resolver) -> None:
    """Match the ``.py`` paths pass 2 recorded against the files under the
    roots (see the module docstring)."""
    out = indexer.out
    index = indexer.index
    roots = indexer.snapshot.source_roots
    unmapped = {
        path: _digest(content.decode("utf-8", "surrogateescape"))
        for path, content in indexer.snapshot.files.items()
        if path.endswith((".py", ".pyw")) and module_name_for(path, roots) is None
    }
    index.scripts = dict(sorted(unmapped.items()))
    if not out.script_paths and not out.test_runs:
        return
    module_of = {scope.path: name for name, scope in indexer.scopes.items()}
    # Files ``[index] exclude`` keeps out: run unread (audit round 6, T2).
    excluded = set(indexer.excluded_paths)
    by_name: dict[str, list[str]] = {}
    for path in sorted(set(module_of) | set(unmapped) | excluded):
        by_name.setdefault(path.rpartition("/")[2], []).append(path)

    def unread(source: str, path: str) -> None:
        if path in excluded:
            # Its code is not read at all: an unknown dependency.
            indexer.excluded_import(source, module_name_for(path, roots) or "", path)
            return
        # A file the index does not read: its program, read here as
        # ``-c`` code is; the symbol also sees a change to the file.
        index.script_refs.add((source, path))
        code = indexer.snapshot.files[path].decode("utf-8", "surrogateescape")
        if not code_edges(indexer, source, code):
            out.unresolved.add(
                UnresolvedReference(
                    source,
                    UnresolvedKind.DYNAMIC,
                    "",
                    f"{RUNS_SCRIPT}{path}, a Python file no module name maps to that "
                    "imports or runs code by a name it computes: an import of anything",
                )
            )

    for source, suffix in sorted(out.script_paths):
        for path in _matching_paths(by_name, suffix):
            module = module_of.get(path)
            if module is not None:
                if module != source:
                    out.edges.add(Edge(source, module, EdgeKind.IMPORTS))
                continue
            unread(source, path)
    for source, suffix, runner in sorted(out.test_runs):
        if suffix.endswith((".py", ".pyw")):
            paths = _matching_paths(by_name, suffix)
        else:
            # A directory (``pytester.copy_example("cases")``): every
            # Python file under it.
            prefix = suffix.strip("/")
            paths = sorted(
                p
                for p in set(module_of) | set(unmapped) | excluded
                if p.startswith(prefix + "/") or f"/{prefix}/" in f"/{p}"
            )
        if not paths:
            out.unresolved.add(
                UnresolvedReference(
                    source,
                    UnresolvedKind.DYNAMIC,
                    "",
                    f"{RUNS_PROGRAM}: an import of anything ({runner} given {suffix}, which "
                    "no file under the source roots matches)",
                )
            )
        for path in paths:
            module = module_of.get(path)
            if module is None:
                unread(source, path)
                continue
            whole_module(indexer, source, module)
            if runner in ("pytest", "py.test"):
                for conftest in _conftest_chain(module_of, path):
                    whole_module(indexer, source, conftest)
                for conftest in _conftest_chain({p: p for p in excluded}, path):
                    unread(source, conftest)


def plugin_options(indexer: Resolver, source: str, words: list[str]) -> None:
    """``-p NAME`` among a pytest session's arguments (or an ini
    ``addopts``): the plugin module, loaded whole."""
    for i, word in enumerate(words):
        name = None
        if word == "-p" and i + 1 < len(words):
            name = words[i + 1]
        elif word.startswith("-p") and len(word) > 2 and not word.startswith("--"):
            name = word[2:]
        if name and not name.startswith("no:"):
            whole_module(indexer, source, name, "loaded as a pytest plugin")


def format_attribute_names(template: str) -> list[str]:
    """The attribute names the replacement fields of a format string read
    (``{0.name}``, ``{x.a.b}``, ``{x[0].c}``)."""
    found: list[str] = []
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    except ValueError:
        return found
    for field in fields:
        for name in _FIELD_ATTRIBUTE.findall(field):
            if name not in found:
                found.append(name)
    return found


_FIELD_ATTRIBUTE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")

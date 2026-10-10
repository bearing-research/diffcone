"""Project code a symbol runs by naming it in a string.

A test can run a script or a module of the project as a subprocess
(``[sys.executable, "scripts/gen.py"]``, ``-m pkg.tool``, ``python -c
"import pkg"``), or load a file by path; no import edge says so. Pass 2
records (cacheable per module):

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
  file reaches it (``SourceIndex.script_refs``);
* ``-m NAME`` (in a string, or as two items of a command list) naming an
  in-scope module: an import of it (and of ``NAME.__main__`` for a
  package);
* ``-c CODE`` whose code is written out (a literal, a local bound once to
  one, ``textwrap.dedent`` of one): the modules it imports and the names it
  reaches through them.

A command whose first item is the interpreter (a list starting with
``sys.executable`` or a ``python``/``python3`` literal, an f-string starting
with ``sys.executable``, a string a shell-running call is given that starts
with ``python``) and whose program is not one of those (a script path or a
``-m`` name from elsewhere, ``-c`` code built at run time, code read from
standard input, ``*args``) can run any code of the project: an unbounded
dynamic reference. A ``-m`` name that is a parameter is bounded by the
literal names the call sites pass, as an ``import_module`` of one is. ``-m``
of a code runner (``coverage``, ``pytest``, ``runpy``, ...) runs what
follows it in turn; a directory given to one runs whatever it holds.

Not modelled: a console script the project installs (``["tool", ...]``), a
file outside the source roots, a command handed over whole from elsewhere
with no interpreter in sight, a file loaded by a path built at run time
(``spec_from_file_location(name, path)``), ``sys.path`` changes, a test
reading a source file as text.
"""

from __future__ import annotations

import ast
import re
import shlex
from typing import TYPE_CHECKING

from diffcone.indexer.definitions import _flatten_chain
from diffcone.indexer.scopes import ModuleNode, Resolved
from diffcone.indexer.syntax import _digest
from diffcone.model import IMPORTS, MODULE, UNRESOLVED_DYNAMIC, Edge, UnresolvedReference
from diffcone.snapshot import module_name_for

if TYPE_CHECKING:
    from diffcone.indexer.references import _ReferenceCollector
    from diffcone.indexer.resolver import Resolver

# Detail prefixes of the dynamic references this module records. Evidence
# mode follows child processes itself and leaves both to static planning.
RUNS_PROGRAM = "starts a Python program it builds at run time"
RUNS_SCRIPT = "runs or reads "

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
# Calls whose argument is code text: ``textwrap.dedent("""...""")``.
_TEXT_WRAPPERS = frozenset({"dedent", "cleandoc", "strip", "lstrip", "rstrip"})


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
    node = indexer.resolve_dotted(parts)
    if not isinstance(node, ModuleNode) or node.module != ".".join(parts):
        return False
    indexer._module_import_edge(source, node.module)
    main = f"{node.module}.__main__"
    if main in indexer.scopes:
        indexer._module_import_edge(source, main)
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
    return parts is not None and collector._canonical_name(parts) == "sys.executable"


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
    node = collector.indexer.resolve_chain(parts[:-1], collector.scope)
    if isinstance(node, Resolved):
        symbol = collector.indexer.index.symbols.get(node.symbol)
        if symbol is not None and symbol.kind == MODULE and not node.detail:
            node = ModuleNode(symbol.id)
    if isinstance(node, ModuleNode):
        scope = collector.indexer.scopes.get(node.module)
        if scope is not None:
            collector.indexer.out.script_paths.add((collector.source, scope.path))
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
        indexer.out.unresolved.add(UnresolvedReference(source, UNRESOLVED_DYNAMIC, "", detail))
    else:
        indexer.out.script_paths.add((source, path))


def _code_texts(collector: _ReferenceCollector, expr: ast.expr) -> tuple[str, ...] | None:
    """The code ``-c`` may be given, when it is written out."""
    seen = 0
    while seen < 4:
        seen += 1
        if isinstance(expr, ast.Call):
            func = expr.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _TEXT_WRAPPERS:
                inner = expr.args[0] if expr.args else None
                if inner is None and isinstance(func, ast.Attribute):
                    inner = func.value
                if inner is None:
                    return None
                expr = inner
                continue
            return None
        if isinstance(expr, ast.Name) and _string(expr) is None:
            source = collector._local_source(expr.id)
            if source is not None:
                expr = source
                continue
        break
    return collector.scope.string_candidates(expr)


def _run_code(collector: _ReferenceCollector, code: str) -> bool:
    """``python -c code``: what it imports, and the names it reaches through
    those imports. False when that cannot be bounded (see _code_edges)."""
    return _code_edges(collector.indexer, collector.source, code)


# Calls in a program that import or run code by a name or path the program
# computes, and the ``sys.path`` changes that redirect an import.
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
        "insert",
        "append",
        "extend",
        "addsitedir",
    }
)


def _code_edges(indexer: Resolver, source: str, code: str) -> bool:
    """A program ``source`` runs (``-c`` code, a script the index does not
    read): an import of each in-scope module it imports, and a reference to
    each name it reaches through those imports (``from pkg.core import
    value``; ``value()``). False when the program does not parse, or may
    import or run something by a name or path it computes (``import_module``,
    ``exec``, ``sys.path`` changes, ``runpy``): then it can run anything."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return False
    aliases: dict[str, str] = {}
    stars: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _flatten_chain(node) == ["sys", "executable"]:
            return False  # it starts a Python program of its own
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _DYNAMIC_IN_CODE:
                if name in ("insert", "append", "extend"):
                    parts = _flatten_chain(func.value) if isinstance(func, ast.Attribute) else None
                    if parts is None or parts[-1:] != ["path"]:
                        continue  # a list's own method, not ``sys.path``'s
                return False
        if isinstance(node, ast.Import):
            for alias in node.names:
                _module_run(indexer, source, alias.name)
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            _module_run(indexer, source, node.module)
            for alias in node.names:
                if alias.name == "*":
                    stars.append(node.module)
                    continue
                full = f"{node.module}.{alias.name}"
                _module_run(indexer, source, full)
                aliases[alias.asname or alias.name] = full
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            parts = _flatten_chain(node)
            if not parts:
                continue
            if parts[0] in aliases:
                chains = [aliases[parts[0]].split(".") + parts[1:]]
            elif isinstance(node, ast.Name):
                chains = [[*star.split("."), parts[0]] for star in stars]
            else:
                continue
            for chain in chains:
                target = indexer.resolve_dotted(chain)
                if target is not None:
                    indexer._record(source, target, chain=".".join(chain))
    return True


def observe_command(collector: _ReferenceCollector, node: ast.List | ast.Tuple) -> None:
    """A command list starting with the interpreter: what program it runs.
    One the analysis cannot name is an unbounded dynamic reference. A list
    the interpreter list is concatenated with (``[sys.executable] + [...]``)
    is part of the command."""
    items = list(node.elts)
    if not items or not _is_interpreter(collector, items[0]):
        return
    parent = collector._parent(node)
    if _string(_unwrap(items[0])) is not None and not (
        (
            isinstance(parent, ast.Call)
            and (parts := _flatten_chain(parent.func)) is not None
            and parts[-1] in _PROCESS_CALLS
        )
        or any(
            (text := _string(item)) is not None
            and (text.startswith("-") or text.rstrip().endswith((".py", ".pyw")))
            for item in items[1:]
        )
    ):
        return  # ``["python", "recursion"]``: words, not a command
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add) and parent.left is node:
        if not isinstance(parent.right, (ast.List, ast.Tuple)):
            collector._dynamic(f"{RUNS_PROGRAM}: an import of anything")
            return
        items += parent.right.elts
    if not _program_known(collector, items[1:]):
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


def observe_shell(collector: _ReferenceCollector, node: ast.Constant | ast.JoinedStr) -> None:
    """A command line starting with the interpreter (an f-string starting
    with ``sys.executable``, or a string a shell-running call is given that
    starts with ``python``): the program it runs, as for a command list."""
    if isinstance(node, ast.Constant):
        parent = collector._parent(node)
        if not (
            isinstance(parent, ast.Call)
            and parent.args
            and parent.args[0] is node
            and (parts := _flatten_chain(parent.func)) is not None
            and collector._canonical_name(parts) in SHELL_CALLS
        ):
            return
        text = node.value if isinstance(node.value, str) else ""
        words = text.split(maxsplit=1)
        if not words or not _PYTHON.match(words[0]):
            return
    else:
        values = node.values
        if not values or not isinstance(values[0], ast.FormattedValue):
            return
        if not _is_interpreter(collector, values[0].value):
            return
        text = "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _HOLE
            for v in values
        )
    rest = text.split(maxsplit=1)
    if not _command_line_known(collector, rest[1] if len(rest) > 1 else ""):
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
                continue  # what the runner runs comes next
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
            if any(n.split(".")[0] in CODE_RUNNERS for n in names):
                rest = items[i + (2 if text == "-m" else 1) :]
                return _runner_args_known(collector, rest)
            return True
        if text == "-c" or (text.startswith("-c") and len(text) > 2):
            code = items[i + 1] if text == "-c" and i + 1 < len(items) else None
            texts = (
                (text[2:],)
                if text != "-c"
                else (_code_texts(collector, code) if code is not None else None)
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


def _runner_args_known(collector: _ReferenceCollector, items: list[ast.expr]) -> bool:
    """What a code runner (``coverage run``, ``pytest``) is given: known when
    every argument is a literal, a ``.py`` path or a module's ``__file__``,
    and no literal is a directory to collect from."""
    for i, item in enumerate(items):
        if isinstance(item, ast.Starred):
            return False
        text = _string(item)
        if text is None:
            if not _script_known(collector, item):
                return False
        elif text == "-m" and i + 1 < len(items):
            if _module_names(collector, items[i + 1]) is None:
                return False
        elif not text.startswith("-") and "/" in text and not text.endswith((".py", ".pyw")):
            return False  # a directory: whatever it holds may run
    return True


# --------------------------------------------------------------------------- after pass 2


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
    if not out.script_paths:
        return
    module_of = {scope.path: name for name, scope in indexer.scopes.items()}
    by_name: dict[str, list[str]] = {}
    for path in sorted(set(module_of) | set(unmapped)):
        by_name.setdefault(path.rpartition("/")[2], []).append(path)
    for source, suffix in sorted(out.script_paths):
        name = suffix.rpartition("/")[2]
        for path in by_name.get(name, ()):
            if path != suffix and not path.endswith("/" + suffix):
                continue
            module = module_of.get(path)
            if module is not None:
                if module != source:
                    out.edges.add(Edge(source, module, IMPORTS))
                continue
            # A file the index does not read: its program, read here as
            # ``-c`` code is; the symbol also sees a change to the file.
            index.script_refs.add((source, path))
            code = indexer.snapshot.files[path].decode("utf-8", "surrogateescape")
            if not _code_edges(indexer, source, code):
                out.unresolved.add(
                    UnresolvedReference(
                        source,
                        UNRESOLVED_DYNAMIC,
                        "",
                        f"{RUNS_SCRIPT}{path}, a Python file no module name maps to that "
                        "imports or runs code by a name it computes: an import of anything",
                    )
                )

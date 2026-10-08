"""pytest plugin that records execution evidence.

Loaded as ``-p diffcone_collect``: ``diffcone`` links this file into a
directory of its own on ``PYTHONPATH``, so it imports nothing of diffcone and
needs nothing but the standard library and pytest.

It runs inside the project's own test process, started by ``diffcone
collect`` or ``diffcone run --evidence`` (execution.py), never by planning.
It must not change what a test does: it prints nothing, and an exception
inside it is recorded as a collection error, which makes diffcone refuse
the store, rather than raised into the suite.

What it records, per test (parameter cases folded into their function, as
``validate`` folds them):

* the code objects executed in the test's setup, call and teardown, and in
  the setup of every non-function-scoped fixture the test activated
  (credited to every such test, not only the first; and the keys of those
  fixtures, so the fold credits what another process's setup of the same
  fixture ran too: an xdist "compute once" fixture runs in one worker).
  ``sys.monitoring`` ``PY_START`` and ``PY_RESUME`` (a generator or
  coroutine resumed in a later test) with DISABLE, re-armed at every window
  boundary, and ``PY_THROW`` (re-entered by ``.throw()``/``.close()``);
* the repository paths it opened, ``stat``ed, ``access``ed or listed (audit
  hooks, ``sqlite3.connect`` and ``ctypes.dlopen`` included, and wrappers
  of ``os.stat``/``os.lstat``/``os.access``, which never warn, so the extra
  frame cannot move a warning's ``stacklevel``), with ``/`` separators;
* whether it started a subprocess (other than a ``python -c`` probe that
  can run no project code) or a subinterpreter: code no record names runs
  there. The project code other threads are in the middle of when a window
  opens is credited to it, as those threads run beside its test.

Outside every test window it records the code and paths of imports,
collection and hooks; for each code object run by an import, the innermost
module whose top-level code was running (``import_by``); and the code that
ran outside every window while no import was running (hooks, collection).
``functools`` caches of project code are cleared before each test so a
value one test computed is recomputed, and recorded, by the next.

Environment variables:

``DIFFCONE_COLLECT_OUT``       directory for the raw records; recording is
                               off without it
``DIFFCONE_COLLECT_ROOT``      the checkout root (default: the current directory)
``DIFFCONE_COLLECT_REPO``      the repository it was made from (``collect --rev``
                               runs a temporary worktree); the project's own
                               editable install there is not environment
``DIFFCONE_COLLECT_PACKAGES``  comma-separated top-level packages of the
                               project: a module of one of them loaded from
                               outside the root means an installed copy ran
``DIFFCONE_COLLECT_REVERSE``   run the collected tests in reverse order
``DIFFCONE_ENV_VARIABLES``     comma-separated project variables that change
                               what tests do (pandas' ``PANDAS_FUTURE``),
                               recorded with the environment and checked
``DIFFCONE_CHECK_ENV``         check mode: the environment hash evidence was
                               recorded under; the plugin always writes
                               ``DIFFCONE_CHECK_REPORT`` (``{"match": bool,
                               "environment": {...}}``) and, on a mismatch,
                               stops the session before any test runs
``DIFFCONE_CHECK_ONLY``        with check mode: stop after the check (the
                               evidence plan selected nothing to run)

Files written per process: ``process-<pid>.json`` (code table, import-time
records, environment, errors) and ``tests-<pid>.bin`` (length-prefixed
zlib-compressed JSON records, one per run of consecutive items of a test).
"""

from __future__ import annotations

import ast
import functools
import gc
import hashlib
import importlib
import json
import os
import platform
import re
import shlex
import struct
import sys
import threading
import zlib
from typing import Any
from urllib.parse import urlparse

import pytest

TOOL = 3
FLAG_SUBPROCESS = 1
ENV_VARIABLES = ("PYTHONHASHSEED", "TZ", "LANG", "LC_ALL", "PYTHONWARNINGS")


def _variables() -> tuple[str, ...]:
    """The variables the environment includes: these, and the project's own
    (``DIFFCONE_ENV_VARIABLES``, from ``collect --env-var``)."""
    extra = {v for v in os.environ.get("DIFFCONE_ENV_VARIABLES", "").split(",") if v}
    return ENV_VARIABLES + tuple(sorted(extra - set(ENV_VARIABLES)))


SUBPROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        # Windows' process creation (multiprocessing's spawn there).
        "_winapi.CreateProcess",
    }
)
# Files opened by C code, which raises no ``open`` event: the first
# argument is the path.
C_OPEN_EVENTS = frozenset({"sqlite3.connect", "ctypes.dlopen"})
LISTING_EVENTS = frozenset({"os.listdir", "os.scandir"})


# --------------------------------------------------------------------------- environment


def _editable(dist) -> str | None:
    """The local directory of an editable install, else None. The checkout's
    own (a source checkout, whose version string ``3.0.0.dev0+1234.gabcdef``
    changes with every commit) is code the index reads, not environment."""
    try:
        text = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    url = urlparse(data.get("url", ""))
    if not (data.get("dir_info", {}).get("editable") and url.scheme == "file"):
        return None
    return os.path.realpath(url.path)


def _in_checkout(path: str) -> bool:
    """Inside the checkout being run, or the repository it was made from
    (``collect --rev`` runs a temporary worktree, while the project's own
    editable install points at the repository)."""
    roots = {
        os.path.realpath(r)
        for r in (
            os.environ.get("DIFFCONE_COLLECT_ROOT") or os.getcwd(),
            os.environ.get("DIFFCONE_COLLECT_REPO") or "",
        )
        if r
    }
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def environment() -> dict:
    """What a recorded path depends on besides the code: the interpreter, the
    installed distributions and a few variables. Computed identically when
    recording and when checking."""
    import importlib.metadata as metadata

    dists = set()
    for dist in metadata.distributions():
        try:
            name = dist.metadata["Name"] or ""
        except Exception:
            name = ""
        if not name:
            continue
        local = _editable(dist)
        if local is not None and _in_checkout(local):
            continue
        entry = f"{name.lower().replace('_', '-')}=={dist.version}"
        # A sibling editable install (``pip install -e ../lib``) is not traced
        # either, but where it lives is part of what runs.
        dists.add(entry + (f" @ {local}" if local is not None else ""))
    # The user's own entries: diffcone's plugin directory and the checkout's
    # source roots (``collect`` puts them first; a recorded commit's checkout
    # is a temporary worktree) are diffcone's, not the environment's.
    pythonpath = [
        p
        for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if p
        and not os.path.basename(p.rstrip(os.sep)).startswith("diffcone-plugin-")
        and not _in_checkout(os.path.realpath(p))
    ]
    return {
        "implementation": sys.implementation.name,
        "python": sys.version,
        "platform": sys.platform,
        "machine": platform.machine(),
        # ``-O`` strips asserts, and the calls inside them.
        "optimize": sys.flags.optimize,
        "distributions": sorted(dists),
        "variables": {k: os.environ.get(k) for k in _variables()},
        "pythonpath": pythonpath,
    }


def environment_hash(env: dict) -> str:
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- state

OUT = os.environ.get("DIFFCONE_COLLECT_OUT")
environment_at_start: dict | None = None
_root = os.environ.get("DIFFCONE_COLLECT_ROOT") or os.getcwd()
# Both spellings: a code object's file name is the path it was imported by
# (``/tmp/x`` on macOS is ``/private/tmp/x``).
ROOTS = tuple(
    sorted(
        {
            os.path.normcase(r) if os.name == "nt" else r
            for r in (os.path.abspath(_root) + os.sep, os.path.realpath(_root) + os.sep)
        }
    )
)
PACKAGES = frozenset(p for p in os.environ.get("DIFFCONE_COLLECT_PACKAGES", "").split(",") if p)
IGNORED_DIRS = (".git" + os.sep, ".diffcone" + os.sep)
INSTALLED = (os.sep + "site-packages" + os.sep, os.sep + "dist-packages" + os.sep)

errors: list[str] = []
codes: dict[object, int] = {}
table: list[list] = []
active: list[dict] = []  # open windows, innermost last
importing: list[str] = []  # modules whose top-level code is running, innermost last
import_by: dict[int, set[str]] = {}
finished = False  # the process record is written
hook_codes: set[int] = set()  # ran outside every test window with no import running
# Opened or stat'ed, and listed, by project code outside every test window.
import_paths: dict[str, set[str]] = {}
import_dirs: dict[str, set[str]] = {}
fixture_windows: dict[tuple[str, str, str], dict] = {}
collected: set[str] = set()  # every test collected, parameters folded
collection_ran = False  # this process collected (an xdist controller does not)
used_fixtures: dict[str, list] = {}
caches: list = []
recording = False


def _window() -> dict:
    # ``fixtures``: the non-function-scoped fixtures a test used, by key, so
    # a fixture another process (an xdist worker) set up is credited too.
    return {"codes": set(), "paths": set(), "dirs": set(), "flags": 0, "fixtures": set()}


import_window = _window()


def _windows():
    return active if active else (import_window,)


def _error(where: str, exc: BaseException) -> None:
    if len(errors) < 50:
        errors.append(f"{where}: {type(exc).__name__}: {exc}")


def _relative(path: str) -> str | None:
    """The checkout-relative path, "" for the root itself; None outside the
    checkout, and for an environment kept inside it (``.venv``). Normalised
    first: a module imported through ``sys.path.insert(0, ".../tests/../src")``
    (a common conftest idiom) has ``..`` in its code objects' file names."""
    if os.path.isabs(path):
        path = os.path.normpath(path)
    rel = _under_root(path)
    if rel is None and os.name == "nt" and "~" in path and os.path.isabs(path):
        # ``C:\\Users\\RUNNER~1`` (an 8.3 short name) and its long name are one
        # directory.
        try:
            rel = _under_root(os.path.realpath(path))
        except (OSError, ValueError):
            rel = None
    return rel


def _under_root(path: str) -> str | None:
    # Windows compares case-folded (``normcase`` keeps the length), and the
    # path keeps its own case.
    folded = os.path.normcase(path) if os.name == "nt" else path
    for root in ROOTS:
        if folded + os.sep == root:
            return ""
        if folded.startswith(root):
            rel = path[len(root) :]
            if rel.startswith(IGNORED_DIRS) or any(p in rel for p in INSTALLED):
                return None
            # Records use ``/`` on every platform, as the index does.
            return rel.replace(os.sep, "/") if os.sep != "/" else rel
    return None


CYTHON_SUFFIXES = (".pyx", ".pxd", ".pxi")


_code_paths: dict[str, str | None] = {}


def _code_path(name: str) -> str | None:
    """A code object's checkout-relative file. A profiled Cython build names
    its source relative to the directory it was built from, the checkout
    root, rather than by an absolute path. Cached: a profiled build raises a
    start event on every call of a Cython function, so this runs per call."""
    try:
        return _code_paths[name]
    except KeyError:
        pass
    path: str | None = name
    if not os.path.isabs(name) and name.endswith(CYTHON_SUFFIXES):
        path = os.path.normpath(os.path.join(ROOTS[0], name))
        if not os.path.isfile(path):
            path = None
    rel = _code_paths[name] = _relative(path) if path is not None else None
    return rel


# --------------------------------------------------------------------------- monitoring

# None before 3.12, where pytest_configure stops before anything uses it.
mon: Any = getattr(sys, "monitoring", None)


def _on_start(code, offset):
    try:
        rel = _code_path(code.co_filename)
        if rel is None:
            return mon.DISABLE
        i = codes.get(code)
        if i is None:
            i = codes[code] = len(table)
            table.append([rel, code.co_firstlineno, code.co_qualname])
        for w in _windows():
            w["codes"].add(i)
        if not active and not importing and code.co_name != "<module>":
            hook_codes.add(i)
        if code.co_name == "<module>":
            # Credit what this import runs to this module: watch for its end,
            # and re-arm so code that already ran elsewhere is seen again.
            importing.append(rel)
            mon.set_local_events(TOOL, code, mon.events.PY_RETURN)
            mon.restart_events()
        elif importing:
            import_by.setdefault(i, set()).add(importing[-1])
    except Exception as exc:
        _error("PY_START", exc)
    return mon.DISABLE


def _on_resume(code, offset):
    # A generator or coroutine started earlier (in another test, at import)
    # and resumed now: the running test executes its code too.
    try:
        rel = _code_path(code.co_filename)
        if rel is None:
            return mon.DISABLE
        i = codes.get(code)
        if i is None:
            i = codes[code] = len(table)
            table.append([rel, code.co_firstlineno, code.co_qualname])
        for w in _windows():
            w["codes"].add(i)
    except Exception as exc:
        _error("PY_RESUME", exc)
    return mon.DISABLE


def _on_throw(code, offset, exc):
    # A generator or coroutine re-entered by ``.throw()`` or ``.close()``:
    # resumed like PY_RESUME, but this event cannot be disabled.
    if code not in codes and _code_path(code.co_filename) is None:
        return
    try:
        i = codes.get(code)
        if i is None:
            i = codes[code] = len(table)
            table.append([_code_path(code.co_filename), code.co_firstlineno, code.co_qualname])
        for w in _windows():
            w["codes"].add(i)
    except Exception as err:
        _error("PY_THROW", err)


def _on_return(code, offset, retval):
    try:
        if code.co_name == "<module>" and importing:
            importing.pop()
            mon.restart_events()  # the importer's own calls are credited to it again
    except Exception as exc:
        _error("PY_RETURN", exc)


def _on_unwind(code, offset, exc):
    # An import that raises (``pytest.importorskip`` at module level) ends with
    # an unwind, not a return. This event cannot be disabled: keep it cheap.
    if code.co_name != "<module>" or not importing:
        return
    try:
        if importing[-1] == _code_path(code.co_filename):
            importing.pop()
            mon.restart_events()
    except Exception as err:
        _error("PY_UNWIND", err)


# --------------------------------------------------------------------------- paths


# Loader methods that read a package's data for its caller
# (``pkgutil.get_data``, ``importlib.resources``), not to import a module.
DATA_READERS = frozenset({"get_data", "open_resource", "read_binary", "read_text"})


def _actor() -> str:
    """Who touched a path: ``import`` when the import system did (it finds
    modules, which the index covers), ``project`` when project code is on
    the stack above it, ``other`` for pytest or a library acting alone
    (collection walks and stats every directory and file)."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if name.startswith("<frozen importlib") and frame.f_code.co_name not in DATA_READERS:
            return "import"
        if _relative(name) is not None:
            return "project"
        frame = frame.f_back
    return "other"


def _touch(path, listing: bool = False, own_source: bool = False) -> None:
    """``own_source``: a stat of a source file. While a module is being
    imported, its stat of its *own* file (``Path(__file__).resolve()``)
    sees only that the file exists, recorded as a name seen like a listing;
    every other stat of a source file (``os.path.getsize``, ``linecache``
    checking ``inspect.getsource``'s cached copy) can depend on what the
    file holds and counts as reading it."""
    if isinstance(path, int) or path is None:
        return
    try:
        path = os.fsdecode(os.fspath(path))
    except TypeError:
        return
    rel = _relative(os.path.abspath(path))
    if rel is None:
        return
    actor = _actor()
    if actor == "import":
        return
    if own_source and not active and importing and importing[-1] == rel:
        listing = True
    if active:
        # Inside a test anything counts: a library or plugin reading a file
        # for the test (a data-directory fixture copying files) included.
        for w in active:
            w["dirs" if listing else "paths"].add(rel)
    elif actor == "project":
        # Outside every test: project code at import (a parametrize list
        # globbed from a directory) or in a hook, credited to the module
        # being imported ("" for none).
        seen = import_dirs if listing else import_paths
        seen.setdefault(rel, set()).add(importing[-1] if importing else "")


_inert_pending = False


def _audit(event, args):
    try:
        if event == "open":
            if args:
                _touch(args[0])
        elif event in LISTING_EVENTS:
            _touch(args[0] if args and args[0] is not None else ".", listing=True)
        elif event in C_OPEN_EVENTS:
            if args and isinstance(args[0], (str, bytes, os.PathLike)):
                _touch(args[0])
        elif event in SUBPROCESS_EVENTS:
            global _inert_pending
            if event == "_winapi.CreateProcess" and _inert_pending:
                # The process an inert ``subprocess.Popen`` just judged
                # (Windows raises both events for one process).
                _inert_pending = False
            elif _inert_probe(event, args):
                _inert_pending = event == "subprocess.Popen"
            else:
                _inert_pending = False
                for w in _windows():
                    w["flags"] |= FLAG_SUBPROCESS
    except Exception as exc:
        _error(f"audit {event}", exc)


# Single-letter interpreter options that take no value and load no code.
_PLAIN_OPTIONS = frozenset({"-I", "-E", "-S", "-s", "-B", "-u", "-O", "-OO", "-q", "-P"})
# Other programs' queries that run no Python of the project: (program, the
# arguments that start the command line). ``uv run``, ``git`` (hooks) and
# console scripts can run project code and are not here.
_INERT_QUERIES = (
    ("uv", ("python", "list")),
    ("uv", ("python", "find")),
    ("uv", ("python", "dir")),
    ("uv", ("--version",)),
    ("uv", ("-V",)),
)
# A ``-c`` snippet is inert only when everything it imports is built into
# the interpreter (nothing on ``sys.path``, the working directory or
# ``PYTHONPATH`` can stand in for it), and it names nothing that loads or
# runs code by other means.
_LOADING_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "open",
        "__import__",
        "__builtins__",
        "builtins",
        "__loader__",
        "__spec__",
        "breakpoint",
        "_imp",
        "execfile",
        "system",
        "popen",
        "spawnl",
        "spawnv",
        "execv",
        "execl",
        "fork",
        "startfile",
        "modules",
        "path",
        "meta_path",
        "path_hooks",
        "setprofile",
        "settrace",
        "addaudithook",
    }
)
# Environment variables that make an interpreter run or find other code.
_LOADING_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH")
_INTERPRETER = re.compile(r"python[0-9.]*(\.exe)?", re.IGNORECASE)


def _inert_snippet(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""] if not node.level else [""]
        else:
            modules = []
        if any(m.split(".")[0] not in sys.builtin_module_names for m in modules):
            return False
        if isinstance(node, ast.Name) and node.id in _LOADING_NAMES:
            return False
        if isinstance(node, ast.Attribute) and node.attr in _LOADING_NAMES:
            return False
    return True


def _inert_probe(event: str, args: tuple) -> bool:
    """Whether a subprocess cannot run project code: a Python interpreter
    (by name, ``python3.13``, ``python.exe``) running nothing but a ``-c``
    snippet that imports only built-in modules and names nothing that loads
    code, with no environment pointing it at other code (``python -c
    "import sys; print(sys.version_info)"``, how tools probe an interpreter's
    version); or one of ``_INERT_QUERIES`` (``uv python list``). Anything
    else (``-m``, a script, another program, ``os.system``) may run project
    code where this process cannot see it."""
    executable = env = None
    if event == "subprocess.Popen":
        executable, argv = args[0], args[1] if len(args) > 1 else None
        env = args[3] if len(args) > 3 else None
    elif event in ("os.posix_spawn", "os.exec"):
        argv = args[1] if len(args) > 1 else None
        env = args[2] if len(args) > 2 else None
    else:
        return False
    if isinstance(argv, str) and os.name == "nt":
        # Windows raises the event with the command line ``list2cmdline``
        # built. An escaped quote is beyond this simple split: not inert.
        if '\\"' in argv:
            return False
        argv = [
            a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a
            for a in shlex.split(argv, posix=False)
        ]
    if isinstance(argv, (str, bytes)) or argv is None:
        return False
    try:
        argv = [os.fsdecode(a) for a in argv]
    except TypeError:
        return False
    if not argv:
        return False
    program = argv[0]
    name = os.path.basename(program)
    # ``shutil.which("uv")`` is ``...\\uv.EXE`` on Windows.
    tool_name = name.lower().removesuffix(".exe")
    try:
        runs = os.path.basename(os.fsdecode(executable)) if executable is not None else name
    except TypeError:
        return False
    if runs.lower().removesuffix(".exe") == tool_name and any(
        tool_name == tool and tuple(argv[1 : 1 + len(start)]) == start
        for tool, start in _INERT_QUERIES
    ):
        return True
    if not (_INTERPRETER.fullmatch(name) or program == sys.executable):
        return False
    if not _INTERPRETER.fullmatch(runs) and executable != sys.executable:
        return False
    if env is not None:
        try:
            if any(k in env for k in _LOADING_ENV):
                return False
        except TypeError:
            return False
    i = 1
    while i < len(argv) and argv[i] in _PLAIN_OPTIONS:
        i += 1
    # ``-c CODE`` and nothing after it (an argument could be a path the
    # snippet puts on ``sys.path``).
    if len(argv) != i + 2 or argv[i] != "-c":
        return False
    return _inert_snippet(argv[i + 1])


def _credit_running_threads(window: dict) -> None:
    """Credit ``window`` with the project code every other thread is in the
    middle of. A long-lived thread looping in one frame raises no event while
    a later window is open, yet it runs beside that window's test."""
    me = threading.get_ident()
    for ident, frame in sys._current_frames().items():
        if ident == me:
            continue
        while frame is not None:
            code = frame.f_code
            rel = _code_path(code.co_filename)
            if rel is not None:
                i = codes.get(code)
                if i is None:
                    i = codes[code] = len(table)
                    table.append([rel, code.co_firstlineno, code.co_qualname])
                window["codes"].add(i)
            frame = frame.f_back


def _flag_multiprocessing_spawns() -> None:
    """``multiprocessing``'s spawn and forkserver start methods launch their
    children through ``_posixsubprocess.fork_exec``, which raises no audit
    event: flag the test from the function they all go through. A
    subinterpreter (``concurrent.interpreters``, Python 3.14) raises none
    either, and runs code this interpreter's monitoring does not see."""
    for module, name in (("multiprocessing.util", "spawnv_passfds"), ("_interpreters", "create")):
        try:
            _flag_calls(importlib.import_module(module), name)
        except ImportError:  # not on this platform or version
            pass


def _flag_calls(module, name: str) -> None:
    original = getattr(module, name, None)
    if original is None or getattr(original, "__diffcone__", False):
        return

    @functools.wraps(original)
    def flagged(*args, **kwargs):
        for w in _windows():
            w["flags"] |= FLAG_SUBPROCESS
        return original(*args, **kwargs)

    setattr(flagged, "__diffcone__", True)  # noqa: B010
    setattr(module, name, flagged)


def _is_source(path) -> bool:
    try:
        return os.fsdecode(os.fspath(path)).endswith(".py")
    except TypeError:
        return False


def _wrap_stat(original):
    @functools.wraps(original)
    def stat(path, *args, **kwargs):
        if recording:
            try:
                _touch(path, own_source=_is_source(path))
            except Exception as exc:
                _error("stat", exc)
        return original(path, *args, **kwargs)

    # shutil decides at import whether it can use fd-based functions by
    # checking membership in these sets; keep the answer the same.
    for supported in (
        os.supports_dir_fd,
        os.supports_fd,
        os.supports_follow_symlinks,
        os.supports_effective_ids,
    ):
        if original in supported:
            supported.add(stat)
    return stat


# --------------------------------------------------------------------------- caches


def _project_function(func) -> bool:
    code = getattr(getattr(func, "__wrapped__", None), "__code__", None)
    return code is not None and _relative(code.co_filename) is not None


_lru_cache = functools.lru_cache


def _tracking_lru_cache(*args, **kwargs):
    """``functools.lru_cache`` that remembers the caches it makes for project
    code, so caches created after collection (lazy imports) are cleared too.
    Only decoration goes through here; calls to a cached function do not."""
    made = _lru_cache(*args, **kwargs)
    if isinstance(made, functools._lru_cache_wrapper):
        if _project_function(made):
            caches.append(made)
        return made

    def decorate(func):
        wrapper = made(func)
        if _project_function(wrapper):
            caches.append(wrapper)
        return wrapper

    return decorate


# --------------------------------------------------------------------------- writer


def fold_nodeid(nodeid: str) -> str:
    """Drop the parameter case, exactly as ``execution.fold_nodeid`` does."""
    return re.sub(r"\[.*\]$", "", nodeid)


class _Writer:
    def __init__(self, directory: str) -> None:
        self.f = open(os.path.join(directory, f"tests-{os.getpid()}.bin"), "wb")
        self.current: str | None = None
        self.acc = _window()

    def add(self, test_id: str, w: dict) -> None:
        if test_id != self.current:
            self.flush()
            self.current = test_id
        self.acc["codes"] |= w["codes"]
        self.acc["paths"] |= w["paths"]
        self.acc["dirs"] |= w["dirs"]
        self.acc["flags"] |= w["flags"]
        self.acc["fixtures"] |= w["fixtures"]

    def flush(self) -> None:
        if self.current is None:
            return
        payload = json.dumps(
            {
                "codes": sorted(self.acc["codes"]),
                "paths": sorted(self.acc["paths"]),
                "dirs": sorted(self.acc["dirs"]),
                "flags": self.acc["flags"],
                "fixtures": sorted(self.acc["fixtures"]),
            }
        ).encode()
        name = self.current.encode()
        blob = zlib.compress(payload, 6)
        self.f.write(struct.pack("<II", len(name), len(blob)) + name + blob)
        self.current, self.acc = None, _window()

    def close(self) -> None:
        self.flush()
        self.f.close()


writer: _Writer | None = None


# --------------------------------------------------------------------------- start


def _start() -> None:
    """Start at import of this plugin: ``-p`` plugins load before the initial
    conftests, which usually import the project, so its import-time code is
    seen."""
    global recording, environment_at_start
    # Compared with the environment at the end: a test that installs a
    # distribution changes what the recording is keyed by.
    try:
        environment_at_start = environment()
    except Exception as exc:
        _error("environment", exc)
    if mon is None:
        errors.append(f"Python {sys.version.split()[0]} has no sys.monitoring; 3.12+ is needed")
        return
    try:
        mon.use_tool_id(TOOL, "diffcone")
    except ValueError as exc:
        errors.append(f"sys.monitoring tool id {TOOL} is taken ({exc}); nothing was recorded")
        return
    mon.register_callback(TOOL, mon.events.PY_START, _on_start)
    mon.register_callback(TOOL, mon.events.PY_RESUME, _on_resume)
    mon.register_callback(TOOL, mon.events.PY_RETURN, _on_return)
    mon.register_callback(TOOL, mon.events.PY_UNWIND, _on_unwind)
    mon.register_callback(TOOL, mon.events.PY_THROW, _on_throw)
    mon.set_events(
        TOOL,
        mon.events.PY_START | mon.events.PY_RESUME | mon.events.PY_UNWIND | mon.events.PY_THROW,
    )
    sys.addaudithook(_audit)
    os.stat = _wrap_stat(os.stat)
    os.lstat = _wrap_stat(os.lstat)
    os.access = _wrap_stat(os.access)
    if os.name == "nt":
        # Python 3.12+ on Windows answers these without os.stat.
        for name in ("exists", "lexists", "isfile", "isdir", "islink"):
            setattr(os.path, name, _wrap_stat(getattr(os.path, name)))
    # functools.cache goes through it too; a deliberate patch of the stdlib.
    functools.lru_cache = _tracking_lru_cache  # ty: ignore[invalid-assignment]
    _flag_multiprocessing_spawns()
    recording = True


# --------------------------------------------------------------------------- hooks


def pytest_configure(config):
    global writer
    expected = os.environ.get("DIFFCONE_CHECK_ENV")
    if expected:
        env = environment()
        match = environment_hash(env) == expected
        # Written whether or not it matches: a run without a report was not
        # checked, and diffcone does not trust its evidence plan.
        report = os.environ.get("DIFFCONE_CHECK_REPORT")
        if report:
            with open(report, "w") as f:
                json.dump({"match": match, "environment": env}, f)
        if not match:
            pytest.exit(
                "diffcone: the environment differs from the one the evidence was recorded in",
                returncode=4,
            )
        if os.environ.get("DIFFCONE_CHECK_ONLY"):
            pytest.exit("diffcone: the environment matches the evidence", returncode=0)
    if OUT is not None:
        try:
            os.makedirs(OUT, exist_ok=True)
            writer = _Writer(OUT)
        except Exception as exc:
            _error("configure", exc)


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(session, config, items):
    # Every test pytest collected, before any plugin (``-m``, ``-k``, the
    # selection plugin) deselects: it settles whether discovery's target list
    # is short of the real collection (roadmap item 9).
    global collection_ran
    if recording:
        try:
            collected.update(fold_nodeid(item.nodeid) for item in items)
            collection_ran = True
        except Exception as exc:
            _error("collection_modifyitems", exc)
    result = yield
    # After every other plugin has ordered them.
    if os.environ.get("DIFFCONE_COLLECT_REVERSE"):
        items.reverse()
    return result


def pytest_collection_finish(session):
    if not recording:
        return
    try:
        # Caches made before this plugin loaded; later ones register themselves.
        known = {id(c) for c in caches}
        caches.extend(
            o
            for o in gc.get_objects()
            if isinstance(o, functools._lru_cache_wrapper)
            and id(o) not in known
            and _project_function(o)
        )
    except Exception as exc:
        _error("collection_finish", exc)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if not recording or fixturedef.scope == "function":
        return (yield)
    key = (fixturedef.baseid, fixturedef.argname, fixturedef.scope)
    w = fixture_windows.setdefault(key, _window())
    active.append(w)
    try:
        _credit_running_threads(w)
    except Exception as exc:
        _error("threads", exc)
    # Code this test already ran is disabled; re-arm so the fixture's own
    # window sees everything its setup runs.
    mon.restart_events()
    try:
        return (yield)
    finally:
        _drop(w)


def fixture_key(key: tuple[str, str, str]) -> str:
    return "\x1f".join(key)


def _drop(window: dict) -> None:
    for i in range(len(active) - 1, -1, -1):
        if active[i] is window:
            del active[i]
            return


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    # Every fixture the test activated, ``request.getfixturevalue`` included;
    # pytest drops the request when the protocol ends, so read it now.
    if not recording:
        return
    try:
        request = getattr(item, "_request", None)
        if request is not None:
            used_fixtures[item.nodeid] = list(request._fixture_defs.values())
    except Exception as exc:
        _error("runtest_teardown", exc)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not recording:
        return (yield)
    for cache in caches:
        try:
            cache.cache_clear()
        except Exception:
            pass
    mine = _window()
    active.append(mine)
    importing.clear()  # no import is running when a test starts
    try:
        _credit_running_threads(mine)
    except Exception as exc:
        _error("threads", exc)
    mon.restart_events()
    try:
        return (yield)
    finally:
        _drop(mine)
        mon.restart_events()
        try:
            defs = list(used_fixtures.pop(item.nodeid, ()))
            info = getattr(item, "_fixtureinfo", None)
            for name in getattr(item, "fixturenames", ()):
                if info is not None:
                    defs.extend(info.name2fixturedefs.get(name, ()))
            for fixturedef in defs:
                if fixturedef.scope != "function":
                    key = (fixturedef.baseid, fixturedef.argname, fixturedef.scope)
                    mine["fixtures"].add(fixture_key(key))
                    shared = fixture_windows.get(key)
                    if shared:
                        mine["codes"] |= shared["codes"]
                        mine["paths"] |= shared["paths"]
                        mine["dirs"] |= shared["dirs"]
                        mine["flags"] |= shared["flags"]
            if writer is not None:
                writer.add(fold_nodeid(item.nodeid), mine)
        except Exception as exc:
            _error("runtest_protocol", exc)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    # After pytest's own teardown of session fixtures (an ordinary hook), and
    # before pytest-xdist's wrapper reports the worker finished: the
    # controller then gives each worker ten seconds to exit and kills the
    # rest, so a record written at unconfigure could be cut short on a
    # loaded machine. What runs later (terminal summary, unconfigure) runs
    # after every test and is not recorded.
    _finish()


def pytest_unconfigure(config):
    _finish()  # a session that never started (usage error) still reports


def _finish():
    global finished
    if OUT is None or finished:
        return
    finished = True
    if recording:
        mon.set_events(TOOL, 0)
        for event in (
            mon.events.PY_START,
            mon.events.PY_RESUME,
            mon.events.PY_RETURN,
            mon.events.PY_UNWIND,
            mon.events.PY_THROW,
        ):
            mon.register_callback(TOOL, event, None)
        mon.free_tool_id(TOOL)
    try:
        if writer is not None:
            writer.close()
    except Exception as exc:
        _error("close", exc)
    outside = []
    for name, module in list(sys.modules.items()):
        if name.split(".", 1)[0] not in PACKAGES:
            continue
        file = getattr(module, "__file__", None)
        if file and _relative(os.path.abspath(file)) is None:
            outside.append([name, file])
    env = environment()
    record = {
        "pid": os.getpid(),
        "table": table,
        "import_phase": sorted(import_window["codes"]),
        "import_paths": {p: sorted(m) for p, m in sorted(import_paths.items())},
        "import_dirs": {p: sorted(m) for p, m in sorted(import_dirs.items())},
        "import_flags": import_window["flags"],
        "import_by": {str(k): sorted(v) for k, v in import_by.items()},
        "hook_phase": sorted(hook_codes),
        "outside_modules": sorted(outside),
        # What each shared fixture ran here: a fixture computed once for
        # every worker (a file lock and a cache file) runs its code in one.
        "fixtures": {
            fixture_key(key): {
                "codes": sorted(w["codes"]),
                "paths": sorted(w["paths"]),
                "dirs": sorted(w["dirs"]),
                "flags": w["flags"],
            }
            for key, w in sorted(fixture_windows.items())
        },
        "environment": env,
        "environment_hash": environment_hash(env),
        "environment_at_start": environment_at_start,
        "wrote_tests": writer is not None,
        "collected": sorted(collected) if collection_ran else None,
        "errors": errors,
    }
    with open(os.path.join(OUT, f"process-{os.getpid()}.json"), "w") as f:
        json.dump(record, f)


if OUT is not None:
    _start()

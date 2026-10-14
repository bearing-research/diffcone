"""pytest plugin that records execution evidence.

Loaded as ``-p diffcone_collect``: ``diffcone`` links this file into a
directory of its own on ``PYTHONPATH``, so it imports nothing of diffcone and
needs nothing but the standard library and pytest. The ``sitecustomize``
beside it imports this module as the recorded process starts
(``DIFFCONE_COLLECT_MAIN``), before any ``-p`` plugin of ``addopts``,
``PYTEST_ADDOPTS`` or the command line and before a project's own
``sitecustomize`` (chained after it): what those import is recorded as
import-time code. pytest then finds it imported (PYTEST_DONT_REWRITE: it
asserts nothing, so no rewriting is missed). The modules of the checkout
imported before that all the same (a ``.pth`` file; a ``-p`` plugin when
``sitecustomize`` could not run: ``python -S``) are recorded as
``preloaded``: their code ran where nothing saw it, so a change to one is
planned from the code.

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
* the repository paths it opened, ``stat``ed, ``access``ed, ``readlink``ed
  or listed (audit hooks, ``sqlite3.connect`` (a ``file:`` URI parsed to
  its path) and ``ctypes.dlopen`` included, and wrappers of
  ``os.stat``/``os.lstat``/``os.fstat``/``os.access``/``os.readlink``,
  which never warn, so the extra frame cannot move a warning's
  ``stacklevel``), with ``/`` separators, and each beside its real path
  when a symlink is on the way (``child.RealPaths``: a tracked link, or a
  linked directory, to the file really read); on a file system that folds
  case (``case_insensitive``) the fold respells them as the index does. A
  path relative to a directory file descriptor (``dir_fd=``, a listing of
  an fd, ``os.fstat``) is resolved through the descriptors ``os.open``
  returned (``child.fds``, checked against the file they name); one the
  recorder cannot resolve flags the test (``FLAG_SUBPROCESS``: it read
  what no record names). A directory listed with ``os.scandir`` is also
  recorded as scanned (``child.SCANNED``), since the entries' ``stat()``
  and type checks raise no event. A touch by the import system finding
  modules is not the test's; one by a library's top-level code that an
  import ran (matplotlib reading ``./matplotlibrc``) is;
* the processes it started through ``subprocess.Popen`` (POSIX), which
  record themselves (``child.py``, roadmap item 15), inert interpreter
  probes included, and every such process still running when it opens;
  the fold credits it with what they ran;
* whether it started a process another way (``os.system``, Windows,
  ``multiprocessing``; other than a ``python -c`` probe that can run no
  project code and could not record) or a subinterpreter, or opened while
  such a process (a pool's worker, a forkserver's child) was still
  running (``unfollowed``): code no record names runs there;
* whether it ran code compiled from text no file of the checkout holds (a
  doctest's examples, a ``timeit`` statement; ``child.text_code``). The
  project code other threads are in the middle of when a window opens is
  credited to it, as those threads run beside its test.

Outside every test window it records the code and paths of imports,
collection and hooks; for each code object run by an import, the innermost
module whose top-level code was running (``import_by``); for code run while
pytest made the collect report of a test module or class (a
``pytest_make_collect_report`` wrapper keeps the stack, ``collecting``)
with no import running, the checkout file collected (``collect_by``:
parameter ids, ``ids=`` callables, ``pytest_generate_tests`` for that
module); and the code that ran outside every window, every import and
every such collector (``hook_phase``: session hooks,
``pytest_collection_modifyitems``, a directory's or package's collection).
A process started there is part of that phase (``import_spawns``, folded
into it), and what no record shows there flags the phase
(``import_flagged``: the module being imported, "" for none, while a
collector is on the stack too). A path project code touches while a collector is on the stack
is credited to it, as ``/<file>`` among the modules of ``import_paths``
and ``import_dirs`` (a checkout-relative path never starts with ``/``).
``functools`` caches of project code are cleared before each test so a
value one test computed is recomputed, and recorded, by the next.

Environment variables:

``DIFFCONE_COLLECT_OUT``       directory for the raw records; recording is
                               off without it
``DIFFCONE_COLLECT_MAIN``      set on the recorded process itself: its
                               ``sitecustomize`` starts the recorder, which
                               removes the variable so no child inherits it
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
``DIFFCONE_CACHE_DIR``         an empty directory for pytest's cache plugin
                               instead of the project's ``.pytest_cache``: a
                               recording keeps the plugin (``addopts`` may
                               carry ``--failed-first``, which pytest refuses
                               without it) but ``--lf``/``--ff`` find nothing
                               to narrow or reorder the run by (as in
                               ``selection.py``)

Files written per process: ``process-<pid>.json`` (code table, import-time
records, preloaded modules, environment, errors) and ``tests-<pid>.bin``
(length-prefixed zlib-compressed JSON records, one per run of consecutive
items of a test).
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
import stat
import struct
import subprocess
import sys
import threading
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

try:
    import diffcone_child as child  # ty: ignore[unresolved-import]
except ImportError:  # imported as diffcone.collect, not as the plugin
    from diffcone import child

# Shared with the child recorder (child.py), which records the processes a
# test starts the way this plugin records the test's own.
TOOL = child.TOOL
FLAG_SUBPROCESS = 1
# The test ran code compiled from text no file of the checkout holds (a
# doctest, ``timeit``), which can read any name of the project.
FLAG_TEXT = 4
# The test failed or errored when recorded: its record stops where it failed.
FLAG_FAILED = 8
# ``VIRTUAL_ENV``: a library may act on it (IPython warns when it names
# another environment than the interpreter's, which a suite treating
# warnings as errors fails on), and a tool run from its own environment
# (``uv run`` elsewhere) hands its own to the suite.
ENV_VARIABLES = ("PYTHONHASHSEED", "TZ", "LANG", "LC_ALL", "PYTHONWARNINGS", "VIRTUAL_ENV")


def _variables() -> tuple[str, ...]:
    """The variables the environment includes: these, and the project's own
    (``DIFFCONE_ENV_VARIABLES``, from ``collect --env-var``)."""
    extra = {v for v in os.environ.get("DIFFCONE_ENV_VARIABLES", "").split(",") if v}
    return ENV_VARIABLES + tuple(sorted(extra - set(ENV_VARIABLES)))


SUBPROCESS_EVENTS = child.SUBPROCESS_EVENTS
C_OPEN_EVENTS = child.C_OPEN_EVENTS
LISTING_EVENTS = child.LISTING_EVENTS


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
CACHE_DIR = os.environ.get("DIFFCONE_CACHE_DIR")
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


def _case_folds(root: str) -> bool:
    """Whether the checkout's file system folds case (macOS and Windows by
    default): a path opened as ``Data/X.TXT`` is then ``data/x.txt``, which
    the fold matches against the index's spelling."""
    swapped = root.swapcase()
    if swapped == root:
        return False
    try:
        return os.path.samefile(root, swapped)
    except (OSError, ValueError):
        return False


CASE_FOLDS = _case_folds(os.path.realpath(_root))
IGNORED_DIRS = child.IGNORED_DIRS
INSTALLED = child.INSTALLED


ENVIRONMENTS = child.environments(ROOTS)

errors: list[str] = []
codes: dict[object, int] = {}
table: list[list] = []
active: list[dict] = []  # open windows, innermost last
importing: list[str] = []  # modules whose top-level code is running, innermost last
import_by: dict[int, set[str]] = {}
# The collectors pytest is making a collect report of, innermost last: the
# checkout file of a test module or class, None for any other collector (the
# session, a directory, a package, a file of another kind), which credits
# what runs then to no collector (the hook phase).
collecting: list[str | None] = []
collect_by: dict[int, set[str]] = {}  # ran while collecting these files, no import running
finished = False  # the process record is written
# Ran outside every test window with no import running and no test module
# or class being collected.
hook_codes: set[int] = set()
# Opened or stat'ed, and listed, by project code outside every test window.
import_paths: dict[str, set[str]] = {}
import_dirs: dict[str, set[str]] = {}
fixture_windows: dict[tuple[str, str, str], dict] = {}
collected: set[str] = set()  # every test collected, parameters folded
collection_ran = False  # this process collected (an xdist controller does not)
used_fixtures: dict[str, list] = {}
live: list[tuple] = []  # accounted spawns not yet known to have ended
# Processes no record follows that may outlive the window that started them
# (a ``multiprocessing`` pool or forkserver, a fork, Windows): every window
# opening while one runs is flagged, since work submitted to it then runs
# where nothing records it.
unfollowed: list[int] = []
known: set[int] = set()  # pids followed, or started where they were flagged
accounted: dict[int, int] = {}  # pid -> when an accounted spawn started it
records_seen: set[str] = set()  # this process's children's records, looked at
# Accounted spawns outside every test window, with the module being imported
# then ("" for none: a hook, collection): they ran as part of that phase.
import_spawns: list[tuple[tuple, str]] = []
# The modules whose import ran what no record shows ("" for none).
import_flagged: set[str] = set()
flags_raised = 0  # how many times _flag ran: did a call flag its window?
caches: list = []
recording = False
# The checkout files of the modules imported before the recorder started.
preloaded: list[str] = []
_real = child.RealPaths()


# A window's sets; ``flags`` is a bit mask beside them. ``fixtures``: the
# non-function-scoped fixtures a test used, by key, so a fixture another
# process (an xdist worker) set up is credited too. ``spawns``: (spawner,
# pid, start, argv, inert) of the processes it started, or that were still
# running when it opened, through ``subprocess.Popen``.
_SETS = ("codes", "paths", "dirs", "fixtures", "spawns")


def _window() -> dict:
    window: dict = {key: set() for key in _SETS}
    window["flags"] = 0
    return window


import_window = _window()


def _windows():
    return active if active else (import_window,)


def _merge(into: dict, window: dict) -> None:
    """Add what ``window`` recorded to ``into``: every field, so none is
    lost on the way (a dropped field is a record missing from the store)."""
    for key in _SETS:
        into[key] |= window[key]
    into["flags"] |= window["flags"]


def _serialised(window: dict) -> dict:
    """A window as the records hold it (``tests-<pid>.bin``, and a shared
    fixture's in ``process-<pid>.json``): every field."""
    data: dict = {key: sorted(window[key]) for key in _SETS if key != "spawns"}
    data["spawns"] = _spawn_list(window["spawns"])
    data["flags"] = window["flags"]
    return data


def _credit_rows(rows: list, paths: list) -> None:
    """Credit what an annotation evaluated from text read
    (``child.annotation_reads``): code rows as code that ran now, where it
    ran (a window, an import, a hook), and paths as read."""
    for row in rows:
        i = codes.get(row)
        if i is None:
            i = codes[row] = len(table)
            table.append(list(row))
        for w in _windows():
            w["codes"].add(i)
        if not active:
            if importing:
                import_by.setdefault(i, set()).add(importing[-1])
            else:
                _outside(i)
    for rel in paths:
        _touch(os.path.join(_root, rel))


def _outside(i: int) -> None:
    """Code ``i`` ran outside every test window with no import running:
    while pytest collected a test module or class (``collect_by``), or in
    the hook phase."""
    where = collecting[-1] if collecting else None
    if where:
        collect_by.setdefault(i, set()).add(where)
    else:
        hook_codes.add(i)


def _phase() -> str:
    """The phase a path touched outside every test window belongs to: the
    module being imported, ``/<file>`` for a collector, "" for none."""
    if importing:
        return importing[-1]
    where = collecting[-1] if collecting else None
    return "/" + where if where else ""


def _code_index(code, rel: str) -> int:
    """``code``'s row in the code table, added on first sight (``rel``: its
    checkout-relative file)."""
    i = codes.get(code)
    if i is None:
        i = codes[code] = len(table)
        table.append([rel, code.co_firstlineno, code.co_qualname])
    return i


def _flag(flag: int = FLAG_SUBPROCESS) -> None:
    """Something ran that no record shows: in the open windows, or outside
    every window in the phase it belongs to (the module being imported, or
    none: a hook or collection)."""
    global flags_raised
    flags_raised += 1
    for w in _windows():
        w["flags"] |= flag
    if not active:
        import_flagged.add(importing[-1] if importing else "")


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
            if (
                rel.startswith(IGNORED_DIRS)
                or folded[len(root) :].startswith(ENVIRONMENTS)
                or any(p in rel for p in INSTALLED)
            ):
                return None
            # Records use ``/`` on every platform, as the index does.
            return rel.replace(os.sep, "/") if os.sep != "/" else rel
    return None


CYTHON_SUFFIXES = child.CYTHON_SUFFIXES


_code_paths: dict[str, str | None] = {}
_os_stat = os.stat  # unwrapped: the recorder's own checks touch nothing


def _is_file(path: str) -> bool:
    try:
        return stat.S_ISREG(_os_stat(path).st_mode)
    except (OSError, ValueError):
        return False


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
        if not _is_file(path):
            path = None
    elif not os.path.isabs(name) and not name.startswith("<"):
        # Compiled under a relative name (``compile(text, "pkg/x.py",
        # "exec")``): the file of that name in the working directory.
        path = os.path.abspath(name)
        if not _is_file(path):
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
        i = _code_index(code, rel)
        for w in _windows():
            w["codes"].add(i)
        if not active and not importing and code.co_name != "<module>":
            _outside(i)
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
        i = _code_index(code, rel)
        for w in _windows():
            w["codes"].add(i)
        if not active and not importing and collecting and collecting[-1]:
            collect_by.setdefault(i, set()).add(collecting[-1])
    except Exception as exc:
        _error("PY_RESUME", exc)
    return mon.DISABLE


def _on_throw(code, offset, exc):
    # A generator or coroutine re-entered by ``.throw()`` or ``.close()``:
    # resumed like PY_RESUME, but this event cannot be disabled.
    try:
        i = codes.get(code)
        if i is None:
            rel = _code_path(code.co_filename)
            if rel is None:
                return
            i = _code_index(code, rel)
        for w in _windows():
            w["codes"].add(i)
        if not active and not importing and collecting and collecting[-1]:
            collect_by.setdefault(i, set()).add(collecting[-1])
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


DATA_READERS = child.DATA_READERS


def _actor() -> str:
    """Who touched a path: ``import`` when the import system did (it finds
    modules, which the index covers), ``project`` when project code is on
    the stack above it, ``other`` for pytest or a library acting alone
    (collection walks and stats every directory and file).

    A library's top-level code run by an import (matplotlib reading
    ``./matplotlibrc``) is not the import system: the touch belongs to
    whoever imported the library, ``project`` when project code is above,
    else ``library``, which outside every test counts as a hook's touch.
    ``tracer`` when a coverage tracer resolved the file it is about to
    measure (``child.tracer``): no one's read, in a test or out."""
    frame = sys._getframe(2)
    module = imported = False
    while frame is not None:
        code = frame.f_code
        name = code.co_filename
        if child.tracer(code):
            return "tracer"
        if name.startswith("<frozen importlib") and code.co_name not in DATA_READERS:
            if not module:
                return "import"
            # The import of the library whose top level touched it.
            imported = True
        elif _relative(name) is not None:
            return "project"
        elif code.co_name == "<module>":
            module = True
        frame = frame.f_back
    return "library" if imported else "other"


METADATA_MODULES = child.METADATA_MODULES
_search_path: tuple[list[str], frozenset[str]] = ([], frozenset())


def _metadata_scan(path: str) -> bool:
    """A stat or listing of a ``sys.path`` entry by ``importlib.metadata``
    (``distributions()``, ``version()``, pluggy's entry points) or
    ``pkg_resources`` (its working set, built as it is imported), below any
    project frame. It learns only which ``*.dist-info``, ``*.egg-info``,
    ``*.egg`` and ``*.egg-link`` names the directory holds, and a change to
    one of those selects everything (``planner.build_input``); its other
    names are not seen."""
    global _search_path
    if _search_path[0] != sys.path:
        entries = list(sys.path)
        _search_path = (entries, frozenset(os.path.abspath(e or ".") for e in entries))
    if path not in _search_path[1]:
        return False
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_globals.get("__name__", "").startswith(METADATA_MODULES):
            return True
        if _relative(frame.f_code.co_filename) is not None:
            return False
        frame = frame.f_back
    return False


def _touch(path, listing: bool = False, own_source: bool = False, scanned: bool = False) -> None:
    """``own_source``: a stat of a source file. While a module is being
    imported, its stat of its *own* file (``Path(__file__).resolve()``)
    sees only that the file exists, recorded as a name seen like a listing;
    every other stat of a source file (``os.path.getsize``, ``linecache``
    checking ``inspect.getsource``'s cached copy) can depend on what the
    file holds and counts as reading it. ``scanned``: an ``os.scandir``
    listing, whose entries' metadata may be read without an event. A
    descriptor (an int) names the file ``os.open`` returned it for
    (``_fd_path``); a listing of one nothing can resolve flags."""
    if path is None or child.quiet.depth:
        return
    if isinstance(path, int):
        path = child.fd_path(path)
        if path is None:
            if listing:
                _flag()
            return
    try:
        path = os.fsdecode(os.fspath(path))
    except TypeError:
        return
    absolute = os.path.abspath(path)
    rel = _relative(absolute)
    if rel is None or _metadata_scan(absolute):
        return
    actor = _actor()
    if actor in ("import", "tracer"):
        return
    if own_source and not active and importing and importing[-1] == rel:
        listing = True
    spellings = {rel}
    real = _relative(_real(absolute))
    if real is not None:
        spellings.add(real)
    if scanned:
        spellings |= {s + child.SCANNED for s in spellings}
    if active:
        # Inside a test anything counts: a library or plugin reading a file
        # for the test (a data-directory fixture copying files) included.
        for w in active:
            w["dirs" if listing else "paths"].update(spellings)
    elif actor in ("project", "library"):
        # Outside every test: project code at import (a parametrize list
        # globbed from a directory), while a test module is collected or in
        # a hook, or a library's top level run by such an import (or by a
        # plugin's: a hook's), credited to the module being imported, the
        # collector (``/<file>``), or "" for none.
        seen = import_dirs if listing else import_paths
        phase = _phase()
        for spelling in spellings:
            seen.setdefault(spelling, set()).add(phase)


_inert_pending = False


def _audit(event, args):
    try:
        if event == "open":
            if args:
                _touch(args[0])
        elif event in LISTING_EVENTS:
            _touch(
                args[0] if args and args[0] is not None else ".",
                listing=True,
                scanned=event == "os.scandir",
            )
        elif event == "sqlite3.connect":
            if args:
                _touch(child.sqlite_path(args[0]))
        elif event in C_OPEN_EVENTS:
            if args and isinstance(args[0], (str, bytes, os.PathLike)):
                _touch(args[0])
        elif event in child.LINK_EVENTS:
            _real.clear()
        elif event in SUBPROCESS_EVENTS:
            global _inert_pending
            if child.in_popen(event, args):
                # Accounted when it has started (_spawned).
                if event == "subprocess.Popen" and _inert_probe(event, args):
                    child.popen.inert = True
            elif event == "_winapi.CreateProcess" and _inert_pending:
                # The process an inert ``subprocess.Popen`` just judged
                # (Windows raises both events for one process).
                _inert_pending = False
            elif _inert_probe(event, args):
                _inert_pending = event == "subprocess.Popen"
            else:
                _inert_pending = False
                _flag()
        elif event == "exec":
            caller = sys._getframe().f_back
            # An annotation evaluated from text: credited with what it reads
            # (child.annotation_reads); other text a doctest or ``timeit``
            # runs flags the test (child.text_code).
            reads = child.annotation_reads(args[0], caller, _code_path) if args else None
            if reads is not None:
                _credit_rows(*reads)
            elif args and child.text_code(args[0], caller, _code_path):
                _flag(FLAG_TEXT)
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
# runs code by other means (``child.LOADING_NAMES``). On POSIX an inert
# probe is accounted anyway and judged by its own record: this list vouches
# only for one that cannot record (``python -I``), and on Windows, where no
# child records.
_LOADING_NAMES = child.LOADING_NAMES
# Environment variables that make an interpreter run or find other code.
_LOADING_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH")
_INTERPRETER = child.INTERPRETER


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
                window["codes"].add(_code_index(code, rel))
            frame = frame.f_back


def _spawned(pid: int, start: int, argv: list[str] | None, inert: bool = False) -> None:
    """A process ``subprocess.Popen`` started: the open windows hold it, and
    so does every window opened while it runs. Outside every test it is
    part of the phase that started it (an import, a hook), whose record it
    joins. ``inert``: an interpreter probe that can run no project code by
    its command line; its record decides, and one that could not record
    (``python -I``) is taken at its word."""
    try:
        spawn = (os.getpid(), pid, start, tuple(argv) if argv is not None else None, inert)
        if active:
            for w in active:
                w["spawns"].add(spawn)
        else:
            import_spawns.append((spawn, importing[-1] if importing else ""))
        live.append(spawn)
        accounted[pid] = start
    except Exception as exc:
        _error("spawn", exc)


def _credit_live_children(window: dict) -> None:
    """Credit ``window`` with the accounted processes still running: a
    server or a warm worker started earlier serves this test too. One no
    record follows (``unfollowed``) flags it instead."""
    if unfollowed:
        running = [pid for pid in unfollowed if _running(pid)]
        if running:
            window["flags"] |= FLAG_SUBPROCESS
        unfollowed[:] = running
    if not live or OUT is None:
        return
    still = []
    for spawn in live:
        spawner, pid, start, argv, inert = spawn
        argv_list = list(argv) if argv else None
        if child.tree_alive(Path(OUT), spawner, pid, start, argv_list, inert=inert):
            still.append(spawn)
            window["spawns"].add(spawn)
    live[:] = still


def _running(pid: int) -> bool:
    """Whether a process this one started has not ended. A zombie has ended
    (``waitid`` with ``WNOWAIT`` leaves it for its owner to reap); one this
    process cannot wait for counts as running while its pid answers (as
    does a zombie before Python 3.13 on macOS, which has no ``waitid``)."""
    if os.name == "nt":
        import _winapi

        try:
            handle = _winapi.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        except OSError:
            return False
        try:
            return _winapi.GetExitCodeProcess(handle) == 259  # STILL_ACTIVE
        finally:
            _winapi.CloseHandle(handle)
    waitid = getattr(os, "waitid", None)
    if waitid is not None:
        try:
            return waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
        except OSError:
            pass
    return child.alive(pid)


def _flag_multiprocessing_spawns() -> None:
    """``multiprocessing``'s spawn and forkserver start methods launch their
    children through ``_posixsubprocess.fork_exec``, which raises no audit
    event: flag the test from the function they all go through. A
    subinterpreter (``concurrent.interpreters``, Python 3.14) raises none
    either, and runs code this interpreter's monitoring does not see."""
    for module, name in child.UNAUDITED_STARTS:
        try:
            _flag_calls(importlib.import_module(module), name)
        except ImportError:  # not on this platform or version
            pass
    # A process started another way than an accounted ``subprocess.Popen``
    # may outlive its test and serve a later one: a pool's workers, or a
    # forkserver's children, which start with no event at all once the
    # server runs. Followed by pid (``unfollowed``).
    try:
        process = importlib.import_module("multiprocessing.process")
        _follow_pid(process.BaseProcess, "start", lambda result, self, *a: self.pid)
    except ImportError:
        pass
    _follow_pid(os, "fork", lambda result, *a: result)
    _follow_pid(os, "forkpty", lambda result, *a: result[0])
    for name in ("posix_spawn", "posix_spawnp"):
        # Inside ``subprocess.Popen`` it is accounted.
        _follow_pid(os, name, lambda result, *a: None if child.popen.depth > 0 else result)
    if os.name == "nt":
        # Windows accounts no spawn: a flagged child may outlive its test.
        _follow_pid(
            subprocess.Popen, "_execute_child", lambda result, self, *a: self.pid, flagged=True
        )


def _follow_pid(owner, name: str, pid_of, flagged: bool = False) -> None:
    """Wrap ``owner.name`` to follow the process it started, ``pid_of(result,
    *args)``; with ``flagged``, only when the call flagged its window."""
    original = getattr(owner, name, None)
    if original is None or getattr(original, "__diffcone__", False):
        return

    @functools.wraps(original)
    def following(*args, **kwargs):
        before = flags_raised
        result = original(*args, **kwargs)
        if recording and (not flagged or flags_raised != before):
            try:
                pid = pid_of(result, *args)
                if isinstance(pid, int) and pid > 0:
                    known.add(pid)
                    unfollowed.append(pid)
            except Exception as exc:
                _error(f"follow {name}", exc)
        return result

    setattr(following, "__diffcone__", True)  # noqa: B010
    setattr(owner, name, following)


def _flag_calls(module, name: str) -> None:
    original = getattr(module, name, None)
    if original is None or getattr(original, "__diffcone__", False):
        return

    @functools.wraps(original)
    def flagged(*args, **kwargs):
        _flag()
        result = original(*args, **kwargs)
        if isinstance(result, int):
            # A forkserver or resource tracker: flagged here, and the
            # processes the forkserver forks are followed by pid.
            known.add(result)
        return result

    setattr(flagged, "__diffcone__", True)  # noqa: B010
    setattr(module, name, flagged)


def _unaccounted_children() -> None:
    """A Python process this one started some way no wrapper sees (loky's
    workers start through ``_posixsubprocess`` directly) records itself all
    the same (``child.py``). A record no accounted spawn, followed or known
    pid explains is one: found as a window opens or closes, it started in
    the windows open since the last look, or outside every window, so it
    flags them (``_flag``), and it is followed by pid from then on."""
    if OUT is None or os.name == "nt":
        return
    try:
        names = os.listdir(os.path.join(OUT, child.CHILDREN, str(os.getpid())))
    except OSError:
        return
    for name in names:
        if name in records_seen:
            continue
        records_seen.add(name)
        stem = name.removesuffix(child.BROKEN).removesuffix(".jsonl")
        pid_text, _, began = stem.partition("-")
        if not (pid_text.isdigit() and began.isdigit()):
            continue
        pid = int(pid_text)
        spawned = accounted.get(pid)
        if pid in known or (spawned is not None and int(began) >= spawned):
            continue
        known.add(pid)
        unfollowed.append(pid)
        _flag()


def _is_source(path) -> bool:
    try:
        return os.fsdecode(os.fspath(path)).endswith(".py")
    except TypeError:
        return False


def _wrap_stat(original):
    """``os.stat``, ``os.lstat``, ``os.fstat``, ``os.access``,
    ``os.readlink``: a path (or a descriptor) is touched; one relative to a
    directory descriptor is resolved through ``_fds``, and flags the window
    when it cannot be (``_joined``)."""

    @functools.wraps(original)
    def stat(path, *args, **kwargs):
        if recording:
            try:
                dir_fd = kwargs.get("dir_fd")
                if dir_fd is not None:
                    joined = child.joined(path, dir_fd)
                    if joined is None:
                        _flag()
                    else:
                        _touch(joined, own_source=_is_source(joined))
                else:
                    _touch(path, own_source=_is_source(path))
            except Exception as exc:
                _error("stat", exc)
        return original(path, *args, **kwargs)

    # shutil decides at import whether it can use fd-based functions by
    # checking membership in these sets; keep the answer the same.
    child.register_supports(original, stat)
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
        _merge(self.acc, w)

    def flush(self) -> None:
        if self.current is None:
            return
        payload = json.dumps(_serialised(self.acc)).encode()
        name = self.current.encode()
        blob = zlib.compress(payload, 6)
        self.f.write(struct.pack("<II", len(name), len(blob)) + name + blob)
        self.current, self.acc = None, _window()

    def close(self) -> None:
        self.flush()
        self.f.close()


writer: _Writer | None = None
# The window of the test whose protocol is running, for its reports.
_test_window: dict | None = None


def _spawn_list(spawns) -> list:
    return sorted([s, p, t, list(a) if a is not None else None, i] for s, p, t, a, i in spawns)


# --------------------------------------------------------------------------- start


def _preloaded() -> list[str]:
    """The checkout files of the modules already imported: their import-time
    code ran before the recorder (a ``.pth`` file, a ``-p`` plugin loaded
    before this one when ``sitecustomize`` did not run), where nothing saw
    it. A lazily loaded module (``importlib.util.LazyLoader``) loads on any
    ordinary attribute read: its namespace is read directly."""
    found: set[str] = set()
    for module in list(sys.modules.values()):
        try:
            namespace = object.__getattribute__(module, "__dict__")
        except Exception:
            continue
        if not isinstance(namespace, dict):
            continue
        file = namespace.get("__file__")
        if not isinstance(file, str):
            continue
        rel = _relative(os.path.abspath(file))
        if rel is not None:
            found.add(rel)
    return sorted(found)


def _start() -> None:
    """Start at import of this plugin, which the ``sitecustomize`` beside it
    does as the process starts (``DIFFCONE_COLLECT_MAIN``, removed here so
    no process this one starts inherits it); failing that, when pytest
    loads ``-p diffcone_collect``, before the initial conftests. What the
    checkout had imported by then is ``preloaded``."""
    global recording, environment_at_start, preloaded
    os.environ.pop(child.ENV_MAIN, None)
    preloaded = _preloaded()
    if os.name != "nt":
        # An xdist worker is a process a recorded run started: it records
        # itself here, as a test process.
        child.stop(discard=True)
        os.environ[child.ENV_PARENT] = str(os.getpid())
        child.on_spawn = _spawned
        child.install_popen()
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
    os.fstat = _wrap_stat(os.fstat)
    os.access = _wrap_stat(os.access)
    os.readlink = _wrap_stat(os.readlink)
    child.install_fd_tracking(_touch, _flag, _error)
    if os.name == "nt":
        # Python 3.12+ on Windows answers these without os.stat.
        for name in ("exists", "lexists", "isfile", "isdir", "islink"):
            setattr(os.path, name, _wrap_stat(getattr(os.path, name)))
    # functools.cache goes through it too; a deliberate patch of the stdlib.
    functools.lru_cache = _tracking_lru_cache  # ty: ignore[invalid-assignment]
    _flag_multiprocessing_spawns()
    recording = True


# --------------------------------------------------------------------------- hooks


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global writer
    # Before the cache plugin's configure (registered before this one, and
    # pluggy calls the later registered first among tryfirst), which reads
    # its directory and the failures ``--lf``/``--ff`` use.
    inicache = getattr(config, "_inicache", None)
    if CACHE_DIR and isinstance(inicache, dict):
        inicache["cache_dir"] = CACHE_DIR
    expected = os.environ.get("DIFFCONE_CHECK_ENV")
    if expected:
        env = environment()
        match = environment_hash(env) == expected
        # Written whether or not it matches: a run without a report was not
        # checked, and diffcone does not trust its evidence plan.
        report = os.environ.get("DIFFCONE_CHECK_REPORT")
        if report:
            with open(report, "w", encoding="utf-8") as f:
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


def pytest_sessionstart(session):
    # The setting above uses pytest internals; should it stop taking effect,
    # ``--lf`` would narrow the recording to the repository's recorded
    # failures without a word: a cache elsewhere is a usage error.
    cache = getattr(session.config, "cache", None)
    if CACHE_DIR and cache is not None:
        where = os.path.realpath(cache.mkdir("diffcone"))
        if os.path.commonpath([where, os.path.realpath(CACHE_DIR)]) != os.path.realpath(CACHE_DIR):
            raise pytest.UsageError(
                f"diffcone could not point pytest's cache at {CACHE_DIR} (it is at {where}); "
                "--lf/--ff would read the repository's recorded failures"
            )


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


def _collector_file(collector) -> str | None:
    """The checkout file of a test module or class pytest is collecting
    (``Module``, ``Class``, a doctest module: every item it yields has that
    file in its node id), else None (the session, a directory, a package,
    another plugin's file collector, a file outside the checkout)."""
    if not isinstance(collector, (pytest.Module, pytest.Class)):
        return None
    path = getattr(collector, "path", None)
    if path is None:
        return None
    return _relative(os.path.abspath(os.fspath(path))) or None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_make_collect_report(collector):
    # Outermost, so other plugins' wrappers run inside it. Re-armed as it
    # opens and closes, as for an import: code another collector (or a hook)
    # ran already is disabled and would be missed.
    if not recording:
        return (yield)
    try:
        collecting.append(_collector_file(collector))
    except Exception as exc:
        _error("make_collect_report", exc)
        collecting.append(None)
    mon.restart_events()
    try:
        return (yield)
    finally:
        collecting.pop()
        mon.restart_events()


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
    try:
        _unaccounted_children()
    except Exception as exc:
        _error("children", exc)
    active.append(w)
    try:
        _credit_running_threads(w)
        _credit_live_children(w)
    except Exception as exc:
        _error("threads", exc)
    # Code this test already ran is disabled; re-arm so the fixture's own
    # window sees everything its setup runs.
    mon.restart_events()
    try:
        return (yield)
    finally:
        try:
            _unaccounted_children()
        except Exception as exc:
            _error("children", exc)
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
    global _test_window
    mine = _test_window = _window()
    try:
        _unaccounted_children()  # started outside every window
    except Exception as exc:
        _error("children", exc)
    active.append(mine)
    importing.clear()  # no import is running when a test starts
    try:
        _credit_running_threads(mine)
        _credit_live_children(mine)
    except Exception as exc:
        _error("threads", exc)
    mon.restart_events()
    # pytest works out where the test is (``item.location``, cached) as it
    # logs the test's start, which stats the test's own file: pytest finding
    # the test, not the test reading its file. Worked out here first, its
    # touches quiet. The test's object comes first and is not quiet: getting
    # it may build an instance of the test's class, whose ``__init__`` is
    # the test's own code.
    try:
        item.obj  # noqa: B018
    except Exception:
        pass
    child.quiet.depth += 1
    try:
        item.location  # noqa: B018
    except Exception:
        pass
    finally:
        child.quiet.depth -= 1
    try:
        return (yield)
    finally:
        try:
            _unaccounted_children()  # started during the test
        except Exception as exc:
            _error("children", exc)
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
                        _merge(mine, shared)
            if writer is not None:
                writer.add(fold_nodeid(item.nodeid), mine)
        except Exception as exc:
            _error("runtest_protocol", exc)


def pytest_runtest_logreport(report):
    # A test that failed or errored (in setup, call or teardown) stopped where
    # it failed: code it runs when it passes is not in its record.
    if recording and report.failed and _test_window is not None:
        _test_window["flags"] |= FLAG_FAILED


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
        "import_flagged": sorted(import_flagged),
        # Spawns outside every window, with the module being imported then.
        "import_spawns": [[*_spawn_list([spawn])[0], module] for spawn, module in import_spawns],
        "case_insensitive": CASE_FOLDS,
        # What the environments inside the checkout own, which nothing records.
        "environments": list(ENVIRONMENTS),
        "import_by": {str(k): sorted(v) for k, v in import_by.items()},
        # Code run while pytest collected a test module or class, with no
        # import running -> the checkout files collected.
        "collect_by": {str(k): sorted(v) for k, v in collect_by.items()},
        "hook_phase": sorted(hook_codes),
        "preloaded": preloaded,
        "outside_modules": sorted(outside),
        # What each shared fixture ran here: a fixture computed once for
        # every worker (a file lock and a cache file) runs its code in one.
        "fixtures": {
            fixture_key(key): _serialised(w) for key, w in sorted(fixture_windows.items())
        },
        "environment": env,
        "environment_hash": environment_hash(env),
        "environment_at_start": environment_at_start,
        "wrote_tests": writer is not None,
        "collected": sorted(collected) if collection_ran else None,
        "errors": errors,
    }
    with open(os.path.join(OUT, f"process-{os.getpid()}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f)


if OUT is not None:
    _start()

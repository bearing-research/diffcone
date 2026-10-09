"""Records Python processes a recorded test run starts (roadmap item 15).

Linked beside the recorder plugin as ``diffcone_child``, with a
``sitecustomize.py`` that calls ``start()``: like ``collect.py`` it imports
nothing of diffcone and needs nothing but the standard library, since a
child may run in another environment (a notebook's virtual environment)
without pytest. diffcone imports it as ``diffcone.child`` to read what the
children wrote (``resolve``), and the plugin as ``diffcone_child`` for the
spawn accounting both sides share.

A process records itself when ``DIFFCONE_COLLECT_PARENT`` is in its
environment: the plugin sets it at its own start, so the pytest process
never sees it at its start-up, and every process it starts inherits it
unless the spawn replaces the environment. It appends to
``<DIFFCONE_COLLECT_OUT>/children/<ppid>/<pid>-<start>.jsonl``, one JSON
value per line, unbuffered, so a child killed by a signal leaves all it
recorded:

* a header: ``{"pid", "ppid", "argv": sys.orig_argv, "start", "entry"}``,
  ``entry`` being what ``__main__`` runs (``entry()``);
* ``["c", path, line, qualname]``: a code object of the checkout, first run;
* ``["p", path]`` and ``["d", path]``: a checkout path opened (or
  ``stat``ed) and listed, as in a test window;
* ``["spawn", spawner, pid, start, argv]``: a child started through
  ``subprocess.Popen`` (accounted: its own record, or a launcher's, says
  what it ran);
* ``["fork", pid]``: a fork, which goes on writing here;
* ``["flag", why]``: something it ran that no record shows (another way of
  starting a process, an installed copy of the project, code compiled
  from text no file of the checkout holds, such as a doctest's examples
  (``text_code``), no ``sys.monitoring`` before Python 3.12, an error in
  the recorder).

A write that fails renames the record to ``<name>.broken`` (``_broken``):
the fold no longer finds it, so the spawn flags as one that recorded
nothing does. A pytest process of the same recording (an xdist worker)
drops its record and writes its own ``process-<pid>.json``; a spawn of one
resolves to that, not to a problem.

Windows keeps the subprocess flag: the plugin accounts no spawn there, and
no child records itself.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

ENV_PARENT = "DIFFCONE_COLLECT_PARENT"
TOOL = 3  # the plugin's sys.monitoring tool id: one recorder per process
CHILDREN = "children"
# A record whose write failed is renamed to this suffix: the fold no longer
# finds it, and the spawn resolves to nothing, which flags.
BROKEN = ".broken"
# Launchers: a program that is not Python but runs the command at the end of
# its command line (``uv run --directory nb python harness.py``), reading
# nothing of the checkout but build inputs, a change to which selects every
# target anyway.
LAUNCHERS = (("uv", ("run",)),)
# Package managers' commands: they install or resolve, running no project
# code themselves. An installed copy of the project they made is flagged
# where it runs; an editable one runs from the checkout and is recorded; a
# Python process they start (a build backend) is followed if it recorded.
# ``uv build`` (which reads the checkout for its artifacts), ``uv run``,
# ``uv tool run`` and ``uvx`` are not here.
TOOLS = (
    ("uv", ("sync",)),
    ("uv", ("lock",)),
    ("uv", ("venv",)),
    ("uv", ("pip",)),
    ("uv", ("add",)),
    ("uv", ("remove",)),
    ("uv", ("python",)),
    ("uv", ("tree",)),
    ("uv", ("export",)),
    ("uv", ("cache",)),
    ("uv", ("--version",)),
    ("uv", ("-V",)),
)
INTERPRETER = re.compile(r"python[0-9.]*(\.exe)?", re.IGNORECASE)


# --------------------------------------------------------------------------- spawn accounting


class _PopenState(threading.local):
    depth = 0
    argv: list[str] | None = None
    inert = False


popen = _PopenState()
# Called with (pid, start, argv, inert) after ``subprocess.Popen`` started a
# process (``inert``: the plugin judged its command line an interpreter probe
# that can run no project code); set by whichever recorder runs here.
on_spawn: Any = None


def argv_of(args: Any) -> list[str] | None:
    """The command line a ``subprocess.Popen`` audit event names."""
    argv = args[1] if len(args) > 1 else None
    if isinstance(argv, (str, bytes)) or argv is None:
        return None
    try:
        return [os.fsdecode(a) for a in argv]
    except TypeError:
        return None


def install_popen() -> None:
    """Account the processes ``subprocess.Popen`` starts (``asyncio``'s
    included): the audit events raised inside (``subprocess.Popen``, the
    ``os.posix_spawn`` it may use) are noted, not flagged, and ``on_spawn``
    learns the child's pid. POSIX only."""
    original = subprocess.Popen._execute_child  # ty: ignore[unresolved-attribute]
    if getattr(original, "__diffcone__", False):
        return

    @functools.wraps(original)
    def _execute_child(self, *args, **kwargs):
        popen.depth += 1
        popen.argv, popen.inert = None, False
        start = time.monotonic_ns()
        try:
            original(self, *args, **kwargs)
        finally:
            popen.depth -= 1
        if on_spawn is not None:
            try:
                on_spawn(self.pid, start, popen.argv, popen.inert)
            except Exception:  # never into the test; each recorder reports its own
                pass

    setattr(_execute_child, "__diffcone__", True)  # noqa: B010
    subprocess.Popen._execute_child = _execute_child  # ty: ignore[unresolved-attribute]


def in_popen(event: str, args: Any) -> bool:
    """Whether an audit event comes from inside an accounted
    ``subprocess.Popen``; the first one sets the command line."""
    if popen.depth <= 0:
        return False
    if event == "subprocess.Popen":
        popen.argv = argv_of(args)
    return True


def alive(pid: int) -> bool:
    """A zombie, or a pid reused by another process, counts as alive: that
    only credits a child to more windows."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# --------------------------------------------------------------------------- reading records


class Record:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pid = 0
        self.argv: list[str] = []
        self.codes: list[tuple[str, int, str]] = []
        self.paths: set[str] = set()
        self.dirs: set[str] = set()
        self.spawns: list[tuple[int, int, int, list[str] | None]] = []
        self.forks: list[int] = []
        self.flags: list[str] = []
        self.entry: list = ["code"]
        try:
            lines = path.read_bytes().split(b"\n")
        except OSError as exc:
            self.flags.append(f"unreadable record {path.name}: {exc}")
            return
        header = False
        for n, line in enumerate(lines):
            if not line:
                continue
            try:
                value = json.loads(line)
                if n == 0:
                    self.pid, self.argv = int(value["pid"]), list(value["argv"])
                    self.entry = list(value.get("entry") or ["code"])
                    header = True
                    continue
                kind = value[0]
                if kind == "c":
                    self.codes.append((value[1], int(value[2]), value[3]))
                elif kind == "p":
                    self.paths.add(value[1])
                elif kind == "d":
                    self.dirs.add(value[1])
                elif kind == "spawn":
                    self.spawns.append((int(value[1]), int(value[2]), int(value[3]), value[4]))
                elif kind == "fork":
                    self.forks.append(int(value[1]))
                elif kind == "flag":
                    self.flags.append(str(value[1]))
            except (ValueError, KeyError, IndexError, TypeError):
                self.flags.append(f"corrupt line {n + 1} in {path.name}")
        if not header:
            # Killed before its first write, or emptied by a failed one.
            self.flags.append(f"no header in {path.name}")


class Tree:
    """What the processes behind one accounted spawn recorded."""

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.pids: set[int] = set()
        self.problems: list[str] = []


def _records(out: Path, spawner: int, start: int, pid: int | None = None) -> list[Path]:
    """Records of processes ``spawner`` started at or after ``start``
    (``pid``: only that one): the file name is ``<pid>-<start>``."""
    directory = out / CHILDREN / str(spawner)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = []
    for name in sorted(names):
        stem = name.removesuffix(".jsonl")
        child, _, began = stem.partition("-")
        if not (child.isdigit() and began.isdigit()) or int(began) < start:
            continue
        if pid is None or int(child) == pid:
            found.append(directory / name)
    return found


def _matches(argv: list[str] | None, table: tuple) -> bool:
    if not argv:
        return False
    name = os.path.basename(argv[0]).lower().removesuffix(".exe")
    # Options before the command (``uv --quiet sync``) are skipped.
    rest = [a for a in argv[1:] if not a.startswith("-")] or argv[1:2]
    return any(
        name == tool and (tuple(argv[1 : 1 + len(s)]) == s or tuple(rest[: len(s)]) == s)
        for tool, s in table
    )


def _launched(argv: list[str] | None) -> bool:
    return _matches(argv, LAUNCHERS)


def _runs(argv: list[str], record: Record) -> bool:
    """Whether the launcher command line ``argv`` ends with the command the
    recorded interpreter ran (``python harness.py m``, or a console script
    ``pytest -x`` that the interpreter runs as ``python /venv/bin/pytest -x``)."""
    ran = record.argv[1:]
    if len(ran) < len(argv) and argv[len(argv) - len(ran) :] == ran:
        program = argv[len(argv) - len(ran) - 1]
        if INTERPRETER.fullmatch(os.path.basename(program)):
            return True
    if ran and len(ran) <= len(argv) - 2:
        tail = argv[len(argv) - len(ran) :]
        if tail[1:] == ran[1:] and os.path.basename(tail[0]) == os.path.basename(ran[0]):
            return True
    return False


def _started(argv: list[str] | None, record: Record) -> bool:
    """Whether the spawned command line started the recorded interpreter
    itself: the same arguments after the program, or a script the kernel
    ran with it (a shebang: ``python /venv/bin/tool args``)."""
    if not argv or not record.argv:
        return False
    if record.argv[1:] == argv[1:]:
        return True
    ran = record.argv[1:]
    return (
        len(ran) == len(argv)
        and ran[1:] == argv[1:]
        and os.path.basename(ran[0]) == os.path.basename(argv[0])
    )


def resolve(
    out: Path, spawner: int, pid: int, start: int, argv: list[str] | None, inert: bool = False
) -> Tree:
    """The records behind a spawn: the process itself when it was Python,
    else (a launcher) the Python processes it started, one of which ran the
    launcher's command; and, recursively, every process those started or
    forked. A spawn nothing recorded, or that a record flags, is a problem,
    except an ``inert`` probe (by its command line, an interpreter that can
    run no project code) that could not record (``python -I``), and a pytest
    process of the same recording (an xdist worker, whose own process record
    is read as one)."""
    tree = Tree()
    _resolve(out, spawner, pid, start, argv, tree, set(), inert)
    return tree


def _resolve(
    out: Path,
    spawner: int,
    pid: int,
    start: int,
    argv: list[str] | None,
    tree: Tree,
    seen: set[Path],
    inert: bool = False,
) -> None:
    tree.pids.add(pid)
    own = _records(out, spawner, start, pid)
    if own:
        # The process became Python: by its own command line, else something
        # else (a shell's ``exec python``) ran before it, unseen.
        if not any(_started(argv, Record(p)) for p in own):
            tree.problems.append(
                f"{' '.join(argv or ['a process'])[:200]} ran other code before Python"
            )
        for path in own:
            _follow(out, path, tree, seen)
        return
    if (out / f"process-{pid}.json").exists():
        return
    started = _records(out, pid, start)
    if inert or _matches(argv, TOOLS):
        for path in started:
            _follow(out, path, tree, seen)
        return
    if not _launched(argv):
        tree.problems.append(
            f"started {' '.join(argv or ['a process'])[:200]}, which recorded nothing"
        )
        return
    records = [Record(p) for p in started]
    if not any(_runs(argv or [], r) for r in records):
        tree.problems.append(f"{' '.join(argv or [])[:200]}: its command recorded nothing")
    for path in started:
        _follow(out, path, tree, seen)


def _follow(out: Path, path: Path, tree: Tree, seen: set[Path]) -> None:
    if path in seen:
        return
    seen.add(path)
    record = Record(path)
    tree.records.append(record)
    tree.pids.add(record.pid)
    tree.problems.extend(record.flags)
    listed = set()
    for spawner, pid, start, argv in record.spawns:
        listed.add(pid)
        _resolve(out, spawner, pid, start, argv, tree, seen)
    # Started without a spawn line (killed between the two), or by a fork.
    for process in (record.pid, *record.forks):
        tree.pids.add(process)
        for child in _records(out, process, 0):
            if int(child.name.partition("-")[0]) not in listed:
                _follow(out, child, tree, seen)


def tree_alive(
    out: Path, spawner: int, pid: int, start: int, argv: list[str] | None, inert: bool = False
) -> bool:
    if alive(pid):
        return True
    return any(alive(p) for p in resolve(out, spawner, pid, start, argv, inert).pids)


# --------------------------------------------------------------------------- recording a child

recording = False
_fd = -1
_path = ""
_seen_codes: set = set()
_seen_paths: set[tuple[str, str]] = set()
_roots: tuple[str, ...] = ()
_environments: tuple[str, ...] = ()  # as collect.ENVIRONMENTS, for this interpreter
_cwd = ""
_packages: frozenset[str] = frozenset()
IGNORED_DIRS = (".git" + os.sep, ".diffcone" + os.sep)
INSTALLED = (os.sep + "site-packages" + os.sep, os.sep + "dist-packages" + os.sep)
CYTHON_SUFFIXES = (".pyx", ".pxd", ".pxi")
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
        "_winapi.CreateProcess",
    }
)
C_OPEN_EVENTS = frozenset({"sqlite3.connect", "ctypes.dlopen"})
LISTING_EVENTS = frozenset({"os.listdir", "os.scandir"})
DATA_READERS = frozenset({"get_data", "open_resource", "read_binary", "read_text"})


def environments(roots: tuple[str, ...]) -> tuple[str, ...]:
    """What the interpreter's environments kept inside the checkout own,
    relative to a root (``svc/bin/``, ``.venv/lib/python3.13/``, on Windows
    ``.venv/Scripts/`` and ``.venv/Lib/site-packages/``, ``svc/pyvenv.cfg``):
    installed code, like ``site-packages``, scripts included (``uv run
    pytest`` runs ``.venv/bin/pytest``). Only those: an environment made in a
    directory that holds source (``cd svc && python -m venv .``) leaves the
    source the project's. The fold refuses a recording
    in which one of them holds an indexed file. ``roots`` end in a
    separator, and are case-folded on Windows."""
    if os.name == "nt":
        owned = ["Scripts" + os.sep, os.path.join("Lib", "site-packages", ""), "pyvenv.cfg"]
    else:
        version = f"python{sys.version_info[0]}.{sys.version_info[1]}"
        abiflags = getattr(sys, "abiflags", "")
        owned = ["bin" + os.sep, "pyvenv.cfg"]
        for name in {version, version + abiflags}:
            owned += [os.path.join("lib", name, ""), os.path.join("lib64", name, "")]
    found = set()
    for prefix in {sys.prefix, sys.exec_prefix, sys.base_prefix}:
        for path in (os.path.abspath(prefix), os.path.realpath(prefix)):
            path = os.path.join(path, "")
            for root in roots:
                if not path.startswith(root):
                    continue
                for entry in owned:
                    rel = path[len(root) :] + entry
                    found.add(os.path.normcase(rel) if os.name == "nt" else rel)
    return tuple(sorted(found))


def _write(value: Any) -> None:
    """One line, whole, or none: a write that fails (a full disk, a file too
    large) leaves the record unreadable (``_broken``), which flags every
    test this process is credited to, as a process that recorded nothing
    does. A record silently cut short could not be told from a complete
    one."""
    if _fd < 0:
        return
    try:
        data = json.dumps(value).encode() + b"\n"
    except (TypeError, ValueError) as exc:
        data = json.dumps(["flag", f"recorder: unserialisable record: {exc}"]).encode() + b"\n"
    try:
        if os.write(_fd, data) == len(data):
            return
    except OSError:
        pass
    _broken()


def _broken() -> None:
    """Make the record unreadable: renamed out of the names the fold reads,
    or, failing that, emptied (a record without its header is a problem)."""
    global _fd
    fd, _fd = _fd, -1
    try:
        os.close(fd)  # first: Windows renames no open file
    except OSError:
        pass
    try:
        os.rename(_path, _path + BROKEN)
    except OSError:
        try:
            os.close(os.open(_path, os.O_WRONLY | os.O_TRUNC))
        except OSError:
            pass


def _flag(why: str) -> None:
    _write(["flag", why])


def _relative(path: str) -> str | None:
    """As ``collect._relative``: the checkout-relative path with ``/``, None
    outside the checkout and for an environment inside it."""
    if path.startswith("<"):  # <frozen ...>, <string>
        return None
    if not os.path.isabs(path):
        path = os.path.join(_cwd, path)
    path = os.path.normpath(path)
    for root in _roots:
        if path + os.sep == root:
            return ""
        if path.startswith(root):
            rel = path[len(root) :]
            if (
                rel.startswith(IGNORED_DIRS)
                or rel.startswith(_environments)
                or any(p in rel for p in INSTALLED)
            ):
                return None
            return rel.replace(os.sep, "/") if os.sep != "/" else rel
    return None


_code_paths: dict[str, str | None] = {}


def _code_path(name: str) -> str | None:
    """Cached per file name: a child importing a large library starts tens of
    thousands of code objects."""
    try:
        return _code_paths[name]
    except KeyError:
        pass
    path = name
    if not os.path.isabs(name) and name.endswith(CYTHON_SUFFIXES) and _roots:
        path = os.path.join(_roots[0], name)
    rel = _code_paths[name] = _relative(path)
    return rel


def _installed_copy(name: str) -> bool:
    """A module of one of the project's packages loaded from an installed
    copy (``site-packages/<package>/...``): code no record of the checkout
    shows."""
    for marker in INSTALLED:
        at = name.rfind(marker)
        if at >= 0:
            top = name[at + len(marker) :].split(os.sep, 1)[0]
            return top.removesuffix(".py") in _packages
    return False


def _on_code(code, offset):
    if code in _seen_codes:
        return _disable()
    _seen_codes.add(code)
    try:
        rel = _code_path(code.co_filename)
        if rel is not None:
            _write(["c", rel, code.co_firstlineno, code.co_qualname])
        elif code.co_name == "<module>" and _installed_copy(code.co_filename):
            _flag(f"ran an installed copy of the project: {code.co_filename}")
    except Exception as exc:
        _flag(f"recorder: {type(exc).__name__}: {exc}")
    return _disable()


def _disable():
    mon = getattr(sys, "monitoring", None)
    return mon.DISABLE if mon is not None else None


def _on_throw(code, offset, exc):
    # Cannot be disabled.
    if code not in _seen_codes:
        _on_code(code, offset)


def _actor() -> str:
    """As ``collect._actor``: ``import`` only when the import system itself
    touched the path, not a library's top level that an import ran."""
    frame = sys._getframe(2)
    module = False
    while frame is not None:
        code = frame.f_code
        name = code.co_filename
        if name.startswith("<frozen importlib") and code.co_name not in DATA_READERS:
            if not module:
                return "import"
        elif _relative(name) is not None:
            return "project"
        elif code.co_name == "<module>":
            module = True
        frame = frame.f_back
    return "other"


METADATA_MODULES = ("importlib.metadata", "importlib_metadata", "pkg_resources")


def _metadata_scan(path: str) -> bool:
    """As ``collect._metadata_scan``: ``importlib.metadata`` stat'ing or
    listing a ``sys.path`` entry, below any project frame."""
    if path not in {os.path.abspath(e or ".") for e in sys.path}:
        return False
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_globals.get("__name__", "").startswith(METADATA_MODULES):
            return True
        if _relative(frame.f_code.co_filename) is not None:
            return False
        frame = frame.f_back
    return False


def _touch(path: Any, listing: bool = False) -> None:
    if isinstance(path, int) or path is None:
        return
    try:
        path = os.fsdecode(os.fspath(path))
    except TypeError:
        return
    absolute = os.path.abspath(path)
    rel = _relative(absolute)
    if rel is None or _metadata_scan(absolute) or _actor() == "import":
        return
    key = ("d" if listing else "p", rel)
    if key not in _seen_paths:
        _seen_paths.add(key)
        _write(list(key))


def _audit(event: str, args: Any) -> None:
    if not recording:
        return
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
            # A fork goes on recording here (_forked); an exec after it flags.
            if event not in ("os.fork", "os.forkpty") and not in_popen(event, args):
                _flag(f"started a process another way ({event})")
        elif event == "exec":
            if args and text_code(args[0], sys._getframe().f_back, _code_path):
                name = args[0].co_filename
                if name not in _text_seen:
                    _text_seen.add(name)
                    _flag(f"ran code compiled from text no file of the checkout holds: {name}")
    except Exception as exc:
        _flag(f"recorder: audit {event}: {type(exc).__name__}: {exc}")


_text_seen: set[str] = set()
# Modules that run text they generate themselves from a definition, reading
# only names they put in its namespace (``collections.namedtuple``,
# dataclasses, annotations, attrs' generated methods), or text from pytest's
# command line (``-m``/``-k`` expressions, matched against marks and names).
TEXT_GENERATORS = (
    "collections",
    "dataclasses",
    "annotationlib",
    "typing",
    "attr",
    "_pytest.mark",
)
# Modules that run modules, whose code comes from a file (or is frozen into
# the interpreter): the import system, ``runpy``, pytest's assertion
# rewriting of test modules and plugins.
MODULE_RUNNERS = (
    "importlib",
    "_frozen_importlib",
    "_frozen_importlib_external",
    "zipimport",
    "runpy",
    "_pytest.assertion",
)


def _of(module: str, names: tuple[str, ...]) -> bool:
    return any(module == n or module.startswith(n + ".") for n in names)


def text_code(code: Any, caller: Any, code_path: Any) -> bool:
    """Whether ``exec``/``eval`` of ``code``, called from the frame
    ``caller``, runs text no file of the checkout holds (a doctest's
    example, a ``timeit`` statement, a notebook cell, a ``skipif`` string)
    for something other than project code, and the text names something it
    could read of the project: no record shows what that reads.

    Not counted: project code's own ``exec`` (a lookup site the planner
    knows); text that names nothing (a library's ``eval`` of a literal);
    the methods ``collections`` and ``dataclasses`` generate; and text run
    while a module is imported, which is a library generating its own code
    (numpy 1.x's dispatch wrappers), not a test running supplied text.
    ``code_path`` maps a code object's file name to its checkout path, None
    outside the checkout. Shared by both recorders."""
    name = getattr(code, "co_filename", None)
    if not isinstance(code, types.CodeType) or not isinstance(name, str):
        return False
    if not name.startswith("<") and code_path(name) is not None:
        return False  # a file of the checkout: recorded as its code
    if caller is None or code_path(caller.f_code.co_filename) is not None:
        return False
    module = str(caller.f_globals.get("__name__", ""))
    if _of(module, MODULE_RUNNERS) or _of(module, TEXT_GENERATORS):
        return False
    if not _names_something(code):
        return False
    frame = caller
    while frame is not None:
        if frame.f_code.co_filename.startswith("<frozen importlib"):
            return False
        frame = frame.f_back
    return True


def _names_something(code: types.CodeType) -> bool:
    return bool(code.co_names) or any(
        _names_something(c) for c in code.co_consts if isinstance(c, types.CodeType)
    )


def _wrap_stat(original):
    @functools.wraps(original)
    def stat(path, *args, **kwargs):
        if recording:
            try:
                _touch(path)
            except Exception as exc:
                _flag(f"recorder: stat: {type(exc).__name__}: {exc}")
        return original(path, *args, **kwargs)

    for supported in (
        os.supports_dir_fd,
        os.supports_fd,
        os.supports_follow_symlinks,
        os.supports_effective_ids,
    ):
        if original in supported:
            supported.add(stat)
    return stat


def entry() -> list:
    """What ``__main__`` runs, from the command line: ``["code"]`` for
    ``-c`` or standard input, ``["module", name]`` for ``-m``, ``["script",
    path, installed]`` for a script (``path`` checkout-relative, or None
    outside the checkout; ``installed``: under the interpreter's prefix, a
    console script). Code the index does not hold can read any project name,
    which no record shows."""
    argv = list(getattr(sys, "orig_argv", sys.argv))[1:]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-c", "-") or (arg.startswith("-c") and len(arg) > 2):
            return ["code"]
        if arg == "-m":
            return ["module", argv[i + 1] if i + 1 < len(argv) else ""]
        if arg.startswith("-m") and len(arg) > 2:
            return ["module", arg[2:]]
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        path = os.path.abspath(arg)
        prefixes = {sys.prefix, sys.base_prefix, sys.exec_prefix}
        installed = any(path.startswith(os.path.join(p, "")) for p in prefixes if p)
        return ["script", _relative(path), installed]
    return ["code"]  # standard input or an interactive session


def _spawned(pid: int, start: int, argv: list[str] | None, inert: bool = False) -> None:
    if recording:
        _write(["spawn", os.getpid(), pid, start, argv])


def _forked() -> None:
    if recording:
        _write(["fork", os.getpid()])


def _flag_calls(module_name: str, name: str) -> None:
    """Starts that raise no audit event: ``multiprocessing``'s spawn and
    forkserver children, a subinterpreter."""
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return
    original = getattr(module, name, None)
    if original is None or getattr(original, "__diffcone_child__", False):
        return

    @functools.wraps(original)
    def flagged(*args, **kwargs):
        if recording:
            _flag(f"started a process another way ({module_name}.{name})")
        return original(*args, **kwargs)

    setattr(flagged, "__diffcone_child__", True)  # noqa: B010
    setattr(module, name, flagged)


def start() -> None:
    """Record this process, if a recorded test run started it."""
    global recording, _fd, _path, _roots, _environments, _cwd, _packages, on_spawn
    if recording or os.name == "nt" or not os.environ.get(ENV_PARENT):
        return
    out = os.environ.get("DIFFCONE_COLLECT_OUT")
    if not out:
        return
    began = time.monotonic_ns()
    try:
        directory = os.path.join(out, CHILDREN, str(os.getppid()))
        os.makedirs(directory, exist_ok=True)
        _path = os.path.join(directory, f"{os.getpid()}-{began}.jsonl")
        _fd = os.open(_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o644)
    except OSError:
        return  # no record: the spawn resolves to nothing, and flags
    root = os.environ.get("DIFFCONE_COLLECT_ROOT") or os.getcwd()
    _roots = tuple(sorted({os.path.abspath(root) + os.sep, os.path.realpath(root) + os.sep}))
    _environments = environments(_roots)
    _cwd = os.getcwd()
    header = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "argv": getattr(sys, "orig_argv", sys.argv),
        "start": began,
        "entry": entry(),
    }
    _write(header)
    mon = getattr(sys, "monitoring", None)
    if mon is None:
        _flag(f"Python {sys.version.split()[0]} has no sys.monitoring; 3.12+ is needed")
        return
    try:
        mon.use_tool_id(TOOL, "diffcone")
    except ValueError as exc:
        _flag(f"sys.monitoring tool id {TOOL} is taken ({exc})")
        return
    _packages = frozenset(
        p for p in os.environ.get("DIFFCONE_COLLECT_PACKAGES", "").split(",") if p
    )
    for event in (mon.events.PY_START, mon.events.PY_RESUME):
        mon.register_callback(TOOL, event, _on_code)
    mon.register_callback(TOOL, mon.events.PY_THROW, _on_throw)
    mon.set_events(TOOL, mon.events.PY_START | mon.events.PY_RESUME | mon.events.PY_THROW)
    sys.addaudithook(_audit)
    os.stat = _wrap_stat(os.stat)
    os.lstat = _wrap_stat(os.lstat)
    os.access = _wrap_stat(os.access)
    on_spawn = _spawned
    install_popen()
    os.register_at_fork(after_in_child=_forked)
    for module_name, name in (
        ("multiprocessing.util", "spawnv_passfds"),
        ("_interpreters", "create"),
    ):
        _flag_calls(module_name, name)
    recording = True


def stop(discard: bool = False) -> None:
    """Stop recording (a pytest process that records itself: an xdist
    worker); ``discard`` removes what this process wrote."""
    global recording, _fd
    if not recording:
        return
    recording = False
    mon: Any = getattr(sys, "monitoring")  # noqa: B009 (3.12+, as recording is)
    mon.set_events(TOOL, 0)
    for event in (mon.events.PY_START, mon.events.PY_RESUME, mon.events.PY_THROW):
        mon.register_callback(TOOL, event, None)
    mon.free_tool_id(TOOL)
    try:
        os.close(_fd)
        if discard:
            os.unlink(_path)
    except OSError:
        pass
    _fd = -1

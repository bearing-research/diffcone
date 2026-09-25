"""Spike recorder: what each test executes and opens (pytest plugin).

Throwaway prototype for the execution-evidence go/no-go. Load with
``-p diffcone_spike`` (this directory on PYTHONPATH) and set
DIFFCONE_SPIKE_OUT to a directory. Writes per process:

  codes-<pid>.json   code table [path, firstlineno, qualname], and
                     ``import_by``: code index -> the modules whose import
                     (a ``<module>`` frame on the stack, innermost) ran it
  tests-<pid>.bin    per folded test id: executed code indices, opened files

A test's record is everything executed in its setup, call and teardown,
plus the setup of every non-function-scoped fixture it uses (credited to
every user, not just the first). functools caches are cleared before each
test so a cached value is recomputed, and recorded, by each test.
Opened files come from an audit hook. Looked-up names are not recorded:
wrapping ``getattr`` changed pandas test outcomes (docs/evidence_design.md).
"""

from __future__ import annotations

import functools
import gc
import json
import os
import struct
import sys
import zlib

import pytest

TOOL = 3
IMPORT_BY = not os.environ.get("DIFFCONE_SPIKE_NO_IMPORTBY")
AUDIT = not os.environ.get("DIFFCONE_SPIKE_NO_AUDIT")
mon = sys.monitoring
OUT = os.environ.get("DIFFCONE_SPIKE_OUT", ".")
ROOT = os.path.realpath(os.environ.get("DIFFCONE_SPIKE_ROOT", os.getcwd())) + os.sep

codes: dict[object, int] = {}
table: list[list] = []
active: list[dict] = []  # windows, innermost last: {"codes", "files", "names"}
import_window = {"codes": set(), "files": set(), "names": set()}
import_by: dict[int, set[str]] = {}
fixture_windows: dict[tuple[str, str, str], dict] = {}
lru_wrappers: list = []


def _window() -> dict:
    return {"codes": set(), "files": set(), "names": set()}


def _windows():
    return active if active else (import_window,)


importing: list[str] = []  # modules whose top-level code is running, innermost last


def _on_start(code, offset):
    path = code.co_filename
    if not path.startswith(ROOT):
        return mon.DISABLE
    i = codes.get(code)
    if i is None:
        i = codes[code] = len(table)
        table.append([path[len(ROOT) :], code.co_firstlineno, code.co_qualname])
    for w in _windows():
        w["codes"].add(i)
    if IMPORT_BY:
        if code.co_name == "<module>":
            # Credit what this import runs to this module: re-arm, and pop on return.
            importing.append(path[len(ROOT) :])
            mon.set_local_events(TOOL, code, mon.events.PY_RETURN)
            mon.restart_events()
        elif importing:
            import_by.setdefault(i, set()).add(importing[-1])
    return mon.DISABLE


def _on_return(code, offset, retval):
    if code.co_name == "<module>" and importing:
        importing.pop()
        mon.restart_events()  # the importer's own calls are credited to it again
    return None


def _on_unwind(code, offset, exc):
    # An import that raises (``pytest.importorskip`` at module level) ends
    # with an unwind, not a return. This event cannot be disabled, so keep it cheap.
    if code.co_name == "<module>" and importing and code.co_filename.startswith(ROOT):
        if importing[-1] == code.co_filename[len(ROOT) :]:
            importing.pop()
            mon.restart_events()


def _audit(event, args):
    if event != "open" or not args:
        return
    path = args[0]
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str):
        return
    full = os.path.realpath(path)
    if full.startswith(ROOT):
        for w in _windows():
            w["files"].add(full[len(ROOT) :])


def _fold(nodeid: str) -> str:
    head, sep, rest = nodeid.partition("::")
    if "[" in rest:
        rest = rest[: rest.index("[")]
    return head + sep + rest


def _drop(window: dict) -> None:
    for i in range(len(active) - 1, -1, -1):
        if active[i] is window:
            del active[i]
            return


class _Writer:
    def __init__(self) -> None:
        self.f = open(os.path.join(OUT, f"tests-{os.getpid()}.bin"), "wb")
        self.current: str | None = None
        self.acc = _window()

    def add(self, fid: str, w: dict) -> None:
        if fid != self.current:
            self.flush()
            self.current = fid
        for k in self.acc:
            self.acc[k] |= w[k]

    def flush(self) -> None:
        if self.current is None:
            return
        payload = json.dumps(
            {
                "codes": sorted(self.acc["codes"]),
                "files": sorted(self.acc["files"]),
                "names": sorted(self.acc["names"]),
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


def _start() -> None:
    """Start at import of this plugin: ``-p`` plugins load before the initial
    conftests, which import the project, so its import-time code is seen."""
    mon.use_tool_id(TOOL, "diffcone-spike")  # raises if the id is taken: fail loudly
    mon.register_callback(TOOL, mon.events.PY_START, _on_start)
    mon.register_callback(TOOL, mon.events.PY_RETURN, _on_return)
    mon.register_callback(TOOL, mon.events.PY_UNWIND, _on_unwind)
    mon.set_events(TOOL, mon.events.PY_START | (mon.events.PY_UNWIND if IMPORT_BY else 0))
    if AUDIT:
        sys.addaudithook(_audit)


def pytest_configure(config):
    global writer
    os.makedirs(OUT, exist_ok=True)
    writer = _Writer()


def pytest_collection_finish(session):
    lru_wrappers.extend(o for o in gc.get_objects() if isinstance(o, functools._lru_cache_wrapper))


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.scope == "function":
        return (yield)
    key = (fixturedef.baseid, fixturedef.argname, fixturedef.scope)
    w = fixture_windows.setdefault(key, _window())
    active.append(w)
    try:
        return (yield)
    finally:
        _drop(w)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    for c in lru_wrappers:
        try:
            c.cache_clear()
        except Exception:
            pass
    mine = _window()
    active.append(mine)
    importing.clear()  # no import is running when a test starts
    mon.restart_events()
    try:
        return (yield)
    finally:
        _drop(mine)
        mon.restart_events()
        for name in getattr(item, "fixturenames", ()):
            for defs in item._fixtureinfo.name2fixturedefs.get(name, ()):
                if defs.scope != "function":
                    shared = fixture_windows.get((defs.baseid, defs.argname, defs.scope))
                    if shared:
                        for k in mine:
                            mine[k] |= shared[k]
        writer.add(_fold(item.nodeid), mine)


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    mon.register_callback(TOOL, mon.events.PY_START, None)
    mon.register_callback(TOOL, mon.events.PY_RETURN, None)
    mon.register_callback(TOOL, mon.events.PY_UNWIND, None)
    mon.free_tool_id(TOOL)
    if writer is not None:
        writer.close()
    with open(os.path.join(OUT, f"codes-{os.getpid()}.json"), "w") as f:
        json.dump(
            {
                "table": table,
                "import_phase": sorted(import_window["codes"]),
                "import_files": sorted(import_window["files"]),
                "import_by": {str(k): sorted(v) for k, v in import_by.items()},
            },
            f,
        )


_start()

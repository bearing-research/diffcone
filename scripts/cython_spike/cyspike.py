"""Spike plugin (roadmap item 7): per test, which Cython code ran.

CYSPIKE_MODE=starts: sys.monitoring PY_START for code objects from .pyx/.pxd/.pxi
  files (a profile=True build raises them): records (file, co_firstlineno).
CYSPIKE_MODE=lines: sys.settrace line events in Cython frames (a linetrace
  build with legacy tracing raises them): records (file, line).
Writes CYSPIKE_OUT/<mode>-<pid>.json: {"files": [...], "tests": {nodeid: [fid*1e6+line]}}.
"""

import json
import os
import re
import sys
from typing import Any

import pytest

# The spike runs on 3.12+; the project's floor (3.11) has no sys.monitoring.
MONITORING: Any = getattr(sys, "monitoring", None)

OUT = os.environ.get("CYSPIKE_OUT")
MODE = os.environ.get("CYSPIKE_MODE")
SUFFIXES = (".pyx", ".pxd", ".pxi")
files: dict[str, int] = {}
current: set[int] = set()
active = [False]  # recording inside a test window (a dealloc can run any time)
tests: dict[str, set[int]] = {}


def _fid(name: str) -> int:
    if name not in files:
        files[name] = len(files)
        _names.append(name)
    return files[name]


def _fold(nodeid: str) -> str:
    return re.sub(r"\[.*\]$", "", nodeid)


if OUT and MODE == "starts":
    mon = MONITORING
    TOOL = 4
    mon.use_tool_id(TOOL, "cyspike")

    def _start(code, offset):
        name = code.co_filename
        if name.endswith(SUFFIXES):
            if active[0]:
                current.add(_fid(name) * 1_000_000 + code.co_firstlineno)
        return mon.DISABLE

    mon.register_callback(TOOL, mon.events.PY_START, _start)
    mon.set_events(TOOL, mon.events.PY_START)

if OUT and MODE == "monlines":
    # A linetrace build in Cython's sys.monitoring mode raises LINE events.
    mon = MONITORING
    TOOL = 4
    mon.use_tool_id(TOOL, "cyspike")

    def _line(code, line):
        name = code.co_filename
        if name.endswith(SUFFIXES):
            # Cython reports every line of a function from one location, so
            # disabling it would silence the rest of the function.
            if active[0]:
                current.add(_fid(name) * 1_000_000 + line)
            return None
        return mon.DISABLE

    mon.register_callback(TOOL, mon.events.LINE, _line)
    mon.set_events(TOOL, mon.events.LINE)


def _local(frame, event, arg):
    if event == "line":
        name = frame.f_code.co_filename
        if name.endswith(SUFFIXES):
            current.add(_fid(name) * 1_000_000 + frame.f_lineno)
    return _local


def _global(frame, event, arg):
    # Cython's legacy line tracing calls a frame's local trace function
    # without checking for None, so every frame gets one.
    name = frame.f_code.co_filename
    if event == "call" and name.endswith(SUFFIXES):
        current.add(_fid(name) * 1_000_000 + frame.f_lineno)
    return _local


_log = None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _log
    if not OUT:
        return (yield)
    current.clear()
    active[0] = True
    if MODE in ("starts", "monlines"):
        MONITORING.restart_events()
    else:
        sys.settrace(_global)
    try:
        return (yield)
    finally:
        active[0] = False
        if MODE == "lines":
            sys.settrace(None)
        # One line per test, flushed, so a worker that crashes loses only
        # the test it was running.
        if _log is None:
            os.makedirs(OUT, exist_ok=True)
            _log = open(os.path.join(OUT, f"{MODE}-{os.getpid()}.jsonl"), "a")
        _log.write(
            json.dumps(
                {
                    "t": _fold(item.nodeid),
                    "x": sorted([files_list_index(x) for x in current]),
                }
            )
            + "\n"
        )
        _log.flush()


def files_list_index(x):
    # Store the file name with each entry: the per-process file table is not
    # written when a worker crashes.
    return [_names[x // 1_000_000], x % 1_000_000]


_names: list[str] = []

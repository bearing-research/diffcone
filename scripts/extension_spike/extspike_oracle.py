"""The line-traced oracle (item 18 spike): per test case, which compiled
functions ran.

Loaded as ``-p extspike_oracle`` into a pandas build with Cython
``profile``/``linetrace`` (sys.monitoring mode) and CYTHON_TRACE_NOGIL. A
LINE event in a Cython file is mapped to the outermost function holding the
line (diffcone.cython's reader, as the evidence fold does), or ``<outside>``;
a file that is not in the checkout (a generated .pxi, numpy's .pxd) is kept
as ``co_name@co_firstlineno``.

Writes EXTSPIKE_OUT/oracle-<pid>.jsonl: ``{"f": [id, "path::function"]}``
lines interning names, then one ``{"t": nodeid, "x": [ids]}`` per test case.
"""

import json
import os
import sys
from typing import Any

import pytest

from diffcone.cython import read

OUT = os.environ.get("EXTSPIKE_OUT", "extspike-oracle")
SUFFIXES = (".pyx", ".pxd", ".pxi")
mon: Any = getattr(sys, "monitoring", None)  # 3.12+
TOOL = 4
_modules: dict = {}
_ids: dict[str, int] = {}
_cache: dict = {}
current: set[int] = set()
active = [False]
_log = None


def _module(path):
    if path not in _modules:
        try:
            with open(path, encoding="utf-8") as f:
                _modules[path] = read(path, f.read())
        except OSError:
            _modules[path] = None
    return _modules[path]


def _name(code, line):
    path = code.co_filename
    module = _module(path)
    if module is None:
        return f"{path}::{code.co_name}@{code.co_firstlineno}"
    function = module.function_at(line)
    return f"{path}::{function.name if function else '<outside>'}"


def _id(name):
    i = _ids.get(name)
    if i is None:
        i = _ids[name] = len(_ids)
        _write({"f": [i, name]})
    return i


def _write(obj):
    global _log
    if _log is None:
        os.makedirs(OUT, exist_ok=True)
        _log = open(os.path.join(OUT, f"oracle-{os.getpid()}.jsonl"), "a")
    _log.write(json.dumps(obj) + "\n")


def _line(code, line, _suffixes=SUFFIXES, _disable=mon.DISABLE, _active=active):
    # Default arguments: module globals are gone during interpreter shutdown.
    name = code.co_filename
    if name and name.endswith(_suffixes):
        if _active[0]:
            key = (code, line)
            i = _cache.get(key)
            if i is None:
                i = _cache[key] = _id(_name(code, line))
            current.add(i)
        return None
    return _disable


if OUT:
    mon.use_tool_id(TOOL, "extspike")
    mon.register_callback(TOOL, mon.events.LINE, _line)
    mon.set_events(TOOL, mon.events.LINE)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not OUT:
        return (yield)
    current.clear()
    active[0] = True
    mon.restart_events()
    try:
        return (yield)
    finally:
        active[0] = False
        _write({"t": item.nodeid, "x": sorted(current)})
        if _log is not None:
            _log.flush()

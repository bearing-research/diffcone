"""Planning scenarios for state one piece of code writes and another reads.

A change reaches the readers of a module-level variable, a registry, an
object or process-global state (the environment, ``sys.path``, warning
filters) through the code that writes it: at import, in a hook or while
collecting, through an argument, a receiver, a module object however held,
or a patch. A store undone when the test ends, or made by code that runs
only inside one test (the isolation assumption), is seen by that test
alone. Assertions check exact target sets.
"""

from __future__ import annotations

import pytest

from diffcone.discovery import DiscoveryOptions
from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
from diffcone.testing import (
    asv_target,
    path_ids,
    py_target,
    reason,
    rules,
    selected,
    unselected,
)

# A project with a benchmark on nothing that changes (``NOOP``), planned with
# the pytest targets discovery finds; ``ASV_BENCH`` is the same with an ASV
# configuration.
BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
NOOP = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")


def _plan(repo, base, head, **kwargs):
    return repo.plan(base, head, [NOOP], discover_runners=["pytest"], **kwargs)


ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'
ASV_BENCH = {
    "asv.conf.json": ASV_CONF,
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}
BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")


# ------------------------------------------------------------------ registries filled by decorators
# Functions a decorator registers are reached through what holds them.


@pytest.mark.parametrize(
    ("files", "changed"),
    [
        (
            {
                "pkg/__init__.py": "from pkg import impls\n",
                "pkg/core.py": (
                    "from functools import singledispatch\n\n\n@singledispatch\n"
                    "def show(x):\n    return 'obj'\n"
                ),
                "pkg/impls.py": (
                    "from pkg.core import show\n\n\n"
                    "@show.register(int)\ndef _(x):\n    return 'int'\n"
                ),
                "tests/test_r.py": (
                    "import pkg\nfrom pkg.core import show\n\n\ndef test_r():\n    show(1)\n"
                ),
            },
            ("pkg/impls.py", "'int'", "'INT'"),
        ),
        (
            {
                "pkg/__init__.py": "",
                "pkg/reg.py": (
                    "REG = {}\n\n\n"
                    "def register(fn):\n    REG[fn.__name__] = fn\n    return fn\n\n\n"
                    "def dispatch(name):\n    return REG[name]()\n"
                ),
                "pkg/handlers.py": (
                    "from pkg.reg import register\n\n\n@register\ndef hello():\n    return 1\n"
                ),
                "tests/test_r.py": (
                    "import pkg.handlers\nfrom pkg.reg import dispatch\n\n\n"
                    "def test_r():\n    dispatch('hello')\n"
                ),
            },
            ("pkg/handlers.py", "return 1", "return 11"),
        ),
        (
            {
                "pkg/__init__.py": "",
                "pkg/app.py": (
                    "class App:\n    def __init__(self):\n        self.commands = {}\n\n"
                    "    def command(self, fn):\n"
                    "        self.commands[fn.__name__] = fn\n        return fn\n\n"
                    "    def run(self, name):\n        return self.commands[name]()\n\n\n"
                    "app = App()\n"
                ),
                "pkg/cli.py": (
                    "from pkg.app import app\n\n\n@app.command\ndef hello():\n    return 1\n"
                ),
                "tests/test_r.py": (
                    "import pkg.cli\nfrom pkg.app import app\n\n\n"
                    "def test_r():\n    app.run('hello')\n"
                ),
            },
            ("pkg/cli.py", "return 1", "return 11"),
        ),
    ],
)
def test_s2_a_registered_function_reaches_users_of_the_registry(repo, files, changed):
    path, old, new = changed
    base = repo.commit(
        {
            **files,
            "pkg/other.py": "def g():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    g()\n",
        }
    )
    head = repo.commit({path: files[path].replace(old, new)})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_r.py::test_r" in selected(plan)
    assert "tests/test_other.py::test_g" in unselected(plan)


# --------------------------------------------------------- writes through an argument or a receiver
# A call that changes the state it is handed (through a parameter, a receiver,
# a helper it calls) writes that state for its readers, at import or in a
# test.

# A test and a benchmark on unrelated code: no scenario below may select them.
UNRELATED_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/other.py": "def g():\n    return 3\n",
    "tests/__init__.py": "",
    "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    assert g() == 3\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.other import g\n\n\ndef time_g():\n    g()\n",
}
UNRELATED_BENCH = asv_target("bench.time_g", "benchmarks.bench.time_g")
UNRELATED_TEST = "tests/test_other.py::test_g"


def _static(repo, files, change):
    base = repo.commit({**UNRELATED_BASE, **files})
    head = repo.commit(change)
    plan = repo.plan(base, head, [UNRELATED_BENCH], discover_runners=["pytest"])
    chosen = selected(plan)
    assert UNRELATED_TEST not in chosen and UNRELATED_BENCH.runner_id not in chosen, [
        (r.rule, r.detail, [(s.source, s.target, s.kind) for s in r.path])
        for d in plan.selected
        if d.target.runner_id == UNRELATED_TEST
        for r in d.reasons
    ]
    return chosen


REGISTRY = "REG = {}\n\n\ndef get():\n    return REG.get('k')\n"
READS_REG = "from pkg.registry import get\n\n\ndef test_b():\n    assert get() == 'slow'\n"
WRITER = "def set_mode(d, v='slow'):\n    d['k'] = v\n"
CALLS_AT_IMPORT = "from pkg.registry import REG\nfrom pkg.state import set_mode\n\nset_mode(REG)\n"
W4_FILES = {
    "pkg/registry.py": REGISTRY,
    "pkg/state.py": WRITER,
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": CALLS_AT_IMPORT,
    "tests/sub/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": READS_REG,
}


@pytest.mark.parametrize(
    "change",
    [
        {"pkg/state.py": WRITER.replace("'slow'", "'fast'")},
        {"tests/sub/conftest.py": CALLS_AT_IMPORT.replace("set_mode(REG)", "set_mode(REG, 'x')")},
    ],
    ids=["writer body", "call argument"],
)
def test_w4_a_call_at_import_writing_through_its_argument(repo, change):
    """``set_mode(REG)`` in a conftest fills ``REG`` through the parameter
    ``d``: test_b reads it without importing the conftest."""
    chosen = _static(repo, W4_FILES, change)
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


# How the call at import reaches the state it changes: through a helper,
# a local alias, a keyword, ``setattr``, a method of an unknown receiver.
WRITE_SHAPES = {
    "helper": (
        "def set_mode(d, v='slow'):\n    _store(d, v)\n\n\ndef _store(m, v):\n    m['k'] = v\n",
        "set_mode(REG)",
    ),
    "local alias": ("def set_mode(d, v='slow'):\n    m = d\n    m.update(k=v)\n", "set_mode(REG)"),
    "keyword": ("def set_mode(v='slow', *, d):\n    d['k'] = v\n", "set_mode(d=REG)"),
    "setattr": ("def set_mode(d, v='slow'):\n    setattr(d, 'k', v)\n", "set_mode(REG)"),
    "unresolved receiver": (
        "class Filler:\n    def fill(self, d, v='slow'):\n        d['k'] = v\n\n\n"
        "def filler():\n    return [Filler()]\n",
        "filler()[0].fill(REG)",
    ),
}


@pytest.mark.parametrize("shape", list(WRITE_SHAPES))
def test_w4_how_a_call_writes_through_its_argument(repo, shape):
    writer, call = WRITE_SHAPES[shape]
    conftest = CALLS_AT_IMPORT.replace("set_mode(REG)", call)
    if "filler" in call:
        conftest = conftest.replace("import set_mode", "import filler")
    files = {**W4_FILES, "pkg/state.py": writer, "tests/sub/conftest.py": conftest}
    chosen = _static(repo, files, {"pkg/state.py": writer.replace("'slow'", "'fast'")})
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


RECEIVER = (
    "class Registry:\n    def __init__(self):\n        self.items = []\n\n"
    "    def register(self, x):\n        self._add(x)\n\n"
    "    def _add(self, x):\n        self.items.append(x)\n\n\n"
    "registry = Registry()\n\n\ndef get():\n    return registry.items\n"
)
RECEIVER_FILES = {
    "pkg/registry.py": RECEIVER,
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": "from pkg.registry import registry\n\nregistry.register('slow')\n",
    "tests/sub/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": (
        "from pkg.registry import get\n\n\ndef test_b():\n    assert get() == ['slow']\n"
    ),
}


def test_w4_a_call_at_import_writing_through_its_receiver(repo):
    """``registry.register(x)`` changes ``registry`` (``register`` appends to
    ``self.items`` through a helper): test_b reads it through ``get``."""
    conftest = RECEIVER_FILES["tests/sub/conftest.py"]
    chosen = _static(
        repo, RECEIVER_FILES, {"tests/sub/conftest.py": conftest.replace("slow", "fast")}
    )
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


STATE = (
    'MODE = ["slow"]\n\n\n'
    "def set_mode(m):\n    _store(m)\n    return m\n\n\n"
    "def _store(m):\n    MODE[0] = m\n\n\n"
    "def mode():\n    return MODE[0]\n"
)


@pytest.mark.parametrize(
    "caller",
    [
        "pkg/config.py",  # at import, in the library
        "tests/sub/conftest.py",  # at import, in a conftest
        "tests/sub/test_a.py",  # in a test
    ],
)
def test_w4_a_changed_body_reaches_what_its_callees_write(repo, caller):
    """``set_mode`` stores what it is given through ``_store``; a change to
    what it hands on (``_store(m + "!")``) changes ``MODE``, read by test_b
    through ``mode`` without running ``set_mode``."""
    call = "from pkg.state import set_mode\n\nX = set_mode('slow')\n"
    files = {
        "pkg/state.py": STATE,
        "tests/sub/__init__.py": "",
        "tests/sub/test_a.py": "import pkg.config  # noqa: F401\n\n\ndef test_a():\n    pass\n",
        "tests/test_b.py": (
            "from pkg.state import mode\n\n\ndef test_b():\n    assert mode() == 'slow'\n"
        ),
    }
    if caller == "tests/sub/test_a.py":
        files[caller] = "from pkg.state import set_mode\n\n\ndef test_a():\n    set_mode('slow')\n"
    else:
        files[caller] = call
    edit = STATE.replace("    _store(m)\n", "    _store(m + '!')\n")
    chosen = _static(repo, files, {"pkg/state.py": edit})
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_w4_a_changed_value_handed_to_a_writer(repo):
    """``setup`` hands ``_store`` what ``compute`` returns: a change to
    ``compute`` changes ``MODE`` though neither ``setup`` nor the writer
    changed. The explanation names the real call chain."""
    state = (
        'MODE = ["slow"]\n\n\n'
        "def compute():\n    return 'slow'\n\n\n"
        "def setup():\n    _store(compute())\n\n\n"
        "def _store(m):\n    MODE[0] = m\n\n\n"
        "def mode():\n    return MODE[0]\n"
    )
    files = {
        "pkg/state.py": state,
        "tests/sub/__init__.py": "",
        "tests/sub/conftest.py": "from pkg.state import setup\n\nsetup()\n",
        "tests/sub/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": (
            "from pkg.state import mode\n\n\ndef test_b():\n    assert mode() == 'slow'\n"
        ),
    }
    base = repo.commit({**UNRELATED_BASE, **files})
    head = repo.commit({"pkg/state.py": state.replace("return 'slow'", "return 'fast'")})
    plan = repo.plan(base, head, [UNRELATED_BENCH], discover_runners=["pytest"])
    assert selected(plan) == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}
    why = reason(plan, "tests/test_b.py::test_b")
    assert path_ids(why)[-4:] == [
        "pkg.state.MODE",
        "pkg.state._store",
        "pkg.state.setup",
        "pkg.state.compute",
    ], path_ids(why)
    assert [s.kind for s in why.path][-3:] == ["references", "called_by", "references"]
    assert why.path[-3].detail == "mutated_by"


@pytest.mark.parametrize(
    ("value", "base", "reaches"),
    [
        ("Recorder()", "", True),  # an instance of ours whose ``info`` writes it
        ("make()", "", True),  # what a factory returns: any ``info`` of ours
        ("{'name': 'x'}", "", False),  # a dict display: only its own mutators
        # A third-party value: no ``info`` of ours, unless a class of ours
        # derives from a third-party one (``logging.setLoggerClass``).
        ("logging.getLogger('x')", "", False),
        ("logging.getLogger('x')", "logging.Logger", True),
        # A wrapper whose every return is a third-party call: the same,
        # whatever ``cast`` says (W4 on strata: ``logger.info`` matched a
        # test fake's ``info``).
        ("get_logger('x')", "", False),
        ("get_logger('x')", "logging.Logger", True),
    ],
)
def test_w4_a_method_called_on_a_variable_writes_it_when_it_may_be_ours(repo, value, base, reaches):
    log = (
        "import logging\nfrom typing import cast\n\n\n"
        f"class Recorder({base}):\n    def info(self, m):\n        self.last = m\n\n\n"
        "def make():\n    return Recorder()\n\n\n"
        "def get_logger(name):\n    if not name:\n        return logging.getLogger()\n"
        "    return cast(Recorder, logging.getLogger(name))\n\n\n"
        f"log = {value}\n\n\n"
        "def get():\n    return log\n"
    )
    files = {
        "pkg/log.py": log,
        "tests/sub/__init__.py": "",
        "tests/sub/conftest.py": "from pkg.log import log\n\nlog.info('slow')\n",
        "tests/sub/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": "from pkg.log import get\n\n\ndef test_b():\n    assert get()\n",
    }
    change = {"tests/sub/conftest.py": "from pkg.log import log\n\nlog.info('fast')\n"}
    want = {"tests/sub/test_a.py::test_a"} | ({"tests/test_b.py::test_b"} if reaches else set())
    assert _static(repo, files, change) == want


def test_w4_locals_bound_to_each_other_are_no_loop(repo):
    """``a = b[0]`` and ``b = a[0]``: following what a local holds stops."""
    files = {
        "pkg/state.py": WRITER,
        "tests/test_loop.py": (
            "from pkg.state import set_mode\n\n\ndef test_loop():\n"
            "    if False:\n        a = b[0]  # noqa: F821\n        b = a[0]\n        set_mode(a)\n"
        ),
    }
    chosen = _static(repo, files, {"pkg/state.py": WRITER.replace("'slow'", "'fast'")})
    assert chosen == {"tests/test_loop.py::test_loop"}


# ---------------------------------------------- state held on a class, or reached by another name
# Code changing state in place is a dependency of every reader of that state,
# however it reaches it: through the class holding it (``Config.mode = m``,
# ``cls._instance = ...``), a name bound to part of it (``c = _CACHE``), what
# a call returns (``registry()[k] = v``) or ``globals()``. In each scenario
# test_a runs the writer, which changes; test_z and a benchmark read the
# state without running it.

READER_BENCH = asv_target("bench_read.time_peek", "benchmarks.bench_read.time_peek")
STATE_READERS = {
    "tests/test_a.py": "from pkg.lib import fill\n\n\ndef test_a():\n    fill('k')\n",
    "tests/test_z.py": "from pkg.state import peek\n\n\ndef test_z():\n    peek('k')\n",
    "benchmarks/bench_read.py": "from pkg.state import peek\n\n\ndef time_peek():\n    peek('k')\n",
}
# ``fill`` in pkg/lib.py, unless the state's own module defines it.
FILL_HERE = "from pkg.state import fill  # noqa: F401\n"


def _state_plan(repo, state, lib=FILL_HERE, edit=("'x'", "'y'"), extra=None):
    """Plan an edit of whichever module holds ``edit[0]``: the selection."""
    files = {**UNRELATED_BASE, **STATE_READERS, "pkg/state.py": state, "pkg/lib.py": lib}
    files.update(extra or {})
    path = "pkg/lib.py" if edit[0] in lib else "pkg/state.py"
    assert edit[0] in files[path]
    base = repo.commit(files)
    head = repo.commit({path: files[path].replace(*edit)})
    plan = repo.plan(base, head, [UNRELATED_BENCH, READER_BENCH], discover_runners=["pytest"])
    return selected(plan)


STATE_SELECTED = {"tests/test_a.py::test_a", "tests/test_z.py::test_z", READER_BENCH.runner_id}

CONFIG = (
    "class Config:\n    mode = None\n    items = []\n\n\n"
    "def peek(k):\n    return Config.mode, Config.items\n"
)
CONFIG_WRITER = "from pkg.state import Config\n\n\ndef fill(k):\n    {}\n"
CLASS_STATE = {
    "an attribute stored from another module": (CONFIG, CONFIG_WRITER.format("Config.mode = 'x'")),
    "a container changed from another module": (
        CONFIG,
        CONFIG_WRITER.format("Config.items.append('x')"),
    ),
    "setattr": (CONFIG, CONFIG_WRITER.format("setattr(Config, 'mode', 'x')")),
    "an attribute deleted": (
        CONFIG.replace("    mode = None\n", "    mode = None\n    old = 'x'\n"),
        CONFIG_WRITER.format("del Config.old"),
        ("del Config.old", "del Config.mode"),
    ),
    "the same module": (CONFIG + "\n\ndef fill(k):\n    Config.mode = 'x'\n", FILL_HERE),
    "a classmethod's cls": (
        CONFIG.replace(
            "    items = []\n",
            "    items = []\n\n    @classmethod\n    def set(cls):\n        cls.mode = 'x'\n",
        ),
        CONFIG_WRITER.format("Config.set()"),
    ),
    "a singleton": (
        "class S:\n    _instance = None\n\n    @classmethod\n    def get(cls):\n"
        "        if cls._instance is None:\n            cls._instance = cls()\n"
        "            cls._instance.level = 'x'\n        return cls._instance\n\n\n"
        "def peek(k):\n    return S._instance\n",
        "from pkg.state import S\n\n\ndef fill(k):\n    S.get()\n",
    ),
    "type(self)": (
        "class R:\n    _cache = {}\n\n    def put(self, k):\n"
        "        type(self)._cache[k] = 'x'\n\n\n"
        "def peek(k):\n    return R._cache.get(k)\n",
        "from pkg.state import R\n\n\ndef fill(k):\n    R().put(k)\n",
    ),
    "self.__class__": (
        "class R:\n    count = 0\n\n    def put(self, k):\n        self.__class__.count = 'x'\n\n\n"
        "def peek(k):\n    return R.count\n",
        "from pkg.state import R\n\n\ndef fill(k):\n    R().put(k)\n",
    ),
    "a class-level container through self": (
        "class R:\n    _items = []\n\n    def put(self, k):\n        self._items.append('x')\n\n"
        "    def count(self):\n        return len(self._items)\n\n\n"
        "def peek(k):\n    return R().count()\n",
        "from pkg.state import R\n\n\ndef fill(k):\n    R().put(k)\n",
    ),
    "read through a subclass": (
        CONFIG.replace("return Config.mode, Config.items", "return Sub.mode")
        + "\n\nclass Sub(Config):\n    pass\n",
        CONFIG_WRITER.format("Config.mode = 'x'"),
    ),
    "the instance a classmethod returns": (
        "class Config:\n    _instance = None\n\n    @classmethod\n    def instance(cls):\n"
        "        if cls._instance is None:\n            cls._instance = cls()\n"
        "        return cls._instance\n\n\ndef peek(k):\n    return Config._instance\n",
        "from pkg.state import Config\n\n\ndef fill(k):\n    Config.instance().mode = 'x'\n",
    ),
}


@pytest.mark.parametrize("case", list(CLASS_STATE))
def test_t6_state_held_on_a_class_reaches_its_readers(repo, case):
    """Class attributes are no symbols: a write onto one is a write of the
    class (a ``mutated_by`` edge), and every reader of the class reads it."""
    assert _state_plan(repo, *CLASS_STATE[case]) == STATE_SELECTED


CACHE_PEEK = "\n\ndef peek(k):\n    return _CACHE.get(k)\n"


def _cache(body, head="_CACHE = {}", read=CACHE_PEEK):
    return f"{head}\n\n\ndef fill(k):\n{body}{read}"


PEEK_A = "\n\ndef peek(k):\n    return _CACHE['a']\n"
WRITE_INTO = "\n\ndef _write(d, k):\n    d[k] = 'x'\n"
ALIASES = {
    "a local": _cache("    c = _CACHE\n    c[k] = 'x'\n"),
    "a local, a mutator": _cache("    c = _CACHE\n    c.update(k='x')\n"),
    "two locals": _cache("    c = _CACHE\n    d = c\n    d[k] = 'x'\n"),
    "a walrus": _cache("    if (c := _CACHE) is not None:\n        c[k] = 'x'\n"),
    "a loop over a tuple": _cache("    for c in (_CACHE,):\n        c[k] = 'x'\n"),
    "a with target": _cache(
        "    with contextlib.nullcontext(_CACHE) as c:\n        c[k] = 'x'\n",
        "import contextlib\n\n_CACHE = {}",
    ),
    "an unpacked pair": _cache("    c, o = _CACHE, {}\n    c[k] = 'x'\n"),
    "a conditional": _cache("    c = _CACHE if k else {}\n    c[k] = 'x'\n"),
    "a later binding in a loop": _cache(
        "    c = None\n    for _ in range(2):\n        if c is not None:\n            c[k] = 'x'\n"
        "        c = _CACHE\n"
    ),
    "a nested function": _cache(
        "    c = _CACHE\n\n    def put():\n        c[k] = 'x'\n\n    put()\n"
    ),
    "an item": _cache("    d = _CACHE['a']\n    d[k] = 'x'\n", "_CACHE = {'a': {}}", PEEK_A),
    "an item's mutator": _cache(
        "    d = _CACHE['a']\n    d.append('x')\n", "_CACHE = {'a': []}", PEEK_A
    ),
    "an item got": _cache(
        "    d = _CACHE.get('a')\n    d[k] = 'x'\n", "_CACHE = {'a': {}}", PEEK_A
    ),
    "a defaultdict entry": _cache(
        "    lst = _CACHE[k]\n    lst.append('x')\n",
        "import collections\n\n_CACHE = collections.defaultdict(list)",
    ),
    "a loop over the values": _cache(
        "    for d in _CACHE.values():\n        d[k] = 'x'\n", "_CACHE = {'a': {}}", PEEK_A
    ),
    "an attribute of an item": _cache(
        "    b = _CACHE['a']\n    b.v = 'x'\n",
        "class Box:\n    v = None\n\n\n_CACHE = {'a': Box()}",
        PEEK_A,
    ),
    "a bound method at module level": _cache(
        "    _put(k, 'x')\n", "_CACHE = {}\n_put = _CACHE.__setitem__"
    ),
    "a bound method in the function": _cache("    put = _CACHE.update\n    put(k='x')\n"),
    "globals()": _cache("    globals()['_CACHE'][k] = 'x'\n"),
    "what a getter returns": _cache(
        "    registry()[k] = 'x'\n",
        "_CACHE = {}\n\n\ndef registry():\n    return _CACHE",
    ),
    "a local bound to what a getter returns": _cache(
        "    r = registry()\n    r.update(k='x')\n",
        "_CACHE = {}\n\n\ndef registry():\n    return _CACHE",
    ),
    "a local handed to a writer": _cache(
        "    for c in (_CACHE,):\n        _write(c, k)\n", "_CACHE = {}", CACHE_PEEK + WRITE_INTO
    ),
}


@pytest.mark.parametrize("case", list(ALIASES))
def test_t7_a_write_through_another_name_reaches_the_readers(repo, case):
    """A write in place through a name bound (anywhere in the function) to
    a module variable, an item of it or a method's result on it, or
    through a bound mutator, ``globals()`` or what a getter returns, is a
    write of the variable."""
    assert _state_plan(repo, ALIASES[case]) == STATE_SELECTED


def test_t7_an_alias_of_another_modules_variable_reaches_its_readers(repo):
    lib = (
        "from pkg import state\n\n\n"
        "def fill(k):\n    for c in (state._CACHE,):\n        c[k] = 'x'\n"
    )
    assert _state_plan(repo, "_CACHE = {}" + CACHE_PEEK, lib) == STATE_SELECTED


# A write into a new object (a copy, an instance's own attribute, a fresh
# instance) changes no state another reader shares.
OWN_OBJECTS = {
    "a copy": _cache("    c = dict(_CACHE)\n    c[k] = 'x'\n"),
    "a copy by method": _cache("    c = _CACHE.copy()\n    c.update(k='x')\n"),
    "a display": _cache("    c = {**_CACHE}\n    c[k] = 'x'\n"),
    "a deep copy": _cache(
        "    c = copy.deepcopy(_CACHE)\n    c[k] = 'x'\n", "import copy\n\n_CACHE = {}"
    ),
    "an instance's own attribute": (
        "class R:\n    items = []\n\n    def __init__(self):\n        self.items = []\n\n"
        "    def put(self, k):\n        self.items.append('x')\n\n\n"
        "def peek(k):\n    return R().items\n\n\ndef fill(k):\n    R().put(k)\n"
    ),
    "a dataclass field": (
        "import dataclasses\n\n\n@dataclasses.dataclass\nclass R:\n"
        "    items: list = dataclasses.field(default_factory=list)\n\n"
        "    def put(self, k):\n        self.items.append('x')\n\n\n"
        "def peek(k):\n    return R().items\n\n\ndef fill(k):\n    R().put(k)\n"
    ),
    "a new instance": (
        "class R:\n    v = None\n\n    @classmethod\n    def make(cls):\n        return cls()\n\n\n"
        "def peek(k):\n    return R.v\n\n\ndef fill(k):\n    r = R.make()\n    r.v = 'x'\n"
    ),
    "a base's __init__ on the instance": (
        "class B:\n    def __init__(self):\n        self.v = None\n\n\n"
        "class R(B):\n    def __init__(self):\n        B.__init__(self)\n        self.w = 'x'\n\n\n"
        "def peek(k):\n    return B().v\n\n\ndef fill(k):\n    R()\n"
    ),
}


@pytest.mark.parametrize("case", list(OWN_OBJECTS))
def test_a_write_into_an_object_of_its_own_changes_no_shared_state(repo, case):
    """The bounds of the rules above: the readers of what was copied, or of
    the class, are not selected."""
    assert _state_plan(repo, OWN_OBJECTS[case]) == {"tests/test_a.py::test_a"}


# A module variable whose one binding is an immutable value (a string, a
# number, a tuple of such, a compiled pattern) cannot be changed in place:
# whatever ``fill`` does with it, or with what its methods return, its
# readers read what they read before. Rebinding it is no write in place: a
# variable some code binds anew, or bound to anything else, keeps every
# writer (audit round 6, T20).
WRITER_HERE = "\n\ndef _write(d, k):\n    d[k] = 'x'\n"


def _constant(value, read="_C", prelude=""):
    return f"{prelude}_C = {value}\n\n\ndef peek(k):\n    return {read}\n"


def _lib(body, prelude=""):
    head = f"{prelude}from pkg import state\nfrom pkg.state import _C{WRITER_HERE}"
    return f"{head}\n\ndef fill(k):\n{body}    return 'x'\n"


IMMUTABLE_HANDLED = {
    "a string handed to a writer": (_constant("'CREATE TABLE t (a)'"), _lib("    _write(_C, k)\n")),
    "a string handed to an unknown receiver's method": (
        _constant("'CREATE TABLE t (a)'"),
        _lib(
            "    conn.executescript(_C)\n",
            "class Conn:\n    def __init__(self, inner):\n        self._inner = inner\n\n"
            "    def executescript(self, sql):\n        return self._inner.execute(sql)\n\n\n"
            "class Batch:\n    def execute(self, items):\n        items.append(1)\n\n\n"
            "conn = Conn(object())\n",
        ),
    ),
    "a dedented string": (
        _constant(
            "textwrap.dedent('''\\n    CREATE TABLE t (a)\\n''')", prelude="import textwrap\n\n"
        ),
        _lib("    _write(_C, k)\n"),
    ),
    "a float handed on as a keyword": (
        _constant("20.0"),
        _lib(
            "    r = get(k)\n    r.status = 'x'\n",
            "import requests\n\n\ndef get(url):\n    return requests.get(url, timeout=_C)\n\n\n",
        ),
    ),
    "an f-string and a concatenation": (
        _constant("f'{_P}:key' + '!' + _P.upper()", prelude="_P = 'pkg'\n"),
        _lib("    _write(_C, k)\n"),
    ),
    "a frozen tuple": (_constant("('a', ('b', 1), -2, None)"), _lib("    _write(_C, k)\n")),
    "an element of a frozen tuple": (
        _constant("('a', ('b', 1))"),
        _lib("    for kind in _C:\n        _write(kind, k)\n"),
    ),
    "a frozenset": (
        _constant("frozenset({('a', 1), ('b', 2)})"),
        _lib("    c = _C\n    c.update(k)\n"),
    ),
    "a compiled pattern's results": (
        _constant("re.compile(r'(a)')", "_C.pattern", "import re\n\n"),
        _lib("    found = _C.findall(k)\n    found.append('x')\n"),
    ),
    "what a function returns of a pattern's match": (
        _constant("re.compile(r'(a)')", "_C.pattern", "import re\n\n"),
        _lib(
            "    named(k).append('x')\n",
            "def named(k):\n    m = _C.match(k)\n    return m.groups()\n\n\n",
        ),
    ),
    "a pattern through the module": (
        _constant("compile(r'(a)')", "_C.pattern", "from re import compile\n\n"),
        _lib("    m = state._C.match(k)\n    m.extra = 'x'\n"),
    ),
}


@pytest.mark.parametrize("case", list(IMMUTABLE_HANDLED))
def test_t20_an_immutable_variable_has_no_writers(repo, case):
    """Nothing ``fill`` does reaches the constant's readers."""
    assert _state_plan(repo, *IMMUTABLE_HANDLED[case]) == {"tests/test_a.py::test_a"}


APPEND = "    state._C.append('x')\n"
STILL_WRITTEN = {
    "rebound under global": (
        _constant("''") + "\n\ndef reset():\n    global _C\n    _C = []\n",
        _lib(APPEND),
    ),
    "rebound under global by the changed code": (
        _constant("''") + "\n\ndef fill(k):\n    global _C\n    _C = 'x'\n",
        FILL_HERE,
    ),
    "augmented under global by the changed code": (
        _constant("''") + "\n\ndef fill(k):\n    global _C\n    _C += 'x'\n",
        FILL_HERE,
    ),
    "a tuple holding a list": (_constant("([],)"), _lib("    _C[0].append('x')\n")),
    "bound again at module level": (_constant("''") + "\n\n_C = []\n", _lib(APPEND)),
    "bound in two branches": (
        "try:\n    _C = ''\nexcept ImportError:\n    _C = []\n\n\ndef peek(k):\n    return _C\n",
        _lib(APPEND),
    ),
    "bound to a name": (_constant("_D", prelude="_D = []\n"), _lib(APPEND)),
    "bound to a call of project code": (
        _constant("make()", prelude="def make():\n    return []\n\n\n"),
        _lib(APPEND),
    ),
    "bound to a rebound constant": (
        _constant("_D", prelude="_D = ''\n") + "\n\ndef reset():\n    global _D\n    _D = []\n",
        _lib(APPEND),
    ),
    "a module attribute bound from another module": (
        _constant("''"),
        _lib(APPEND, "def init():\n    state._C = []\n\n\n"),
    ),
    "augmented from another module": (_constant("''"), _lib("    state._C += 'x'\n")),
    "setattr on the module": (
        _constant("''"),
        _lib(APPEND, "def init():\n    setattr(state, '_C', [])\n\n\n"),
    ),
    "setattr on the module by a computed name": (
        _constant("''"),
        _lib(APPEND, "def init(name):\n    setattr(state, name, [])\n\n\n"),
    ),
    "the module's __dict__": (
        _constant("''"),
        _lib(APPEND, "def init():\n    state.__dict__['_C'] = []\n\n\n"),
    ),
    "vars() of the module": (
        _constant("''"),
        _lib(APPEND, "def init():\n    vars(state)['_C'] = []\n\n\n"),
    ),
    "sys.modules": (
        _constant("''"),
        _lib(APPEND, "import sys\n\n\ndef init():\n    sys.modules['pkg.state']._C = []\n\n\n"),
    ),
    "globals() in its module": (
        _constant("''") + "\n\ndef reset():\n    globals()['_C'] = []\n",
        _lib(APPEND),
    ),
    "the module handed to a writer": (
        _constant("''"),
        _lib(APPEND, "def init(mod):\n    mod._C = []\n\n\ndef setup():\n    init(state)\n\n\n"),
    ),
    "import_module": (
        _constant("''"),
        _lib(
            APPEND,
            "import importlib\n\n\n"
            "def init():\n    importlib.import_module('pkg.state')._C = []\n\n\n",
        ),
    ),
    "a module found at run time": (
        _constant("''"),
        _lib(
            APPEND,
            "import importlib\n\n\n"
            "def init(name):\n    importlib.import_module(name)._C = []\n\n\n",
        ),
    ),
    "another name for the module": (
        _constant("''"),
        _lib(APPEND, "def init():\n    m = state\n    m._C = []\n\n\n"),
    ),
    "the module held by another object": (
        _constant("''"),
        _lib(
            APPEND,
            "class Holder:\n    def __init__(self, mod):\n        self.mod = mod\n\n"
            "    def reset(self):\n        self.mod._C = []\n\n\n"
            "HOLDER = Holder(state)\n\n\n",
        ),
    ),
    "the module handed to a library": (
        _constant("''"),
        _lib(APPEND, "import somelib\n\n\ndef init():\n    somelib.configure(state)\n\n\n"),
    ),
    "a patch never stopped": (
        _constant("''"),
        _lib(
            APPEND,
            "from unittest import mock\n\n\n"
            "def init():\n    mock.patch.object(state, '_C', []).start()\n\n\n",
        ),
    ),
    "a shadowed re": (
        _constant("re.compile(r'(a)')", "_C.pattern", "from pkg import re\n\n"),
        _lib("    found = _C.findall(k)\n    found.append('x')\n"),
    ),
}


def test_t20_an_imported_constant_bound_anew_where_imported(repo):
    """``from pkg.state import _C`` then ``_C = []``: the importing module's
    own ``_C`` is a list its readers see written."""
    lib = (
        "from pkg.state import _C\n\n_C = []\n\n\n"
        "def fill(k):\n    _C.append('x')\n\n\ndef peek(k):\n    return _C\n"
    )
    readers = {
        "tests/test_z.py": "from pkg.lib import peek\n\n\ndef test_z():\n    peek('k')\n",
        "benchmarks/bench_read.py": (
            "from pkg.lib import peek\n\n\ndef time_peek():\n    peek('k')\n"
        ),
    }
    assert _state_plan(repo, _constant("''"), lib, extra=readers) == STATE_SELECTED


@pytest.mark.parametrize("case", list(STILL_WRITTEN))
def test_t20_a_variable_bound_anew_keeps_its_writers(repo, case):
    """The bounds of the rule above: a variable some code may bind to a
    mutable object, or whose value is not immutable by construction, keeps
    the writers its readers depend on."""
    extra = {"pkg/re.py": "def compile(p):\n    return []\n"} if case == "a shadowed re" else {}
    assert _state_plan(repo, *STILL_WRITTEN[case], extra=extra) == STATE_SELECTED


# State a class holds (a class-level container) is shared by its instances:
# a write in place into a module variable holding an instance, through any
# name or method result, writes the class, whose readers read it (audit
# round 6, T21).
STORE = (
    "class Store:\n    _cache = {}\n\n    def cache(self):\n        return self._cache\n\n"
    "    @classmethod\n    def lookup(cls, k):\n        return cls._cache.get(k)\n\n\n"
    "STORE = Store()\n\n\ndef peek(k):\n    return Store._cache.get(k)\n"
)
STORE_WRITER = "from pkg import state\nfrom pkg.state import STORE\n\n\ndef fill(k):\n{}"
INSTANCE_OF_A_CLASS_WITH_STATE = {
    "a method returning the class-level container": (
        STORE,
        STORE_WRITER.format("    STORE.cache()[k] = 'x'\n"),
    ),
    "the class-level container through the instance": (
        STORE,
        STORE_WRITER.format("    STORE._cache[k] = 'x'\n"),
    ),
    "read in a classmethod": (
        STORE.replace("return Store._cache.get(k)", "return Store.lookup(k)"),
        STORE_WRITER.format("    STORE._cache.update(k='x')\n"),
    ),
    "a local alias of the instance": (
        STORE,
        STORE_WRITER.format("    s = state.STORE\n    s.cache().setdefault(k, 'x')\n"),
    ),
    "a lazily made singleton": (
        STORE.replace(
            "STORE = Store()\n",
            "STORE = None\n\n\ndef get_store():\n    global STORE\n    if STORE is None:\n"
            "        STORE = Store()\n    return STORE\n",
        ),
        "from pkg.state import get_store\n\n\ndef fill(k):\n    get_store()._cache[k] = 'x'\n",
    ),
    "made by a factory": (
        STORE.replace("STORE = Store()\n", "def make():\n    return Store()\n\n\nSTORE = make()\n"),
        STORE_WRITER.format("    STORE._cache[k] = 'x'\n"),
    ),
    "the container on a base": (
        "class Base:\n    _cache = {}\n\n\nclass Store(Base):\n    pass\n\n\n"
        "STORE = Store()\n\n\ndef peek(k):\n    return Base._cache.get(k)\n",
        STORE_WRITER.format("    STORE._cache[k] = 'x'\n"),
    ),
    "a class-level object's method result": (
        "class Registry:\n    def __init__(self):\n        self._items = {}\n\n"
        "    def entries(self):\n        return self._items\n\n\n"
        "class Store:\n    registry = Registry()\n\n\nSTORE = Store()\n\n\n"
        "def peek(k):\n    return Store.registry.entries().get(k)\n",
        STORE_WRITER.format("    STORE.registry.entries()[k] = 'x'\n"),
    ),
    "a method of the instance writing the container": (
        STORE.replace(
            "    @classmethod\n",
            "    def put(self, k):\n        self._cache[k] = 'x'\n\n    @classmethod\n",
        ),
        STORE_WRITER.format("    STORE.put(k)\n"),
    ),
    "an instance in a container display": (
        STORE.replace("STORE = Store()\n", "STORE = Store()\n_STORES = {'a': Store()}\n"),
        STORE_WRITER.format("    state._STORES['a']._cache[k] = 'x'\n"),
    ),
    "an instance under another instance's attribute": (
        STORE.replace(
            "STORE = Store()\n",
            "class Holder:\n    def __init__(self):\n        self.store = Store()\n\n\n"
            "STORE = Store()\nHOLDER = Holder()\n",
        ),
        STORE_WRITER.format("    state.HOLDER.store._cache[k] = 'x'\n"),
    ),
    "state a classmethod puts on the class": (
        "class Store:\n    @classmethod\n    def setup(cls):\n        cls._cache = {}\n\n\n"
        "Store.setup()\nSTORE = Store()\n\n\ndef peek(k):\n    return Store._cache.get(k)\n",
        STORE_WRITER.format("    STORE._cache[k] = 'x'\n"),
    ),
}


@pytest.mark.parametrize("case", list(INSTANCE_OF_A_CLASS_WITH_STATE))
def test_t21_a_write_into_an_instance_reaches_the_state_its_class_holds(repo, case):
    """``STORE._cache[k] = v``, ``STORE.cache()[k] = v``: what the instance
    reaches may be the class's own container, so the class's readers
    (``Store._cache``, ``cls._cache``, a base's) read the write."""
    assert _state_plan(repo, *INSTANCE_OF_A_CLASS_WITH_STATE[case]) == STATE_SELECTED


INSTANCE_OF_A_CLASS_WITHOUT_STATE = {
    "constants and methods only": (
        "import functools\n\n\nclass Store:\n    kind = 'store'\n    limits = (1, 2)\n"
        "    size = property(lambda self: len(self._cache))\n\n    def __init__(self):\n"
        "        self._cache = {}\n\n    @functools.cached_property\n    def name(self):\n"
        "        return self.kind\n\n\nSTORE = Store()\n\n\n"
        "def peek(k):\n    return Store.kind, Store.limits\n",
        STORE_WRITER.format("    STORE._cache[k] = 'x'\n"),
    ),
    "a dataclass": (
        "import dataclasses\n\n\n@dataclasses.dataclass\nclass Store:\n    kind: str = 'store'\n"
        "    items: dict = dataclasses.field(default_factory=dict)\n\n\n"
        "STORE = Store()\n\n\ndef peek(k):\n    return Store.kind\n",
        STORE_WRITER.format("    STORE.items[k] = 'x'\n"),
    ),
}


@pytest.mark.parametrize("case", list(INSTANCE_OF_A_CLASS_WITHOUT_STATE))
def test_t21_a_write_into_an_instance_of_a_class_without_state(repo, case):
    """The bound of the rule above: a class whose body binds only immutable
    values, functions and descriptors holds nothing an instance's write
    reaches, and its readers are not selected."""
    assert _state_plan(repo, *INSTANCE_OF_A_CLASS_WITHOUT_STATE[case]) == {
        "tests/test_a.py::test_a"
    }


# A fresh instance (``cls.__new__(cls)``, ``self.__class__.__new__(...)``,
# ``Store()``) is not the class it was made from: binding an attribute on it
# writes the instance alone. A write in place into it or into what one of its
# attributes holds may reach a class-level container, as through a module
# variable holding an instance (T21), and is a write of the class when the
# class may hold state (audit round 6, T22).
VERSION = (
    "import re\n\n\nclass _Base:\n    __slots__ = ()\n\n\nclass Version(_Base):\n"
    "    __slots__ = ('_v', '_key_cache')\n    __match_args__ = ('_v',)\n"
    "    _regex = re.compile(r'[0-9]+')\n\n    def __init__(self, v):\n"
    "        self._v = v\n        self._key_cache = None\n\n{method}\n\n"
    "def peek(k):\n    return Version(k)._v\n"
)
VERSION_FILL = "from pkg.state import Version\n\n\ndef fill(k):\n    Version(k).{call}\n"
REPLACE = "    def replace(self):\n        new = {new}\n{body}        return new\n"
MAKE = "    @classmethod\n    def make(cls):\n        new = {new}\n{body}        return new\n"
BINDS = "        new._key_cache = None\n        new._v = 'x'\n"
FRESH_PRECISION = {
    "self.__class__.__new__": (REPLACE, "self.__class__.__new__(self.__class__)", BINDS),
    "type(self).__new__": (REPLACE, "type(self).__new__(type(self))", BINDS),
    "object.__new__": (REPLACE, "object.__new__(self.__class__)", BINDS),
    "cls.__new__": (MAKE, "cls.__new__(cls)", BINDS),
    "super().__new__": (MAKE, "super().__new__(cls)", BINDS),
    "an alternative constructor": (MAKE, "cls('x')", "        new._key_cache = None\n"),
    "a write into an attribute, the class holding no state": (
        REPLACE,
        "self.__class__.__new__(self.__class__)",
        "        new._key_cache = []\n        new._key_cache.append('x')\n",
    ),
}


@pytest.mark.parametrize("case", list(FRESH_PRECISION))
def test_t22_an_attribute_bound_on_a_fresh_instance_is_no_class_state(repo, case):
    """``new = self.__class__.__new__(self.__class__); new._v = v`` (as
    ``Version.__replace__`` in packaging does) binds the attribute on the
    new object only: the readers of ``Version`` read nothing it writes."""
    template, new, body = FRESH_PRECISION[case]
    method = template.format(new=new, body=body)
    call = "make()" if template is MAKE else "replace()"
    state = VERSION.format(method=method)
    assert _state_plan(repo, state, VERSION_FILL.format(call=call)) == {"tests/test_a.py::test_a"}


def test_t22_an_attribute_bound_on_a_fresh_instance_of_a_class_holding_state(repo):
    """The class holds a container, but the bind is on the new object."""
    state = (
        "class Store:\n    _items = []\n\n    def copy(self):\n"
        "        new = self.__class__.__new__(self.__class__)\n        new.v = 'x'\n"
        "        return new\n\n\ndef peek(k):\n    return Store._items\n"
    )
    lib = "from pkg.state import Store\n\n\ndef fill(k):\n    Store().copy()\n"
    assert _state_plan(repo, state, lib) == {"tests/test_a.py::test_a"}


def test_t22_a_write_into_an_attribute_the_fresh_instance_owns(repo):
    """``s.mine`` is bound in ``__init__`` and nowhere in a class body: a
    write into it changes the new object's own list, not ``Store._items``."""
    state = (
        "class Store:\n    _items = []\n\n    def __init__(self):\n        self.mine = []\n\n\n"
        "def peek(k):\n    return Store._items\n"
    )
    lib = "from pkg.state import Store\n\n\ndef fill(k):\n    s = Store()\n    s.mine.append('x')\n"
    assert _state_plan(repo, state, lib) == {"tests/test_a.py::test_a"}


FRESH_STORE = (
    "class Store:\n    _items = []\n    _cache = {{}}\n{methods}\n\n"
    "def peek(k):\n    return Store._items, Store._cache\n"
)
FRESH_WRITER = "from pkg.state import Store\n\n\ndef fill(k):\n{}"
PUT_INTO = "\n\ndef _put(obj):\n    obj._items.append('x')\n"
ADD_TO = "\n\ndef _add(d):\n    d.append('x')\n"


def _method(head, new, write="new._items.append('x')"):
    return f"\n{head}\n        new = {new}\n        {write}\n        return new\n"


CLASSMETHOD = "    @classmethod\n    def make(cls):"
COPY = "    def copy(self):"
FRESH_SOUND = {
    "a constructor's instance": (
        FRESH_STORE.format(methods=""),
        FRESH_WRITER.format("    s = Store()\n    s._items.append('x')\n"),
    ),
    "an item stored into the container": (
        FRESH_STORE.format(methods=""),
        FRESH_WRITER.format("    s = Store()\n    s._cache[k] = 'x'\n"),
    ),
    "an augmented container": (
        FRESH_STORE.format(methods=""),
        FRESH_WRITER.format("    s = Store()\n    s._items += ['x']\n"),
    ),
    "the class's __new__ called on it": (
        FRESH_STORE.format(methods=""),
        FRESH_WRITER.format("    s = Store.__new__(Store)\n    s._items.append('x')\n"),
    ),
    "cls.__new__ in a classmethod": (
        FRESH_STORE.format(methods=_method(CLASSMETHOD, "cls.__new__(cls)")),
        FRESH_WRITER.format("    Store.make()\n"),
    ),
    "object.__new__ in a classmethod": (
        FRESH_STORE.format(methods=_method(CLASSMETHOD, "object.__new__(cls)")),
        FRESH_WRITER.format("    Store.make()\n"),
    ),
    "an alternative constructor": (
        FRESH_STORE.format(methods=_method(CLASSMETHOD, "cls()")),
        FRESH_WRITER.format("    Store.make()\n"),
    ),
    "self.__class__.__new__": (
        FRESH_STORE.format(methods=_method(COPY, "self.__class__.__new__(self.__class__)")),
        FRESH_WRITER.format("    Store().copy()\n"),
    ),
    "type(self).__new__": (
        FRESH_STORE.format(methods=_method(COPY, "type(self).__new__(type(self))")),
        FRESH_WRITER.format("    Store().copy()\n"),
    ),
    "super().__new__ in __new__": (
        FRESH_STORE.format(
            methods="\n    def __new__(cls):\n        new = super().__new__(cls)\n"
            "        new._items.append('x')\n        return new\n"
        ),
        FRESH_WRITER.format("    Store()\n"),
    ),
    "what a factory returns": (
        FRESH_STORE.format(methods="") + "\n\ndef make():\n    return Store()\n",
        "from pkg.state import make\n\n\ndef fill(k):\n    make()._items.append('x')\n",
    ),
    "a container handed to a writer": (
        FRESH_STORE.format(methods=""),
        FRESH_WRITER.format("    s = Store()\n    _add(s._items)\n") + ADD_TO,
    ),
    "the instance handed to a writer": (
        FRESH_STORE.format(methods=""),
        FRESH_WRITER.format("    s = Store.__new__(Store)\n    _put(s)\n") + PUT_INTO,
    ),
    "a method of the instance writing the container": (
        FRESH_STORE.format(methods="\n    def put(self, v):\n        self._items.append(v)\n"),
        FRESH_WRITER.format("    s = Store()\n    s.put('x')\n"),
    ),
    "a method of an instance __new__ made, writing the container": (
        FRESH_STORE.format(methods="\n    def put(self, v):\n        self._items.append(v)\n"),
        FRESH_WRITER.format("    s = Store.__new__(Store)\n    s.put('x')\n"),
    ),
    "a method of a copy self.__class__.__new__ made": (
        FRESH_STORE.format(
            methods="\n    def put(self, v):\n        self._items.append(v)\n"
            + _method(COPY, "self.__class__.__new__(self.__class__)", "new.put('x')")
        ),
        FRESH_WRITER.format("    Store().copy()\n"),
    ),
    "an attribute other code puts on the class": (
        FRESH_STORE.format(methods="") + "\n\ndef setup():\n    Store.extra = []\n",
        FRESH_WRITER.format("    s = Store()\n    s.extra.append('x')\n"),
    ),
    "an attribute a __getattr__ may give": (
        FRESH_STORE.format(
            methods="\n    def __getattr__(self, name):\n        return self._cache[name]\n"
        ),
        FRESH_WRITER.format("    s = Store()\n    s.extra.append('x')\n"),
    ),
    "a module variable made by __new__": (
        FRESH_STORE.format(methods="") + "\n\nSTORE = Store.__new__(Store)\n",
        "from pkg.state import STORE\n\n\ndef fill(k):\n    STORE._items.append('x')\n",
    ),
    "a singleton its __new__ keeps": (
        "class S:\n    _instance = None\n\n    def __new__(cls):\n"
        "        if cls._instance is None:\n            cls._instance = super().__new__(cls)\n"
        "        return cls._instance\n\n\ndef peek(k):\n    return S._instance.mode\n",
        "from pkg.state import S\n\n\ndef fill(k):\n    s = S()\n    s.mode = 'x'\n",
    ),
}


@pytest.mark.parametrize("case", list(FRESH_SOUND))
def test_t22_a_write_into_a_fresh_instance_reaches_the_state_its_class_holds(repo, case):
    """The bounds of the rule above: a write in place into a fresh instance
    (into the class-level container one of its attributes is, however made
    or handed on) is a write of the class when the class holds state, and
    an instance a class's own ``__new__`` returns may be one it keeps."""
    assert _state_plan(repo, *FRESH_SOUND[case]) == STATE_SELECTED


# A class attribute bound once to a value that cannot change in place (a
# string, a number, a tuple of them) holds nothing a write reaches, as a
# module constant does (T20): a constructor handed ``TimeDelta.DAYS``, or a
# local taken from ``min(m, self._N)``, writes no state of the class
# (audit round 6, T24).
FIELDS = (
    "import abc\nfrom typing import Literal, cast\n\nUNITS = Literal['d', 's']\n\n\n"
    "class Field(metaclass=abc.ABCMeta):\n    errors = {{'invalid': 'bad'}}\n\n"
    "    def __init__(self, unit='s'):\n        self.unit = unit\n\n"
    "    def load(self, v):\n        self.last = v\n        return v\n\n\n"
    "class TimeDelta(Field):\n    DAYS = 'days'\n    LIMITS = (1, 2)\n    _N = 12\n"
    "    errors = {{'invalid': 'bad period'}}\n{body}\n\n"
    "def peek(k):\n    return TimeDelta.errors, TimeDelta.DAYS, UNITS\n"
)
FIELDS_LIB = (
    "from typing import cast\n\nfrom pkg.state import UNITS, TimeDelta\n\n\ndef fill(k):\n{}"
)
# An in-scope ``format`` that writes its receiver: a method of that name
# called on a value of unknown type may be it.
OUT = "\n\nclass Out:\n    def format(self, v):\n        self.v = v\n"
COUNT = (
    "    def count(self, m):\n        if m == 'x':\n            m += 1\n"
    "        m = min(m, self._N)\n"
)
IMMUTABLE_CLASS_ATTRIBUTES = {
    "handed to the constructor": (
        FIELDS.format(body=""),
        FIELDS_LIB.format("    f = TimeDelta(TimeDelta.DAYS)\n    f.load('x')\n"),
    ),
    "through a local": (
        FIELDS.format(body=""),
        FIELDS_LIB.format("    u = TimeDelta.DAYS\n    f = TimeDelta(u)\n    f.load('x')\n"),
    ),
    "a tuple of them": (
        FIELDS.format(body=""),
        FIELDS_LIB.format("    f = TimeDelta(TimeDelta.LIMITS)\n    f.load('x')\n"),
    ),
    "a local augmented after min() of one": (
        FIELDS.format(body="\n" + COUNT + "        return m\n"),
        FIELDS_LIB.format("    TimeDelta().count(1)\n"),
    ),
    "the target of typing.cast": (
        FIELDS.format(body=""),
        FIELDS_LIB.format("    g = cast(UNITS, k)\n    g += 'x'\n    return g\n"),
    ),
    "another attribute bound by the module's own code": (
        FIELDS.format(body="") + "\n\nTimeDelta.EXTRA = []\n",
        FIELDS_LIB.format("    f = TimeDelta(TimeDelta.DAYS)\n    f.load('x')\n"),
    ),
    "a method of an item of a literal table": (
        FIELDS.format(body=""),
        FIELDS_LIB.format("    return TimeDelta.errors['invalid'].format('x')\n") + OUT,
    ),
}


@pytest.mark.parametrize("case", list(IMMUTABLE_CLASS_ATTRIBUTES))
def test_t24_an_immutable_class_attribute_roots_nothing(repo, case):
    """``fields.TimeDelta(fields.TimeDelta.DAYS)`` (marshmallow) and then a
    method binding the instance's attribute, or ``m = min(m,
    self._MONTHS_PER_YEAR); m += 1`` (arrow), change nothing ``TimeDelta``
    holds: the readers of the class are not selected. ``cast(T, x)`` is
    ``x``, not ``T``; the module's own code binding another attribute
    (``Arrow.min = ...``) binds no ``DAYS``; a method called on an item of a
    table of literals nothing changes (``timeframes[k].format(n)``) is a
    builtin's."""
    assert _state_plan(repo, *IMMUTABLE_CLASS_ATTRIBUTES[case]) == {"tests/test_a.py::test_a"}


REBIND = "\n\ndef rebind():\n    {}\n"
MAY_CHANGE = {
    "a mutable class attribute": (
        FIELDS.format(body="    UNITS = []\n"),
        FIELDS_LIB.format("    f = TimeDelta(TimeDelta.UNITS)\n    f.unit.append('x')\n"),
    ),
    "a mutable class attribute handed to a writer": (
        FIELDS.format(body="    UNITS = []\n"),
        FIELDS_LIB.format("    _add(TimeDelta.UNITS)\n") + "\n\ndef _add(d):\n    d.append('x')\n",
    ),
    "bound anew by other code": (
        FIELDS.format(body="") + REBIND.format("TimeDelta.DAYS = []"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "bound anew through setattr": (
        FIELDS.format(body="") + REBIND.format("setattr(TimeDelta, 'DAYS', [])"),
        FIELDS_LIB.format("    d = TimeDelta.DAYS\n    d.append('x')\n"),
    ),
    "bound anew by the module's own code": (
        FIELDS.format(body="") + "\n\nTimeDelta.DAYS = []\n",
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "bound anew through cls in a base's classmethod": (
        FIELDS.format(body="").replace(
            "    def load(self, v):",
            "    @classmethod\n    def reset(cls):\n        cls.DAYS = []\n\n"
            "    def load(self, v):",
        ),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "bound anew through type() of an instance": (
        FIELDS.format(body="") + REBIND.format("type(TimeDelta()).DAYS = []"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "bound anew through a class handed to a function": (
        FIELDS.format(body="")
        + "\n\ndef _reset(c):\n    c.DAYS = []\n"
        + REBIND.format("_reset(TimeDelta)"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "bound twice in the class body": (
        FIELDS.format(body="    if UNITS:\n        DAYS = []\n"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "another class attribute's object": (
        FIELDS.format(body="    UNITS = []\n    DAYS = UNITS\n").replace("    DAYS = 'days'\n", ""),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "a decorated class": (
        FIELDS.format(body="").replace(
            "class TimeDelta(Field):",
            "def keep(c):\n    return c\n\n\n@keep\nclass TimeDelta(Field):",
        ),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "a metaclass of the project": (
        FIELDS.format(body="").replace(
            "class Field(metaclass=abc.ABCMeta):",
            "class Meta(type):\n    pass\n\n\nclass Field(metaclass=Meta):",
        ),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "an enum member": (
        "import enum\n\n\nclass Color(enum.Enum):\n    RED = 'r'\n\n\n"
        "def peek(k):\n    return Color.RED.label\n",
        "from pkg.state import Color\n\n\ndef fill(k):\n    c = Color.RED\n    c.label = 'x'\n",
    ),
    "read through self in a method": (
        FIELDS.format(
            body="    UNITS = []\n\n    def put(self, v):\n        self.UNITS.append(v)\n"
        ),
        FIELDS_LIB.format("    TimeDelta().put('x')\n"),
    ),
    "a method of an item of a table holding an object": (
        FIELDS.format(body="")
        .replace("    errors = {'invalid': 'bad period'}", "    errors = {'invalid': Out()}")
        .replace("\n\nclass Field", OUT + "\n\nclass Field"),
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a method of an item of a literal table written elsewhere": (
        FIELDS.format(body="") + REBIND.format("TimeDelta.errors['invalid'] = Out()") + OUT,
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a method of an item of a literal table handed to a function": (
        FIELDS.format(body="")
        + "\n\ndef _put(d):\n    d['invalid'] = Out()\n"
        + REBIND.format("_put(TimeDelta.errors)")
        + OUT,
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a literal table a subclass binds anew": (
        FIELDS.format(body="")
        + "\n\nclass Days(TimeDelta):\n    pass\n"
        + REBIND.format("Days.errors = {'invalid': Out()}")
        + OUT,
        FIELDS_LIB.replace("TimeDelta\n", "TimeDelta\nfrom pkg.state import Days\n").format(
            "    Days.errors['invalid'].format('x')\n"
        ),
    ),
    "a literal table its module's own code changes": (
        FIELDS.format(body="") + OUT + "\n\nTimeDelta.errors['invalid'] = Out()\n",
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a literal table changed through the class handed to a function": (
        FIELDS.format(body="")
        + "\n\ndef _put(c):\n    c.errors['invalid'] = Out()\n"
        + REBIND.format("_put(TimeDelta)")
        + OUT,
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a literal table changed through an instance": (
        FIELDS.format(body="") + REBIND.format("TimeDelta().errors['invalid'] = Out()") + OUT,
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a literal table changed through an instance handed to a function": (
        FIELDS.format(body="")
        + "\n\ndef _put(f):\n    f.errors['invalid'] = Out()\n"
        + REBIND.format("_put(TimeDelta())")
        + OUT,
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a literal table changed through what a function returns": (
        FIELDS.format(body="")
        + "\n\ndef _get():\n    return TimeDelta.errors\n"
        + REBIND.format("_get()['invalid'] = Out()")
        + OUT,
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a literal table changed through what a function returns, at import": (
        FIELDS.format(body="")
        + OUT
        + "\n\ndef _get():\n    return TimeDelta.errors\n\n\n_get()['invalid'] = Out()\n",
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "bound anew through what a function returns, at import": (
        FIELDS.format(body="") + "\n\ndef _cls():\n    return TimeDelta\n\n\n_cls().DAYS = []\n",
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "bound anew through __class__ of an instance": (
        FIELDS.format(body="") + REBIND.format("TimeDelta().__class__.DAYS = []"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
}


@pytest.mark.parametrize("case", list(MAY_CHANGE))
def test_t24_a_class_attribute_that_may_change_keeps_its_writers(repo, case):
    """The bounds of the rule above: a class attribute holding a container,
    or one some code (another module's, a classmethod's ``cls``,
    ``type(obj)``, the class handed to a function, the module's own
    top-level code, the class body itself) may bind anew, a decorated class
    or one a metaclass of the project makes, an enum member, and a table
    holding an object or one some code (a function it is handed to, a
    subclass bound anew) may change are state a write reaches."""
    assert _state_plan(repo, *MAY_CHANGE[case]) == STATE_SELECTED


# What a constructor is handed is part of the new instance (T7), but binding
# an attribute of the instance (``self.position = 0``, ``self.next_token =
# tok``), or augmenting one whose every value is an immutable of a builtin
# type (``self.position += len(text)``), changes nothing the instance holds:
# ``Tokenizer(source, rules=RULES)`` handed to a parser whose methods only do
# that writes no ``RULES`` (packaging; audit round 6, T23).
INIT = (
    "    def __init__(self, source, rules):\n        self.source = source\n"
    "        self.rules = rules\n        self.position = 0\n        self.next_token = None\n"
)
TOKENIZER = (
    "RULES = {{'x': 1}}\n\n\nclass Tokenizer:\n{extra}{init}\n"
    "    def read(self):\n        token = self.next_token\n"
    "        self.position += len(self.source)\n        self.next_token = None\n"
    "        return token\n\n"
    "    def poke(self, v):\n        self.rules[v] = 1\n\n\n"
    "def peek(k):\n    return RULES.get(k)\n"
)
TOKENIZER_LIB = (
    "from pkg.state import RULES, Tokenizer\n\n\n"
    "def fill(k):\n    tok = Tokenizer('x', rules=RULES)\n{}"
    "\n\ndef _parse(tokenizer):\n    tokenizer.read()\n"
    "\n\ndef _mark(t):\n    t.seen = True\n"
    "\n\ndef _poke(t):\n    t.rules['x'] = 2\n"
)
NOTHING_HANDED_WRITTEN = {
    "a parser reading the tokenizer": TOKENIZER_LIB.format("    _parse(tok)\n"),
    "an attribute bound by the caller": TOKENIZER_LIB.format("    tok.position = 1\n"),
    "an attribute bound by a callee": TOKENIZER_LIB.format("    _mark(tok)\n"),
    "computed names stored on parameters and modules elsewhere": "import sys\n\n"
    + TOKENIZER_LIB.format("    _parse(tok)\n")
    + "\n\ndef _tag(f, name):\n    setattr(f, name, RULES)\n    vars(f)[name] = RULES\n"
    + "    setattr(sys, name, RULES)\n"
    + "\n\ndef _attributes(**attrs):\n    def deco(func):\n        for n, v in attrs.items():\n"
    + "            setattr(func, n, v)\n        return func\n\n    return deco\n",
}


@pytest.mark.parametrize("case", list(NOTHING_HANDED_WRITTEN))
def test_t23_binding_attributes_of_an_instance_writes_nothing_it_was_handed(repo, case):
    """``Tokenizer(source, rules=RULES)`` and then the instance's own
    attributes bound or augmented with an ``int`` (computed names stored
    elsewhere on a parameter or a module bind nothing on it): the readers
    of ``RULES`` are not selected."""
    lib = NOTHING_HANDED_WRITTEN[case]
    state = TOKENIZER.format(extra="", init=INIT)
    assert _state_plan(repo, state, lib) == {"tests/test_a.py::test_a"}


ORDERED = "    def __lt__(self, other):\n        return self.position < other.position\n\n"


def test_t23_a_class_functools_orders_is_made_as_its_body_says(repo):
    """``@functools.total_ordering`` gives back the class itself, adding
    comparisons: binding the instance's attributes, in ``__init__`` or
    after, writes nothing it was handed (packaging's ``BoundaryVersion(v,
    BoundaryKind.AFTER_POSTS)``)."""
    state = TOKENIZER.format(extra=ORDERED, init=INIT).replace(
        "class Tokenizer:", "@functools.total_ordering\nclass Tokenizer:"
    )
    state = "import functools\n\n" + state
    lib = TOKENIZER_LIB.format("    _parse(tok)\n")
    assert _state_plan(repo, state, lib) == {"tests/test_a.py::test_a"}


SETTER = "    @property\n    def pos(self):\n        return self.rules\n\n" + (
    "    @pos.setter\n    def pos(self, v):\n        self.rules['p'] = v\n\n"
)
OWN_INIT = "    def __init__(self, source, rules):\n{}"
HANDED_WRITTEN = {
    "a write through the attribute": ("", TOKENIZER_LIB.format("    tok.rules['x'] = 2\n")),
    "a method writing through it": ("", TOKENIZER_LIB.format("    tok.poke('x')\n")),
    "a callee writing through it": ("", TOKENIZER_LIB.format("    _poke(tok)\n")),
    "the attribute handed to a writer": (
        "",
        TOKENIZER_LIB.format("    _add(tok.rules)\n") + "\n\ndef _add(d):\n    d['x'] = 2\n",
    ),
    "the instance written in place": (
        "    def __setitem__(self, k, v):\n        self.rules[k] = v\n\n",
        TOKENIZER_LIB.format("    tok['x'] = 2\n"),
    ),
    "the initialiser writing it": (
        OWN_INIT.format("        self.rules = rules\n        self.rules['k'] = 2\n"),
        "from pkg.state import RULES, Tokenizer\n\n\ndef fill(k):\n    Tokenizer('x', RULES)\n",
    ),
    "stored in a container": (
        OWN_INIT.format("        self.parts = [rules]\n"),
        TOKENIZER_LIB.format("    tok.parts[0]['x'] = 2\n"),
    ),
    "handed to a base initialiser": (
        OWN_INIT.format("        super().__init__(rules)\n"),
        TOKENIZER_LIB.format("    tok.kept['x'] = 2\n"),
    ),
    "stored under a computed name": (
        OWN_INIT.format("        setattr(self, source, rules)\n"),
        TOKENIZER_LIB.format("    getattr(tok, 'x')['k'] = 2\n"),
    ),
    "stored through __dict__": (
        OWN_INIT.format("        self.__dict__.update(r=rules)\n"),
        TOKENIZER_LIB.format("    tok.r['x'] = 2\n"),
    ),
    "moved to another attribute": (
        "    def move(self):\n        self.kept = self.rules\n\n",
        TOKENIZER_LIB.format("    tok.move()\n    tok.kept['x'] = 2\n"),
    ),
    "an attribute that may hold it augmented": (
        "    def take(self):\n        self.acc = self.rules\n        self.acc += 'x'\n\n",
        TOKENIZER_LIB.format("    tok.take()\n"),
    ),
    "a property setter": (SETTER, TOKENIZER_LIB.format("    tok.pos = 'x'\n")),
    "a __setattr__ of the class": (
        "    def __setattr__(self, n, v):\n        object.__setattr__(self, n, v)\n"
        "        if n == 'seen':\n            self.rules['x'] = v\n\n",
        TOKENIZER_LIB.format("    _mark(tok)\n"),
    ),
    "a __new__ giving back what it was handed": (
        "    def __new__(cls, source, rules):\n        return rules\n\n",
        TOKENIZER_LIB.format("    tok.x = 2\n"),
    ),
    "a class decorator of the project": (ORDERED, TOKENIZER_LIB.format("    _parse(tok)\n")),
    "augmented, with a computed name stored on anything": (
        "",
        TOKENIZER_LIB.format("    _parse(tok)\n")
        + "\n\nSLOTS = []\n\n\ndef _anywhere(name):\n    setattr(SLOTS[0], name, RULES)\n",
    ),
    "augmented, with a descriptor of the class": (
        "    cached = Lazy()\n\n",
        TOKENIZER_LIB.format("    _parse(tok)\n"),
    ),
    "returned to a caller writing into it": (
        "",
        "from pkg.state import RULES, Tokenizer\n\n\n"
        "def make(k):\n    return Tokenizer(k, rules=RULES)\n\n\n"
        "def fill(k):\n    make('x').rules['k'] = 2\n",
    ),
    "__dict__ bound to it": (
        OWN_INIT.format("        self.__dict__ = rules\n"),
        TOKENIZER_LIB.format("    tok.position = 1\n"),
    ),
    "__dict__ bound to it by a method": (
        "    def adopt(self):\n        self.__dict__ = self.rules\n\n",
        TOKENIZER_LIB.format("    tok.adopt()\n    _mark(tok)\n"),
    ),
}


@pytest.mark.parametrize("case", list(HANDED_WRITTEN))
def test_t23_a_write_that_may_reach_what_an_instance_was_handed(repo, case):
    """The bounds of the rule above: a change in place of the instance or of
    any part of it (through the attribute, a method, a callee, ``tok[k] =
    v``, the initialiser itself, an attribute the argument was moved to, a
    container or base the initialiser hands it to, a computed name,
    ``__dict__``), binding an attribute that runs code of the class (a
    property setter, ``__setattr__``, a class decorator of the project), an
    augmented attribute that code storing under computed names on objects
    of unknown type, or a descriptor of the class, may have bound to the
    rules, an instance ``__new__`` may give back, or a factory's instance
    written by its caller writes ``RULES``."""
    extra, lib = HANDED_WRITTEN[case]
    init = INIT
    if extra.startswith("    def __init__"):
        extra, init = "", extra
    state = TOKENIZER.format(extra=extra, init=init)
    if case == "a __new__ giving back what it was handed":
        state = state.replace(
            "RULES = {'x': 1}\n", "class Box:\n    pass\n\n\nRULES = Box()\n"
        ).replace("return RULES.get(k)", "return getattr(RULES, k, None)")
    if case == "augmented, with a descriptor of the class":
        # Reading ``tok.cached`` may bind ``position`` to the rules.
        state = state.replace(
            "class Tokenizer:",
            "class Lazy:\n    def __get__(self, inst, owner):\n"
            "        inst.__dict__['position'] = RULES\n        return 0\n\n\n"
            "class Tokenizer:",
        )
    if case == "a class decorator of the project":
        # It may give back a class whose ``__setattr__`` writes the rules.
        state = state.replace(
            "class Tokenizer:", "def hook(c):\n    return c\n\n\n@hook\nclass Tokenizer:"
        )
    if case == "handed to a base initialiser":
        state = state.replace(
            "class Tokenizer:",
            "class Base:\n    def __init__(self, r):\n        self.kept = r\n\n\n"
            "class Tokenizer(Base):",
        )
    assert _state_plan(repo, state, lib) == STATE_SELECTED


def test_t23_a_bound_attribute_counts_with_no_parameter_writer_anywhere(repo):
    """``__new__`` gives back what it was handed, so ``tok.x = 'x'`` binds an
    attribute of ``RULES``: a write of it even when no function of the
    project writes a parameter."""
    state = (
        "class Box:\n    pass\n\n\nRULES = Box()\n\n\n"
        "class Tokenizer:\n    def __new__(cls, rules):\n        return rules\n\n\n"
        "def peek(k):\n    return getattr(RULES, k, None)\n"
    )
    lib = (
        "from pkg.state import RULES, Tokenizer\n\n\n"
        "def fill(k):\n    tok = Tokenizer(RULES)\n    tok.x = 'x'\n"
    )
    assert _state_plan(repo, state, lib) == STATE_SELECTED


def test_t23_a_mutable_class_attribute_handed_to_a_constructor(repo):
    """``Tokenizer(source, rules=Tokenizer.DEFAULT)`` with ``DEFAULT = {}``
    and a write through ``tok.rules``: a write of the class's state."""
    state = TOKENIZER.format(extra="    DEFAULT = {}\n\n", init=INIT).replace(
        "    return RULES.get(k)", "    return Tokenizer.DEFAULT.get(k)"
    )
    lib = (
        "from pkg.state import Tokenizer\n\n\n"
        "def fill(k):\n    tok = Tokenizer('x', rules=Tokenizer.DEFAULT)\n    tok.rules['x'] = 2\n"
    )
    assert _state_plan(repo, state, lib) == STATE_SELECTED


# A write onto the class of an object whose type the analysis does not know
# (``type(o).X = v``, ``o.__class__.items.append(v)`` with ``o`` a parameter)
# may be a write of any in-scope class that may have that attribute: a class
# binding or defining it, and its subclasses. Such a write is a write of each
# of them, as ``Conf.X = v`` is of ``Conf`` (audit round 6, T25).
CONF = (
    "class Conf:\n    X = 1\n\n    @classmethod\n    def registry(cls):\n"
    "        return cls._cache\n\n\nclass Conf2(Conf):\n    pass\n\n\n"
    "def peek(k):\n    return Conf.X\n"
)
CONF_HOLDING = (
    "class Conf:\n    items = []\n    _cache = {}\n\n    @classmethod\n"
    "    def registry(cls):\n        return cls._cache\n\n\n"
    "def peek(k):\n    return Conf.items, Conf._cache\n"
)
CONF_SUB = (
    "class Conf:\n    X = 1\n\n\nclass Sub(Conf):\n    pass\n\n\ndef peek(k):\n    return Sub.X\n"
)
CONF_LIB = "def fill(k):\n{}"
UNKNOWN_CLASS_WRITES = {
    "type() of a parameter": (CONF, CONF_LIB.format("    type(k).X = 'x'\n")),
    "__class__ of a parameter": (CONF, CONF_LIB.format("    k.__class__.X = 'x'\n")),
    "augmented": (CONF, CONF_LIB.format("    type(k).X += 'x'\n")),
    "augmented through __class__": (CONF, CONF_LIB.format("    k.__class__.X += 'x'\n")),
    "setattr": (CONF, CONF_LIB.format("    setattr(type(k), 'X', 'x')\n")),
    "setattr with a computed name": (CONF, CONF_LIB.format("    setattr(type(k), k, 'x')\n")),
    "delattr": (CONF, CONF_LIB.format("    delattr(type(k), 'X' if k else 'x')\n")),
    "deleted": (CONF, CONF_LIB.format("    if k == 'x':\n        del k.__class__.X\n")),
    "through a local alias": (CONF, CONF_LIB.format("    c = type(k)\n    c.X = 'x'\n")),
    "through the class's namespace": (
        CONF,
        CONF_LIB.format("    vars(type(k))['X'] = 'x'\n"),
    ),
    "handed to a function binding it": (
        CONF,
        CONF_LIB.format("    _set(type(k))\n") + "\n\ndef _set(c):\n    c.X = 'x'\n",
    ),
    "returned by a function": (
        CONF,
        CONF_LIB.format("    _cls(k).X = 'x'\n") + "\n\ndef _cls(o):\n    return type(o)\n",
    ),
    "of an attribute of the receiver": (
        CONF,
        "class Holder:\n    def __init__(self, t):\n        self.thing = t\n\n"
        "    def poke(self):\n        type(self.thing).X = 'x'\n\n\n"
        "def fill(k):\n    Holder(k).poke()\n",
    ),
    "of a subclass": (CONF_SUB, CONF_LIB.format("    type(k).X = 'x'\n")),
    "of a local that may be a new instance or a parameter": (
        CONF + "\n\nclass Other:\n    X = 2\n",
        "from pkg.state import Other\n\n\ndef fill(k):\n    o = Other() if k else k\n"
        "    type(o).X = 'x'\n",
    ),
    "an attribute no class binds": (
        "class Conf:\n    pass\n\n\ndef peek(k):\n    return getattr(Conf, 'flag', None)\n",
        CONF_LIB.format("    type(k).flag = 'x'\n"),
    ),
    "a special attribute every class has": (
        "class Conf:\n    pass\n\n\ndef peek(k):\n    return Conf.__doc__\n",
        CONF_LIB.format("    type(k).__doc__ = 'x'\n"),
    ),
    "a container changed in place": (
        CONF_HOLDING,
        CONF_LIB.format("    type(k).items.append('x')\n"),
    ),
    "an item stored through __class__": (
        CONF_HOLDING,
        CONF_LIB.format("    k.__class__._cache['a'] = 'x'\n"),
    ),
    "what a method of the class returns": (
        CONF_HOLDING,
        CONF_LIB.format("    type(k).registry()['a'] = 'x'\n"),
    ),
}


@pytest.mark.parametrize("case", list(UNKNOWN_CLASS_WRITES))
def test_t25_a_write_onto_the_class_of_an_unknown_object(repo, case):
    """``type(o).X = v`` or ``o.__class__.X = v`` where ``o`` is a parameter
    (``bump(Conf())`` in one test, ``Conf.X`` read in another): the class
    may be ``Conf``, so the readers of ``Conf`` read the write."""
    assert _state_plan(repo, *UNKNOWN_CLASS_WRITES[case]) == STATE_SELECTED


REBIND_PARAM = "\n\ndef rebind(o):\n    {}\n"
REBOUND_THROUGH_AN_UNKNOWN_CLASS = {
    "an immutable class attribute bound anew through type()": (
        FIELDS.format(body="") + REBIND_PARAM.format("type(o).DAYS = []"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "an immutable class attribute bound anew through __class__": (
        FIELDS.format(body="") + REBIND_PARAM.format("o.__class__.DAYS = []"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "an immutable class attribute bound anew by a computed name": (
        FIELDS.format(body="") + REBIND_PARAM.format("setattr(type(o), o.name, [])"),
        FIELDS_LIB.format("    TimeDelta.DAYS.append('x')\n"),
    ),
    "a literal table bound anew through type()": (
        FIELDS.format(body="") + REBIND_PARAM.format("type(o).errors = {'invalid': Out()}") + OUT,
        FIELDS_LIB.format("    TimeDelta.errors['invalid'].format('x')\n"),
    ),
    "a class holding only constants given a container through type()": (
        "class Store:\n    limits = (1, 2)\n\n\nSTORE = Store()\n\n\n"
        "def peek(k):\n    return Store.limits\n" + REBIND_PARAM.format("type(o).limits = []"),
        STORE_WRITER.format("    STORE.limits.append('x')\n"),
    ),
}


@pytest.mark.parametrize("case", list(REBOUND_THROUGH_AN_UNKNOWN_CLASS))
def test_t25_a_class_an_unknown_object_may_be_is_written(repo, case):
    """A write onto the class of an unknown object binds the attribute anew
    on every class that may own it: an attribute bound once to a value that
    cannot change (``DAYS = 'days'``) or a literal table may then hold a
    container, and a class whose body binds only constants may hold state
    an instance's write reaches (T21, T24)."""
    assert _state_plan(repo, *REBOUND_THROUGH_AN_UNKNOWN_CLASS[case]) == STATE_SELECTED


KNOWN_CLASS_WRITES = {
    "the class itself": (CONF, "from pkg.state import Conf\n\n\ndef fill(k):\n    Conf.X = 'x'\n"),
    "type() of a fresh instance": (
        CONF,
        "from pkg.state import Conf\n\n\ndef fill(k):\n    type(Conf()).X = 'x'\n",
    ),
}


@pytest.mark.parametrize("case", list(KNOWN_CLASS_WRITES))
def test_t25_a_write_onto_a_known_class_still_selects_its_readers(repo, case):
    """The rule for a class the analysis knows is unchanged (T6, T22)."""
    assert _state_plan(repo, *KNOWN_CLASS_WRITES[case]) == STATE_SELECTED


NOT_AN_OWNER = {
    "type(self) in a subclass's method": (
        CONF,
        "from pkg.state import Conf\n\n\nclass Mine(Conf):\n    def bump(self):\n"
        "        type(self).X = 'x'\n\n\ndef fill(k):\n    Mine().bump()\n",
    ),
    "a mock's own class": (
        CONF,
        "from unittest.mock import Mock, PropertyMock\n\n\ndef fill(k):\n    m = Mock()\n"
        "    type(m).X = PropertyMock(return_value='x')\n",
    ),
    "a class's name": (CONF, CONF_LIB.format("    n = type(k).__name__\n    n += 'x'\n")),
    "an attribute only another class binds": (
        CONF + "\n\nclass Other:\n    other = 1\n",
        CONF_LIB.format("    type(k).other = 'x'\n"),
    ),
}


@pytest.mark.parametrize("case", list(NOT_AN_OWNER))
def test_t25_a_write_onto_a_class_that_cannot_be_the_readers(repo, case):
    """The bound of the rule: ``type(self)`` in a method is that method's
    class (or a subclass), a mock's class is its own (``type(m).p =
    PropertyMock()``), a class's ``__name__`` is a string, and binding on a
    class of unknown type an attribute only some other class binds is no
    write of the reader's class."""
    assert _state_plan(repo, *NOT_AN_OWNER[case]) == {"tests/test_a.py::test_a"}


def test_t25_an_unrelated_attribute_keeps_immutable_class_attributes(repo):
    """T24's narrowing holds when the attribute written on an unknown class
    is one ``TimeDelta``'s family does not have."""
    state = FIELDS.format(body="") + REBIND_PARAM.format("type(o).unrelated = []")
    lib = FIELDS_LIB.format("    f = TimeDelta(TimeDelta.DAYS)\n    f.load('x')\n")
    assert _state_plan(repo, state, lib) == {"tests/test_a.py::test_a"}


# ------------------------------------------------- import-time writes to another module's variables
# Code that runs at import (a library module, a conftest, a test module's top
# level, a decorator or a default) and writes another module's variable
# reaches that variable's readers, which import none of it.


def test_s8_a_conftest_writing_another_modules_variable_reaches_its_readers(repo):
    base = repo.commit(
        {
            "lib/__init__.py": "",
            "lib/settings.py": "DEBUG = False\n",
            "lib/mode.py": "from lib import settings\n\n\ndef mode():\n    return settings.DEBUG\n",
            "lib/other.py": "def g():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/sub/__init__.py": "",
            "tests/sub/conftest.py": "from lib import settings\n\nsettings.DEBUG = False\n",
            "tests/sub/test_a.py": "def test_a():\n    pass\n",
            "tests/test_d.py": (
                "from lib.mode import mode\n\n\ndef test_d():\n    assert mode() is False\n"
            ),
            "tests/test_other.py": "from lib.other import g\n\n\ndef test_g():\n    g()\n",
        }
    )
    head = repo.commit(
        {"tests/sub/conftest.py": "from lib import settings\n\nsettings.DEBUG = True\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_d.py::test_d" in selected(plan)
    assert "tests/test_other.py::test_g" in unselected(plan)


READERS = {
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "pkg/state.py": STATE,
    "tests/test_b.py": (
        "from pkg.state import mode\n\n\ndef test_b():\n    assert mode() == 'slow'\n"
    ),
    "benchmarks/bench_mode.py": "from pkg.state import mode\n\n\ndef time_mode():\n    mode()\n",
    "benchmarks/bench_x.py": "def time_x():\n    pass\n",
}
CALL_SITES = {
    "library variable": {
        "pkg/config.py": 'from pkg.state import set_mode\n\nX = set_mode("slow")\n',
        "tests/test_a.py": "import pkg.config  # noqa\n\n\ndef test_a():\n    pass\n",
    },
    "library class attribute": {
        "pkg/config.py": 'from pkg.state import set_mode\n\n\nclass K:\n    x = set_mode("slow")\n',
        "tests/test_a.py": "import pkg.config  # noqa\n\n\ndef test_a():\n    pass\n",
    },
    "library module statement": {
        "pkg/config.py": 'from pkg.state import set_mode\n\nset_mode("slow")\n',
        "tests/test_a.py": "import pkg.config  # noqa\n\n\ndef test_a():\n    pass\n",
    },
    "test decorator": {
        "tests/test_a.py": (
            "import pytest\n\nfrom pkg.state import set_mode\n\n\n"
            '@pytest.mark.parametrize("x", [set_mode("slow")])\ndef test_a(x):\n    pass\n'
        ),
    },
    "test default": {
        "tests/test_a.py": (
            'from pkg.state import set_mode\n\n\ndef test_a(x=set_mode("slow")):\n    pass\n'
        ),
    },
    "test module variable": {
        "tests/test_a.py": (
            'from pkg.state import set_mode\n\nX = set_mode("slow")\n\n\ndef test_a():\n    pass\n'
        ),
    },
}


@pytest.mark.parametrize("case", list(CALL_SITES))
def test_evp6_import_time_call_argument_reaches_the_mutated_state(repo, case):
    """``X = set_mode("slow")`` -> ``"faster"`` leaves ``MODE`` holding
    something else (set_mode stores it through a helper): test_b and the
    benchmark read it through ``mode()``, which no import edge connects to
    the call site."""
    sites = CALL_SITES[case]
    base = repo.commit({**READERS, **sites})
    head = repo.commit(
        {p: t.replace('set_mode("slow")', 'set_mode("faster")') for p, t in sites.items()}
    )
    targets = [
        py_target("tests/test_a.py::test_a", "tests.test_a.test_a"),
        py_target("tests/test_b.py::test_b", "tests.test_b.test_b"),
        asv_target("bench_mode.time_mode", "benchmarks.bench_mode.time_mode"),
        asv_target("bench_x.time_x", "benchmarks.bench_x.time_x"),
    ]
    plan = repo.plan(base, head, targets, source_roots=["."])
    assert selected(plan) == {
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b",
        "bench_mode.time_mode",
    }
    why = reason(plan, "tests/test_b.py::test_b")
    ids = path_ids(why)
    assert ids[-4:-1] == ["pkg.state.MODE", "pkg.state._store", "pkg.state.set_mode"], ids
    assert [s.kind for s in why.path][-3:] == ["references", "called_at_import", "called_at_import"]
    assert why.changed_symbol == ids[-1]


# A write through a module attribute and an item or method.
W15_STORE = (
    "_CACHE = {'l': [], 'x': 1, 'y': 2, 'n': {}}\nREG = []\n\n\n"
    "def get():\n    return _CACHE.get('k'), list(REG)\n"
)
W15_WRITES = {
    "item through the module": "from pkg import store\n\nstore._CACHE['k'] = 'slow'\n",
    "method through the module": "from pkg import store\n\nstore.REG.append('slow')\n",
    "item through the package": "import pkg.store\n\npkg.store._CACHE['k'] = 'slow'\n",
    "update through an alias": "import pkg.store as s\n\ns._CACHE.update(k='slow')\n",
    "method on an item": "from pkg import store\n\nstore._CACHE['l'].append('slow')\n",
    "augmented item": "from pkg import store\n\nstore._CACHE['l'] += ['slow']\n",
    "deleted item": "from pkg import store\n\ndel store._CACHE['x' if 'slow' else 'y']\n",
    "nested item": "from pkg import store\n\nstore._CACHE['n']['j'] = 'slow'\n",
    "in a function": (
        "from pkg import store\n\n\ndef fill():\n    store._CACHE['k'] = 'slow'\n\n\nfill()\n"
    ),
}


def _w15_files(how):
    return {
        "pkg/store.py": W15_STORE,
        "tests/sub/__init__.py": "",
        "tests/sub/conftest.py": W15_WRITES[how],
        "tests/sub/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": "from pkg.store import get\n\n\ndef test_b():\n    assert get()\n",
    }


@pytest.mark.parametrize("how", list(W15_WRITES))
def test_w15_a_write_through_a_module_attribute(repo, how):
    files = _w15_files(how)
    change = {"tests/sub/conftest.py": W15_WRITES[how].replace("slow", "fast")}
    assert _static(repo, files, change) == {
        "tests/sub/test_a.py::test_a",
        "tests/test_b.py::test_b",
    }


# --------------------------------------------------------------------------- write-only sites
# A site that only empties or rebinds a variable does the same whatever the
# variable holds: it is no reader of what fills it. A site whose outcome
# depends on what the variable holds, or on what kind of object it is, is.

REG = """\
from pkg.calc import compute

REG = {init}


def register(k):
    REG[k] = compute(k)


def lookup(k):
    return REG.get(k)
"""
CALC = "def compute(k):\n    return k * 2\n"
CALC_B = CALC.replace("* 2", "* 3")
RESET = """\
import pytest

from pkg import reg


@pytest.fixture(autouse=True)
def _reset():
    yield
    {body}
"""
TEST_REG = (
    "from pkg.reg import lookup, register\n\n\ndef test_register():\n"
    "    register(1)\n    assert lookup(1) in (2, 3)\n"
)
BENCH_LOOKUP = "from pkg.reg import lookup\n\n\ndef time_lookup():\n    lookup(1)\n"
REGISTER, OTHER = "tests/test_reg.py::test_register", "tests/test_other.py::test_other"
LOOKUP_BENCH = asv_target("bench_reg.time_lookup", "benchmarks.bench_reg.time_lookup")


def _reg_files(init="{}", body="reg.REG.clear()", extra=None):
    return {
        **BASE,
        "pkg/calc.py": CALC,
        "pkg/reg.py": REG.format(init=init),
        "tests/conftest.py": RESET.format(body=body),
        "tests/test_reg.py": TEST_REG,
        "tests/test_other.py": "def test_other():\n    pass\n",
        "benchmarks/bench_reg.py": BENCH_LOOKUP,
        **(extra or {}),
    }


def _reg_plan(repo, base, head, **kwargs):
    return repo.plan(base, head, [NOOP, LOOKUP_BENCH], discover_runners=["pytest"], **kwargs)


@pytest.mark.parametrize(
    "body",
    ["reg.REG.clear()", "reg.REG.update({})", "reg.REG['k'] = 0", "reg.REG.pop('k', None)"],
)
def test_w14_a_reset_fixture_is_not_a_reader(repo, body):
    """An autouse fixture that only empties the registry does the same
    whatever the registry holds: a change to what fills it (``compute``,
    through ``register``) reaches the code reading it, not every test
    through the fixture. ``lookup`` reads it, so the benchmark timing it is
    selected."""
    base = repo.commit(_reg_files(body=body))
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, LOOKUP_BENCH.runner_id}
    assert unselected(plan) == {OTHER, NOOP.runner_id}


@pytest.mark.parametrize(
    "case",
    [
        # Reads the registry: what it holds decides what it does.
        {"body": "for k in list(reg.REG):\n        del reg.REG[k]"},
        {"body": "reg.REG.pop('k')"},  # raises when the key is missing
        {"body": "del reg.REG['k']"},
        # Not a builtin container: its own ``clear`` may read what it holds.
        {
            "init": "Registry()",
            "extra": {"pkg/registry.py": "class Registry(dict):\n    pass\n"},
        },
        # Something binds it anew: it may hold any kind of object.
        {"extra": {"pkg/swap.py": "from pkg import reg\n\n\ndef swap(new):\n    reg.REG = new\n"}},
        # A list's item store raises on what it holds (its length).
        {"init": "[0, 0, 0]", "body": "reg.REG[0] = 0"},
        # Emptying it runs the finalizers of what it held.
        {"extra": {"pkg/res.py": "class Res:\n    def __del__(self):\n        pass\n"}},
    ],
    ids=["iterates", "pop", "del", "custom class", "rebound", "list item", "finalizer"],
)
def test_w14_a_site_that_reads_stays_a_reader(repo, case):
    """Sites whose outcome depends on what the variable holds, or on what
    kind of object it is, are still readers."""
    files = _reg_files(
        case.get("init", "{}"), case.get("body", "reg.REG.clear()"), case.get("extra")
    )
    if case.get("init") == "Registry()":
        files["pkg/reg.py"] = "from pkg.registry import Registry\n" + files["pkg/reg.py"]
    base = repo.commit(files)
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, OTHER, LOOKUP_BENCH.runner_id}
    assert rules(plan, OTHER) == {"dependency"}


def test_w14_a_change_to_the_variable_itself_reaches_write_sites(repo):
    """The registry's own initialiser changing may change what kind of
    object it is: the fixture emptying it is affected."""
    base = repo.commit(_reg_files())
    head = repo.commit({"pkg/reg.py": REG.format(init="{'seed': 0}")})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, OTHER, LOOKUP_BENCH.runner_id}


MODE_REG = REG + "\n\nMODE = None\n\n\ndef set_mode():\n    global MODE\n    MODE = compute(0)\n"


def test_w14_rebinding_another_modules_variable_reads_nothing(repo):
    """``reg.MODE = None`` in a reset fixture binds the variable anew and
    reads nothing of it, so a change to what sets it does not reach the
    fixture's tests. Deleting the variable does."""
    files = _reg_files(body="reg.MODE = None")
    files["pkg/reg.py"] = MODE_REG.format(init="{}")
    files["tests/test_mode.py"] = (
        "from pkg import reg\n\n\ndef test_mode():\n    reg.set_mode()\n    assert reg.MODE == 0\n"
    )
    base = repo.commit(files)
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, "tests/test_mode.py::test_mode", LOOKUP_BENCH.runner_id}
    deleted = repo.commit({"pkg/reg.py": REG.format(init="{}")})
    plan = _reg_plan(repo, head, deleted)
    assert OTHER in selected(plan)


def test_w14_augmented_assignment_through_a_module_reads(repo):
    """``reg.MODE += 1`` reads the old value: the fixture is a reader."""
    files = _reg_files(body="reg.MODE += 0")
    files["pkg/reg.py"] = MODE_REG.format(init="{}").replace("MODE = None", "MODE = 0")
    base = repo.commit(files)
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert OTHER in selected(plan)


# ----------------------------------------------------------------- calls on a value of unknown type
# A call by name on a value of unknown type (``obj.install()``) is a
# module-level function only through a module object. A method called on an
# instance attribute (``self._client.get()``) is one of what every write of
# the attribute makes, when they all say.

INSTALL_REG = "REG = []\n\n\ndef install(x):\n    REG.append(x)\n"
READ = "from pkg.reg import REG\n\n\ndef count():\n    return len(REG)\n"


def _w20_caller_plan(repo, other: str):
    """A changed test calls ``install`` by name on an object of unknown type
    (``obj.install(...)``); ``pkg.reg.install`` writes ``REG``, which
    ``test_read`` reads: it is affected only if the call may be that
    ``install`` (``_add_caller_effects``, ``_import_call_effects``)."""
    files = {
        "pkg/__init__.py": "",
        "pkg/reg.py": INSTALL_REG,
        "pkg/read.py": READ,
        "tests/__init__.py": "",
        "tests/test_read.py": (
            "from pkg.read import count\n\n\ndef test_read():\n    assert count() >= 0\n"
        ),
        "tests/test_other.py": other,
    }
    base = repo.commit(files)
    head = repo.commit({"tests/test_other.py": other.replace("(1)", "(2)")})
    targets = [
        py_target("t::test_read", "tests.test_read.test_read"),
        py_target("t::test_other", "tests.test_other.test_other"),
        asv_target("bench.read", "pkg.read.count"),
    ]
    return repo.plan(base, head, targets)


def test_w20_a_call_by_name_reaches_no_module_level_writer(repo):
    plan = _w20_caller_plan(repo, "def test_other(obj=None):\n    obj.install(1)\n")
    assert selected(plan) == {"t::test_other"}


def test_w20_a_call_through_a_handed_on_module_reaches_the_writer(repo):
    plan = _w20_caller_plan(
        repo,
        "from pkg import reg\n\n\ndef call(m):\n    m.install(1)\n\n\n"
        "def test_other():\n    call(reg)\n",
    )
    assert selected(plan) == {"t::test_other", "t::test_read", "bench.read"}


CACHE = (
    "class Cache:\n    def __init__(self):\n        self.items = {}\n\n"
    "    def get(self, k):\n        self.items[k] = 1\n        return 1\n"
)
ART_STATE = (
    "from pkg.client import Artifact\n\nART = Artifact()\n\n\n"
    "def describe():\n    return ART.info()\n"
)
PEEK = "from pkg.state import ART\n\n\ndef peek():\n    return ART\n"


def _w21_plan(repo, client: str, extra: dict[str, str] | None = None):
    """``describe`` calls ``ART.info()``, and ``info`` calls ``get`` on
    ``self._client``. ``describe`` changes: ``peek``, a reader of ``ART``,
    is affected only if ``info`` may write its receiver, that is, if
    ``self._client.get`` may be a method writing its own receiver
    (``Cache.get``)."""
    files = {
        "pkg/__init__.py": "",
        "pkg/cache.py": CACHE,
        "pkg/client.py": client,
        "pkg/state.py": ART_STATE,
        "pkg/read.py": PEEK,
        "tests/__init__.py": "",
        "tests/test_read.py": (
            "from pkg.read import peek\n\n\ndef test_peek():\n    assert peek()\n"
        ),
        "tests/test_state.py": (
            "from pkg.state import describe\n\n\ndef test_describe():\n    describe()\n"
        ),
        **(extra or {}),
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/state.py": ART_STATE.replace("ART.info()", "ART.info() or 0")})
    targets = [
        py_target("t::test_peek", "tests.test_read.test_peek"),
        py_target("t::test_describe", "tests.test_state.test_describe"),
        asv_target("bench.peek", "pkg.read.peek"),
    ]
    return repo.plan(base, head, targets)


INFO = "\n    def info(self):\n        return self._client.get('x')\n"
CHANGED_ONLY = {"t::test_describe"}
WRITER_REACHES = {"t::test_describe", "t::test_peek", "bench.peek"}


@pytest.mark.parametrize(
    "init",
    [
        "import collections\n\n\nclass Artifact:\n    def __init__(self):\n"
        "        self._client = collections.OrderedDict()\n",
        "import collections\n\n\ndef make():\n    return collections.OrderedDict()\n\n\n"
        "class Artifact:\n    def __init__(self):\n        self._client = make()\n",
        "class Plain:\n    def get(self, k):\n        return k\n\n\n"
        "class Artifact:\n    def __init__(self):\n        self._client = Plain()\n",
    ],
    ids=["third-party", "third-party-factory", "in-scope-class"],
)
def test_w21_a_typed_instance_attribute_writes_nothing(repo, init):
    client = init + INFO
    plan = _w21_plan(repo, client)
    assert selected(plan) == CHANGED_ONLY


@pytest.mark.parametrize(
    ("client", "extra"),
    [
        # a construction of the class whose ``get`` writes its receiver
        (
            "from pkg.cache import Cache\n\n\nclass Artifact:\n    def __init__(self):\n"
            "        self._client = Cache()\n",
            {},
        ),
        # bound to a parameter (nothing says what it holds)
        (
            "class Artifact:\n    def __init__(self, client=None):\n"
            "        self._client = client\n",
            {},
        ),
        # rebound in another method to something unknown
        (
            "import collections\n\n\nclass Artifact:\n    def __init__(self):\n"
            "        self._client = collections.OrderedDict()\n\n"
            "    def use(self, c):\n        self._client = c\n",
            {},
        ),
        # written from outside
        (
            "import collections\n\n\nclass Artifact:\n    def __init__(self):\n"
            "        self._client = collections.OrderedDict()\n",
            {
                "pkg/patch.py": (
                    "from pkg.cache import Cache\nfrom pkg.state import ART\n\n"
                    "ART._client = Cache()\n"
                )
            },
        ),
        # by setattr
        (
            "import collections\n\n\nclass Artifact:\n    def __init__(self, **kw):\n"
            "        self._client = collections.OrderedDict()\n"
            "        for k, v in kw.items():\n            setattr(self, k, v)\n",
            {},
        ),
        # a class-level binding
        (
            "import collections\n\n\nclass Artifact:\n    _client = None\n\n"
            "    def __init__(self):\n        self._client = collections.OrderedDict()\n",
            {},
        ),
        # a subclass binding it otherwise
        (
            "import collections\nfrom pkg.cache import Cache\n\n\nclass Artifact:\n"
            "    def __init__(self):\n        self._client = collections.OrderedDict()\n"
            + INFO
            + "\n\nclass Cached(Artifact):\n    def __init__(self):\n"
            "        self._client = Cache()\n",
            {},
        ),
        # a class whose constructor may return another object
        (
            "from pkg.cache import Cache\n\n\nclass Odd:\n    def __new__(cls):\n"
            "        return Cache()\n\n\nclass Artifact:\n    def __init__(self):\n"
            "        self._client = Odd()\n",
            {},
        ),
    ],
    ids=[
        "writer-class",
        "parameter",
        "rebound",
        "outside",
        "setattr",
        "class-level",
        "subclass",
        "new",
    ],
)
def test_w21_an_untyped_instance_attribute_may_write(repo, client, extra):
    if "def info" not in client:
        client += INFO
    plan = _w21_plan(repo, client, extra)
    assert selected(plan) == WRITER_REACHES


# ------------------------------------------------------ writes through a module object however held
# A change to a module-level variable's object made through a module object
# other than a name the resolver binds (``import_module``, ``sys.modules``,
# ``__import__``, a function-local import, a module passed in or returned,
# ``getattr`` on a package, a star import), by any write form, is a
# ``mutated_by`` dependency of the code reading the variable. An object from
# anywhere may be any module's variable.

TABLES = (
    "def a():\n    return 1\n\n\ndef b():\n    return {0}\n\n\nTABLE = {{'a': a}}\n"
    "LIST = [a]\nNEST = {{'k': [a]}}\n\n\nclass O:\n    x = a\n\n\nOBJ = O()\n"
)
TABLE_READERS = {
    "TABLE": "def read():\n    return TABLE['x']()\n",
    "LIST": "def read():\n    return LIST[-1]()\n",
    "NEST": "def read():\n    return NEST['k'][-1]()\n",
    "OBJ": "def read():\n    return OBJ.x()\n",
}
GETS = {
    "import_module": ("import importlib", "importlib.import_module('pkg.reg')"),
    "sys_modules": ("import sys", "sys.modules['pkg.reg']"),
    "sys_modules_get": ("import sys", "sys.modules.get('pkg.reg')"),
    "dunder_import": ("", "__import__('pkg.reg', fromlist=['x'])"),
    "getattr_package": ("import pkg", "getattr(pkg, 'reg')"),
    "star_import_module": ("from importlib import *", "import_module('pkg.reg')"),
    "unpickled": ("import pickle", "pickle.loads(DATA)"),
    "import_any_name": ("import importlib", "importlib.import_module(NAME)"),
    "gc_object": ("import gc", "gc.get_objects()[0]"),
}
WRITES = {
    "item": ("TABLE", "{m}.TABLE['x'] = pkg.reg.b"),
    "update": ("TABLE", "{m}.TABLE.update(x=pkg.reg.b)"),
    "dunder_setitem": ("TABLE", "{m}.TABLE.__setitem__('x', pkg.reg.b)"),
    "augassign": ("TABLE", "{m}.TABLE |= {{'x': pkg.reg.b}}"),
    "append": ("LIST", "{m}.LIST.append(pkg.reg.b)"),
    "iadd": ("LIST", "{m}.LIST += [pkg.reg.b]"),
    "slice": ("LIST", "{m}.LIST[0:0] = [pkg.reg.b]"),
    "item_on_the_way": ("NEST", "{m}.NEST['k'].append(pkg.reg.b)"),
    "item_iadd": ("NEST", "{m}.NEST['k'] += [pkg.reg.b]"),
    "attribute": ("OBJ", "{m}.OBJ.x = pkg.reg.b"),
    "setattr": ("OBJ", "setattr({m}.OBJ, 'x', pkg.reg.b)"),
    "handed_on": ("TABLE", "_put({m}.TABLE)"),
}


def _l6_writer(imports: str, body: str) -> str:
    return (
        f"{imports}\n\nimport pkg.reg\n\n\ndef _put(t):\n    t['x'] = pkg.reg.b\n\n\n"
        f"def go(DATA=b'', NAME='x'):\n{body}"
    )


def _l6_plan(repo, variable: str, writer: str):
    files = {
        **ASV_BENCH,
        "pkg/__init__.py": "",
        "pkg/reg.py": TABLES.format("1"),
        "pkg/use.py": f"from pkg.reg import {variable}\n\n\n{TABLE_READERS[variable]}",
        "pkg/w.py": writer,
        "tests/__init__.py": "",
        "tests/test_a.py": "import pkg.w\n\n\ndef test_a():\n    pkg.w.go()\n",
        "tests/test_b.py": "from pkg.use import read\n\n\ndef test_b():\n    assert read() == 2\n",
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/reg.py": TABLES.format("2")})
    return repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])


BOTH = {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


@pytest.mark.parametrize("get", sorted(GETS))
def test_l6_an_item_stored_through_any_module_object_reaches_the_readers(repo, get):
    imports, expr = GETS[get]
    variable, write = WRITES["item"]
    plan = _l6_plan(repo, variable, _l6_writer(imports, f"    {write.format(m=expr)}\n"))
    assert selected(plan) == BOTH


@pytest.mark.parametrize("write", sorted(WRITES))
def test_l6_every_write_form_through_a_handle_reaches_the_readers(repo, write):
    variable, text = WRITES[write]
    imports, expr = GETS["import_module"]
    plan = _l6_plan(repo, variable, _l6_writer(imports, f"    {text.format(m=expr)}\n"))
    assert selected(plan) == BOTH


@pytest.mark.parametrize(
    "body",
    [
        "    import pkg.reg as m\n    m.TABLE['x'] = pkg.reg.b\n",
        "    m = importlib.import_module('pkg.reg')\n    m.TABLE['x'] = pkg.reg.b\n",
        "    _store(pkg.reg)\n\n\ndef _store(m):\n    m.TABLE['x'] = pkg.reg.b\n",
        "    _store(sys.modules['pkg.reg'])\n\n\ndef _store(m):\n    m.TABLE['x'] = pkg.reg.b\n",
        "    _get().TABLE['x'] = pkg.reg.b\n\n\ndef _get():\n"
        "    return importlib.import_module('pkg.reg')\n",
    ],
    ids=["local_import", "local_handle", "module_passed", "handle_passed", "handle_returned"],
)
def test_l6_a_module_object_however_held_reaches_the_readers(repo, body):
    plan = _l6_plan(repo, "TABLE", _l6_writer("import importlib\nimport sys", body))
    assert selected(plan) == BOTH


@pytest.mark.parametrize(
    "body",
    [
        "    gc.get_objects()[0]['x'] = pkg.reg.b\n",
        "    pickle.loads(DATA)['x'] = pkg.reg.b\n",
        "    for o in gc.get_objects():\n        o['x'] = pkg.reg.b\n",
    ],
    ids=["gc_item", "unpickled_item", "gc_loop"],
)
def test_l6_an_object_from_anywhere_changed_may_be_any_variable(repo, body):
    """An object nothing names (a ``gc`` list's element, an unpickled one)
    may be any module's variable: changing it reaches every variable's
    readers (the unbounded case)."""
    plan = _l6_plan(repo, "TABLE", _l6_writer("import gc\nimport pickle", body))
    assert selected(plan) == BOTH


# ------------------------------------------------------------- lazily initialised constants
# ``global V; if V is None: V = <pure value>`` in the one function reading V
# (diffcone.indexer.lazy): the writer computes the same value whoever runs it
# first, so code calling it earlier or later changes nothing it reads.

LAZY_EXC = """\
import os

_pkg_dir: str | None = None
_test_dir = None


def find_stack_level():
    global _pkg_dir, _test_dir
    if _pkg_dir is None or _test_dir is None:
        import pkg as pd

        _pkg_dir = os.path.dirname(pd.__file__) + os.sep
        _test_dir = os.path.join(_pkg_dir, "tests") + os.sep
    return len(_pkg_dir) + len(_test_dir)
"""
LAZY = {
    **BASE,
    "pkg/exc.py": LAZY_EXC,
    "pkg/a.py": (
        "from pkg.exc import find_stack_level\n\n\ndef warn_a():\n    return find_stack_level()\n"
    ),
    "pkg/b.py": (
        "from pkg.exc import find_stack_level\n\n\ndef warn_b():\n    return find_stack_level()\n"
    ),
    "pkg/config.py": "from pkg.a import warn_a\n\nLEVEL = warn_a()\n",
    "tests/test_a.py": "from pkg.a import warn_a\n\n\ndef test_a():\n    assert warn_a()\n",
    "tests/test_b.py": "from pkg.b import warn_b\n\n\ndef test_b():\n    assert warn_b()\n",
    "tests/test_config.py": (
        "from pkg.config import LEVEL\n\n\ndef test_config():\n    assert LEVEL\n"
    ),
}
LAZY_BENCHES = [
    asv_target("bench.time_a", "pkg.a.warn_a"),
    asv_target("bench.time_b", "pkg.b.warn_b"),
]
LAZY_CHANGES = {
    # A caller of the writer changed: it may now call it earlier or later.
    "caller body": ("pkg/a.py", "return find_stack_level()", "return find_stack_level() + 0"),
    # Import-time code that can run the writer changed.
    "import-time call": ("pkg/config.py", "LEVEL = warn_a()", "LEVEL = warn_a() + 1"),
}
LAZY_SELECTED = {
    "caller body": {"tests/test_a.py::test_a", "tests/test_config.py::test_config", "bench.time_a"},
    "import-time call": {"tests/test_config.py::test_config"},
}
LAZY_OUTSIDE_READER = (
    "import pkg.exc as pue\n\n\ndef test_dir():\n    pue.find_stack_level()\n"
    "    assert pue._pkg_dir\n"
)


def _lazy_plan(repo, files, change):
    base = repo.commit(files)
    path, old, new = LAZY_CHANGES[change]
    head = repo.commit({path: files[path].replace(old, new)})
    return repo.plan(base, head, LAZY_BENCHES, discover_runners=["pytest"])


@pytest.mark.parametrize("change", sorted(LAZY_CHANGES))
def test_a_lazy_constant_filled_earlier_or_later_reaches_no_reader(repo, change):
    """pandas' ``find_stack_level``: a change to code that can run it (a
    caller's body, import-time code) used to reach every caller of it
    through ``_pkg_dir``, which only it reads; that value is the same
    whoever computes it first, so only what the change itself reaches is
    selected."""
    plan = _lazy_plan(repo, LAZY, change)
    assert selected(plan) == LAZY_SELECTED[change]


@pytest.mark.parametrize("change", sorted(LAZY_CHANGES))
def test_a_lazy_constant_read_outside_its_writer_still_reaches_its_callers(repo, change):
    """Guard: code other than the writer reading the variable may read it
    before the writer ever ran, so filling it earlier or later is seen
    there; the write is followed as before (the writer is a reader again
    in static planning, which reaches every caller of it)."""
    files = {**LAZY, "tests/test_dir.py": LAZY_OUTSIDE_READER}
    plan = _lazy_plan(repo, files, change)
    assert selected(plan) == LAZY_SELECTED[change] | {
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b",
        "tests/test_dir.py::test_dir",
        "bench.time_a",
        "bench.time_b",
    }


def test_a_change_to_the_lazy_constants_writer_reaches_its_callers(repo):
    """A change to the writer itself is planned as any change."""
    base = repo.commit(LAZY)
    head = repo.commit({"pkg/exc.py": LAZY_EXC.replace('"tests"', '"test"')})
    plan = repo.plan(base, head, LAZY_BENCHES, discover_runners=["pytest"])
    assert selected(plan) == {
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b",
        "tests/test_config.py::test_config",
        "bench.time_a",
        "bench.time_b",
    }


# ---------------------------------------------------- process state written by hooks and collection
# Code a hook runs, or a collected module runs at import, runs before any
# test: what it writes into the process (an environment variable,
# ``sys.path``, warning filters) is there for every test, including one that
# imports none of it.

MODES = "def mode():\n    return 'a'\n"
MODES_B = MODES.replace("'a'", "'b'")
ENVSETUP = (
    "import os\n\nfrom pkg.modes import mode\n\n\ndef apply():\n    os.environ['MODE'] = mode()\n"
)
READS_ENV = "import os\n\n\ndef test_env():\n    assert os.environ.get('MODE', 'a') in 'ab'\n"
QUIET = "def test_quiet():\n    pass\n"
ENV, SUB = "tests/other/test_env.py::test_env", "tests/sub/test_s.py::test_s"
QUIET_ID = "tests/test_quiet.py::test_quiet"


# Hooks: a registered plugin object's methods, a sub-conftest's collection
# hook, a module registered as a plugin by name.
PLUGIN_OBJECT = """\
from tests import envsetup


class _Plugin:
    def {hook}(self, {arg}):
        envsetup.apply()


def pytest_configure(config):
    config.pluginmanager.register(_Plugin())
"""
GENERATE = """\
from tests import envsetup


def pytest_generate_tests(metafunc):
    envsetup.apply()
"""
HOOK_FILES = {
    **BASE,
    "pkg/modes.py": MODES,
    "tests/envsetup.py": ENVSETUP,
    "tests/sub/__init__.py": "",
    "tests/sub/test_s.py": "def test_s():\n    pass\n",
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": READS_ENV,
}
HOOK_CASES = [
    {"tests/conftest.py": PLUGIN_OBJECT.format(hook="pytest_sessionstart", arg="session")},
    # A path-scoped hook name on a plugin object: only conftests are scoped.
    {"tests/conftest.py": PLUGIN_OBJECT.format(hook="pytest_runtest_setup", arg="item")},
    {"tests/sub/conftest.py": GENERATE},
    # A module registered as a plugin by name at run time.
    {
        "tests/envplug.py": (
            "from tests import envsetup\n\n\ndef pytest_sessionstart(session):\n"
            "    envsetup.apply()\n"
        ),
        "tests/conftest.py": (
            "def pytest_configure(config):\n"
            "    config.pluginmanager.import_plugin('tests.envplug')\n"
        ),
    },
]
HOOK_IDS = [
    "plugin object",
    "plugin object, runtest hook",
    "sub-conftest generate_tests",
    "module plugin",
]


@pytest.mark.parametrize("conftests", HOOK_CASES, ids=HOOK_IDS)
def test_w11_code_a_hook_runs_reaches_every_test(repo, conftests):
    """A hook sets an environment variable through a helper, and a test in
    another directory reads it through ``os.environ``: no edge leads from
    the changed function to that test. The hook is a registered plugin
    object's method (pytest calls those for every test), or a collection
    hook in a sub-conftest (it runs before any test, and what it writes to
    the process stays)."""
    base = repo.commit({**HOOK_FILES, **conftests})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {ENV, SUB}
    assert rules(plan, ENV) == {"dependency"}
    assert NOOP.runner_id in unselected(plan)


def test_w11_collection_hook_writing_nothing_stays_scoped(repo):
    """A sub-conftest's ``pytest_generate_tests`` that writes no process
    state reaches only the tests under it."""
    base = repo.commit(
        {
            **HOOK_FILES,
            "tests/sub/conftest.py": (
                "from pkg.modes import mode\n\n\ndef pytest_generate_tests(metafunc):\n"
                "    metafunc.config.mode = mode()\n"
            ),
        }
    )
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {SUB}


def test_w11_hook_writing_an_in_scope_variable(repo):
    """State kept in an in-scope variable needs no session rule: the reader
    reaches the variable its writer mutates."""
    base = repo.commit(
        {
            **HOOK_FILES,
            "pkg/state.py": (
                "from pkg.modes import mode\n\nSTATE = {}\n\n\ndef apply():\n"
                "    STATE['mode'] = mode()\n\n\ndef current():\n    return STATE.get('mode')\n"
            ),
            "tests/sub/conftest.py": (
                "from pkg import state\n\n\ndef pytest_generate_tests(metafunc):\n"
                "    state.apply()\n"
            ),
            "tests/other/test_env.py": (
                "from pkg.state import current\n\n\ndef test_env():\n"
                "    assert current() in (None, 'a', 'b')\n"
            ),
        }
    )
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {ENV, SUB}


# Import-time code of what pytest collects: test modules, conftests and the
# modules they import.
SETS_AT_IMPORT = """\
import os

from pkg.modes import mode

os.environ['MODE'] = mode()


def test_sets():
    pass
"""
SETS = "tests/test_sets.py::test_sets"
IMPORT_FILES = {
    **BASE,
    "pkg/modes.py": MODES,
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": READS_ENV,
    "tests/test_quiet.py": QUIET,
}
IMPORT_CASES = [
    # A test module's own import-time code.
    ({"tests/test_sets.py": SETS_AT_IMPORT}, {SETS, ENV}),
    # A sub-conftest's.
    (
        {
            "tests/sub/__init__.py": "",
            "tests/sub/conftest.py": SETS_AT_IMPORT.split("\n\n\ndef")[0] + "\n",
            "tests/sub/test_s.py": "def test_s():\n    pass\n",
        },
        {SUB, ENV},
    ),
    # A helper module a test module imports, whose import-time code writes.
    (
        {
            "tests/envinit.py": SETS_AT_IMPORT.split("\n\n\ndef")[0] + "\n",
            "tests/test_sets.py": "import tests.envinit  # noqa: F401\n\n\ndef test_sets():\n"
            "    pass\n",
        },
        {SETS, ENV},
    ),
    # ``sys.path`` instead of the environment, through a called helper.
    (
        {
            "tests/paths.py": (
                "import sys\n\nfrom pkg.modes import mode\n\n\ndef extend():\n"
                "    sys.path.append(mode())\n"
            ),
            "tests/test_sets.py": "from tests.paths import extend\n\nextend()\n\n\n"
            "def test_sets():\n    pass\n",
        },
        {SETS, ENV},
    ),
]


@pytest.mark.parametrize(
    "files, picked",
    IMPORT_CASES,
    ids=["test module", "sub-conftest", "imported helper", "sys.path helper"],
)
def test_w12_import_time_write_of_process_state_reaches_every_test(repo, files, picked):
    """pytest imports every test module and conftest while collecting,
    before any test runs: what their import-time code writes into the
    process (an environment variable, ``sys.path``) is there for every
    test, including one that imports none of them."""
    base = repo.commit({**IMPORT_FILES, **files})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == picked | {QUIET_ID}
    assert NOOP.runner_id in unselected(plan)


def test_w12_import_time_code_writing_nothing_stays_scoped(repo):
    """Import-time code that only computes a module constant reaches the
    tests of its own module."""
    base = repo.commit(
        {
            **IMPORT_FILES,
            "tests/test_sets.py": (
                "from pkg.modes import mode\n\nMODE = mode()\n\n\ndef test_sets():\n"
                "    assert MODE\n"
            ),
        }
    )
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {SETS}


# Code run while collecting that imports or runs a module named at run time
# may run any code writing process-global state.
SESSION_FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/modes.py": "def mode():\n    return 'a'\n",
    "tests/__init__.py": "",
    "tests/envsetup.py": (
        "import os\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    os.environ['MODE'] = mode()\n\n\napply()\n"
    ),
    "tests/sub/__init__.py": "",
    "tests/sub/test_s.py": "def test_s():\n    pass\n",
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": (
        "import os\n\n\ndef test_env():\n    assert os.environ.get('MODE', 'a') in 'ab'\n"
    ),
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}


@pytest.mark.parametrize("call", ["importlib.import_module", "runpy.run_module"])
def test_q2_collection_hook_running_a_module_named_at_run_time(repo, call):
    """A sub-conftest's ``pytest_generate_tests`` imports or runs a module
    named by the environment (``tests.envsetup``, which sets ``MODE`` from
    ``pkg.modes``), and a test elsewhere reads ``MODE``. ``runpy``'s call
    did not count as an import of anything, in planning or in discovery's
    reading of what collection runs, so the reader was missed.
    (``import_module`` is the guard.)"""
    conftest = (
        "import importlib\nimport os\nimport runpy\n\n\n"
        f"def pytest_generate_tests(metafunc):\n    {call}(os.environ['SETUP'])\n"
    )
    base = repo.commit({**SESSION_FILES, "tests/sub/conftest.py": conftest})
    head = repo.commit({"pkg/modes.py": "def mode():\n    return 'b'\n"})
    bench = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")
    plan = repo.plan(base, head, [bench], discover_runners=["pytest"])
    assert selected(plan) == {
        "tests/other/test_env.py::test_env",
        "tests/sub/test_s.py::test_s",
    }


# A write to ``os.environ`` through any binding of the name (a star import, an
# import in ``try`` and another in ``except``, a function-local one) is a
# process write. Where recognising a name leaves something out (``with
# catch_warnings():`` puts the filters back), every binding must be it.
TEST_B = "def test_b():\n    pass\n"
WRITE = "environ['MODE'] = '{0}'\n\n\ndef test_a():\n    pass\n"


@pytest.mark.parametrize(
    "module",
    [
        "from os import *\n\n" + WRITE,
        "try:\n    from os import environ\nexcept ImportError:\n    from compat import environ\n\n"
        + WRITE,
        "def _set(v):\n    try:\n        from os import environ\n    except ImportError:\n"
        "        from compat import environ\n    environ['MODE'] = v\n\n\n_set('{0}')\n\n\n"
        "def test_a():\n    pass\n",
    ],
    ids=["star", "alt_import", "local_alt_import"],
)
def test_l2_a_process_write_through_any_binding_reaches_every_test(repo, module):
    files = {
        "tests/__init__.py": "",
        "tests/test_a.py": module.format("x"),
        "tests/test_b.py": TEST_B,
    }
    base = repo.commit({**ASV_BENCH, **files})
    head = repo.commit({"tests/test_a.py": module.format("y")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_l2_a_with_that_may_not_be_catch_warnings_restores_nothing(repo):
    module = (
        "import warnings\n\ntry:\n    from compat import nothing as catch_warnings\n"
        "except ImportError:\n    from warnings import catch_warnings\n\n"
        "with catch_warnings():\n    warnings.filterwarnings('{0}')\n\n\n"
        "def test_a():\n    pass\n"
    )
    files = {
        "tests/__init__.py": "",
        "tests/test_a.py": module.format("ignore"),
        "tests/test_b.py": TEST_B,
    }
    base = repo.commit({**ASV_BENCH, **files})
    head = repo.commit({"tests/test_a.py": module.format("error")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


# A store into process-global state through a patcher or a ``MonkeyPatch``
# that nothing undoes after the test is a process write. A sub-conftest's
# ``pytest_generate_tests`` runs ``tests/envsetup.py``'s ``apply`` before any
# test; a test in another directory reads the environment.
PATCHER_HOOK_FILES = {
    "pkg/__init__.py": "",
    "pkg/modes.py": MODES,
    "tests/__init__.py": "",
    "tests/sub/__init__.py": "",
    "tests/sub/test_s.py": "def test_s():\n    pass\n",
    "tests/sub/conftest.py": (
        "from tests import envsetup\n\n\ndef pytest_generate_tests(metafunc):\n"
        "    envsetup.apply()\n"
    ),
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": (
        "import os\n\n\ndef test_env():\n    assert os.environ.get('MODE', 'a') in 'ab'\n"
    ),
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
PATCHERS = {
    "patch.dict start": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch.dict(os.environ, {'MODE': mode()}).start()\n"
    ),
    "patch.dict string start": (
        "from unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch.dict('os.environ', {'MODE': mode()}).start()\n"
    ),
    "MonkeyPatch().setenv": (
        "import pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().setenv('MODE', mode())\n"
    ),
    "MonkeyPatch().setitem": (
        "import os\nimport pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().setitem(os.environ, 'MODE', mode())\n"
    ),
    "MonkeyPatch().syspath_prepend": (
        "import pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().syspath_prepend(mode())\n"
    ),
    "patch.object start": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch.object(os, 'environ', {'MODE': mode()}).start()\n"
    ),
    "MonkeyPatch().setattr of a module": (
        "import sys\nimport pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().setattr(sys, 'argv', [mode()])\n"
    ),
    "patch string start": (
        "from unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch('os.sep', mode()).start()\n"
    ),
    # One notion of undone (patches.py): a ``with`` at module level is
    # import-time code, no test's, and counts as not undone.
    "with patch.dict at module level": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n"
        "with mock.patch.dict(os.environ):\n    os.environ['MODE'] = mode()\n\n\n"
        "def apply():\n    pass\n"
    ),
    "with patch.dict around a yield": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def _patched():\n    with mock.patch.dict(os.environ):\n"
        "        os.environ['MODE'] = mode()\n        yield\n\n\n"
        "def apply():\n    for _ in _patched():\n        break\n"
    ),
}


@pytest.mark.parametrize("source", PATCHERS.values(), ids=PATCHERS.keys())
def test_q20_patcher_write_of_process_state_reaches_every_test(repo, source):
    """A collection hook stores into the environment (or the import path)
    through a patcher nothing undoes after the test: what it stores outlives
    the test, so every test depends on the hook's code, as for a direct
    ``os.environ[...] = ...``."""
    base = repo.commit({**PATCHER_HOOK_FILES, "tests/envsetup.py": source})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = repo.plan(base, head, [NOOP], discover_runners=["pytest"])
    assert selected(plan) == {ENV, SUB}


UNDONE_PATCHERS = {
    "with patch.dict": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    with mock.patch.dict(os.environ, {'MODE': mode()}):\n        pass\n"
    ),
    "with patch.dict around a store": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    with mock.patch.dict(os.environ):\n"
        "        os.environ['MODE'] = mode()\n"
    ),
    "MonkeyPatch.context": (
        "import pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    with pytest.MonkeyPatch.context() as mp:\n"
        "        mp.setenv('MODE', mode())\n"
    ),
}


@pytest.mark.parametrize("source", UNDONE_PATCHERS.values(), ids=UNDONE_PATCHERS.keys())
def test_q20_patcher_write_undone_stays_scoped(repo, source):
    """The same stores undone when the statement ends write nothing that
    outlives it: the test in another directory is not selected."""
    base = repo.commit({**PATCHER_HOOK_FILES, "tests/envsetup.py": source})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = repo.plan(base, head, [NOOP], discover_runners=["pytest"])
    assert selected(plan) == {SUB}


# A ``with`` that puts process state back when it ends.
PUT_BACK_ENV = (
    "import os\nimport warnings\nfrom unittest import mock\n\n\n"
    "def restored():\n    with mock.patch.dict(os.environ, {}):\n"
    "        os.environ['MODE'] = 'x'\n    with warnings.catch_warnings():\n"
    "        warnings.simplefilter('ignore')\n\n\n"
    "def left():\n    with mock.patch.dict(os.environ, {}):\n"
    "        os.environ['MODE'] = 'x'\n        yield\n\n\n"
    "def filters_left():\n    with warnings.catch_warnings():\n"
    "        warnings.simplefilter('ignore')\n        yield\n"
)


def test_w26_process_state_a_with_puts_back_after_a_yield_is_written(repo):
    """``with mock.patch.dict(os.environ)`` and ``with
    warnings.catch_warnings()`` put the state back when the statement ends;
    a body that yields leaves it changed while it is suspended, for
    whatever runs meanwhile."""
    base = repo.commit({"pkg/__init__.py": "", "pkg/env.py": PUT_BACK_ENV})
    index = build_index(read_snapshot(repo.path, base, source_roots=["."]))
    writers = {symbol for symbol, _ in index.process_writes}
    assert writers == {"pkg.env.left", "pkg.env.filters_left"}


# ---------------------------------------------------------------- patches undone when the test ends
# A patch undone after the test is seen only by code running during that test,
# which depends on what it stores: it is no write onto an external module for
# a lookup there, and puts nothing on an in-scope module for later code. A
# store that outlives the test (a patcher started and kept, a
# ``MonkeyPatch()`` of its own, a ``with patch`` around a ``yield``, any other
# receiver) is a write in both: the external lookup sees every change, and the
# module's lookups follow the writer (indexer/patches.py).

HEADER = (
    "import contextlib\nimport unittest\nfrom unittest import mock\n"
    "from unittest.mock import patch\n\nimport pytest\n\n{imp}\n\n\n"
)


# Each form stores ``{new}`` as ``{mod}.{attr}`` in a test of
# tests/test_patch.py, and calls the lookup there (as marshmallow's
# test does).
UNDONE = {
    "with": ("def test_patch():\n    with patch('{mod}.{attr}', {new}):\n        assert {call}\n"),
    "async-with": (
        "async def test_patch():\n    async with patch('{mod}.{attr}', {new}):\n"
        "        assert {call}\n"
    ),
    "with-object": (
        "def test_patch():\n    with mock.patch.object({mod}, '{attr}', {new}):\n"
        "        assert {call}\n"
    ),
    "with-returns": (
        "def test_patch():\n    with patch('{mod}.{attr}', {new}):\n        return {call}\n"
    ),
    "decorator": "@patch('{mod}.{attr}', {new})\ndef test_patch():\n    assert {call}\n",
    "decorator-object": (
        "@mock.patch.object({mod}, '{attr}', {new})\ndef test_patch():\n    assert {call}\n"
    ),
    "class-decorator": (
        "@patch('{mod}.{attr}', {new})\nclass TestPatch(unittest.TestCase):\n"
        "    def test_patch(self):\n        assert {call}\n"
    ),
    # A decorated generator runs unpatched: the patch ends with the call
    # that makes it.
    "generator-decorator": (
        "@pytest.fixture\n@patch('{mod}.{attr}', {new})\ndef patched():\n    yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "monkeypatch": (
        "def test_patch(monkeypatch):\n    monkeypatch.setattr({mod}, '{attr}', {new})\n"
        "    assert {call}\n"
    ),
    "monkeypatch-dotted": (
        "def test_patch(monkeypatch):\n    monkeypatch.setattr('{mod}.{attr}', {new})\n"
        "    assert {call}\n"
    ),
    "monkeypatch-fixture": (
        "@pytest.fixture\ndef patched(monkeypatch):\n"
        "    monkeypatch.setattr({mod}, '{attr}', {new})\n    yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "monkeypatch-method": (
        "class TestPatch:\n    def test_patch(self, monkeypatch):\n"
        "        monkeypatch.setattr({mod}, '{attr}', {new})\n        assert {call}\n"
    ),
    "mocker": (
        "def test_patch(mocker):\n    mocker.patch('{mod}.{attr}', {new})\n    assert {call}\n"
    ),
    "monkeypatch-context": (
        "def test_patch():\n    with pytest.MonkeyPatch.context() as mp:\n"
        "        mp.setattr({mod}, '{attr}', {new})\n        assert {call}\n"
    ),
}


KEPT = {
    "start": "def test_patch():\n    patch('{mod}.{attr}', {new}).start()\n    assert {call}\n",
    "start-stop": (
        "def test_patch():\n    p = patch('{mod}.{attr}', {new})\n    p.start()\n"
        "    assert {call}\n    p.stop()\n"
    ),
    "stored": (
        "PATCHER = patch('{mod}.{attr}', {new})\n\n\n"
        "def test_patch():\n    PATCHER.start()\n    assert {call}\n"
    ),
    "stored-attribute": (
        "class TestPatch(unittest.TestCase):\n    def setUp(self):\n"
        "        self.patcher = mock.patch.object({mod}, '{attr}', {new})\n"
        "        self.patcher.start()\n\n    def test_patch(self):\n        assert {call}\n"
    ),
    "monkeypatch-object": (
        "def test_patch():\n    mp = pytest.MonkeyPatch()\n"
        "    mp.setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    "monkeypatch-object-dotted": (
        "def test_patch():\n    mp = pytest.MonkeyPatch()\n"
        "    mp.setattr('{mod}.{attr}', {new})\n    assert {call}\n"
    ),
    "context-entered": (
        "def test_patch():\n    mp = pytest.MonkeyPatch.context().__enter__()\n"
        "    mp.setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    "yield-fixture": (
        "@pytest.fixture(scope='module')\ndef patched():\n"
        "    with patch('{mod}.{attr}', {new}):\n        yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "yield-context-fixture": (
        "@pytest.fixture(scope='module')\ndef patched():\n"
        "    with pytest.MonkeyPatch.context() as mp:\n"
        "        mp.setattr({mod}, '{attr}', {new})\n        yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "contextmanager": (
        "@contextlib.contextmanager\ndef patched():\n"
        "    with patch('{mod}.{attr}', {new}):\n        yield\n\n\n"
        "def test_patch():\n    with patched():\n        assert {call}\n"
    ),
    "receiver": (
        "def put(patcher):\n    patcher.setattr({mod}, '{attr}', {new})\n\n\n"
        "def test_patch():\n    put(pytest.MonkeyPatch())\n    assert {call}\n"
    ),
    "helper-parameter": (
        "def put(monkeypatch):\n    monkeypatch.setattr({mod}, '{attr}', {new})\n\n\n"
        "def test_patch():\n    put(pytest.MonkeyPatch())\n    assert {call}\n"
    ),
    "closure": (
        "LATER = []\n\n\ndef test_patch(monkeypatch):\n    def later():\n"
        "        monkeypatch.setattr({mod}, '{attr}', {new})\n\n"
        "    LATER.append(later)\n    assert {call}\n"
    ),
    "rebound": (
        "def test_patch(monkeypatch):\n    monkeypatch = pytest.MonkeyPatch()\n"
        "    monkeypatch.setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    "module-mocker": (
        "def test_patch(module_mocker):\n    module_mocker.patch('{mod}.{attr}', {new})\n"
        "    assert {call}\n"
    ),
    "module-level-with": (
        "with patch('{mod}.{attr}', {new}):\n    KEPT = {call}\n\n\n"
        "def test_patch():\n    assert KEPT\n"
    ),
    "builtin-setattr": (
        "def test_patch():\n    setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    # A store for good beside an undone one of the same name, by the same
    # code: still a store.
    "store-beside-patch": (
        "def test_patch(monkeypatch):\n    monkeypatch.setattr({mod}, '{attr}', {new})\n"
        "    {mod}.{attr} = {new}\n    assert {call}\n"
    ),
}


def _patch_module(form: str, *, imp: str, mod: str, attr: str, new: str, call: str) -> str:
    body = {**UNDONE, **KEPT}[form]
    fields = {"mod": mod, "attr": attr, "new": new, "call": call}
    return HEADER.format(imp=imp) + body.format(**fields)


# An external module (``datetime``).
def _external_plan(repo, form: str):
    """``pkg.fields.make`` looks a name nothing bounds up on ``datetime``;
    tests/test_patch.py stores ``NEW`` there by ``form``. An unrelated test
    changes."""
    files = {
        "pkg/__init__.py": "",
        "pkg/fields.py": (
            "import datetime as dt\n\n\ndef make(name):\n    return getattr(dt, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/test_fields.py": (
            "import os\n\nfrom pkg.fields import make\n\n\n"
            "def test_make():\n    assert make(os.environ.get('W26', 'date'))\n"
        ),
        "tests/test_patch.py": _patch_module(
            form,
            imp="import datetime\n\nfrom pkg.fields import make\n\n\ndef NEW():\n    return 1",
            mod="datetime",
            attr="datetime",
            new="NEW",
            call="make('datetime')",
        ),
        "tests/test_other.py": "def test_other():\n    assert True\n",
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": (
            "import os\n\nfrom pkg.fields import make\n\n\n"
            "def time_make():\n    make(os.environ.get('W26', 'date'))\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit({"tests/test_other.py": "def test_other():\n    assert 1\n"})
    targets = [asv_target("bench.time_make", "benchmarks.bench.time_make")]
    return repo.plan(base, head, targets, discover_runners=["pytest"])


def _outside_patch_module(plan) -> set[str]:
    return {s for s in selected(plan) if not s.startswith("tests/test_patch.py")}


@pytest.mark.parametrize("form", sorted(UNDONE))
def test_w26_a_patch_undone_after_the_test_writes_no_external_module(repo, form):
    """Only ``test_patch`` sees ``NEW`` on ``datetime``, and it depends on
    ``NEW``: the lookup in ``make`` is bounded by what ``datetime``
    defines, so a change to another test selects only that test."""
    plan = _external_plan(repo, form)
    assert _outside_patch_module(plan) == {"tests/test_other.py::test_other"}
    assert not selected(plan) & {"tests/test_patch.py::test_patch"}


# The forms whose store is made by code that runs only inside the test (the
# test, a helper only it calls, ``setUp``): under the isolation assumption
# what it leaves on ``datetime`` is no later test's.
IN_THE_TEST = {
    "builtin-setattr",
    "closure",
    "context-entered",
    "contextmanager",
    "helper-parameter",
    "module-mocker",
    "monkeypatch-object",
    "monkeypatch-object-dotted",
    "rebound",
    "receiver",
    "start",
    "start-stop",
    "store-beside-patch",
    "stored-attribute",
}


@pytest.mark.parametrize("form", sorted(KEPT))
def test_w26_a_store_that_outlives_the_test_writes_the_external_module(repo, form):
    """``NEW`` may stay on ``datetime`` for code that runs later: the
    lookup in ``make`` may find it, so every lookup sees any change. Made
    by code that runs only inside one test, it is that test's alone (the
    isolation assumption): the lookup stays bounded for the others."""
    plan = _external_plan(repo, form)
    if form in IN_THE_TEST:
        assert _outside_patch_module(plan) == {"tests/test_other.py::test_other"}
        return
    assert _outside_patch_module(plan) == {
        "tests/test_other.py::test_other",
        "tests/test_fields.py::test_make",
        "bench.time_make",
    }


# An in-scope module.
def _module_plan(repo, form: str):
    """``pkg.lib.get`` looks a name nothing bounds up on ``pkg.registry``;
    tests/test_patch.py stores ``tests.fake_registry.VALUE`` there by
    ``form``. ``VALUE`` changes."""
    files = {
        "pkg/__init__.py": "",
        "pkg/registry.py": "VALUE = 0\n",
        "pkg/lib.py": (
            "def get(name):\n    from pkg import registry\n\n    return getattr(registry, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/fake_registry.py": "VALUE = 1\n",
        "tests/test_b.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def test_b():\n    assert get(os.environ.get('W26', 'VALUE')) == 1\n"
        ),
        "tests/test_c.py": "def test_c():\n    pass\n",
        "tests/test_patch.py": _patch_module(
            form,
            imp=(
                "import pkg.registry\nfrom pkg.lib import get\n"
                "from tests.fake_registry import VALUE as NEW"
            ),
            mod="pkg.registry",
            attr="VALUE",
            new="NEW",
            call="get('VALUE')",
        ),
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def time_get():\n    get(os.environ.get('W26', 'VALUE'))\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit({"tests/fake_registry.py": "VALUE = 2\n"})
    targets = [asv_target("bench.time_get", "benchmarks.bench.time_get")]
    return repo.plan(base, head, targets, discover_runners=["pytest"])


@pytest.mark.parametrize("form", sorted(UNDONE))
def test_w26_a_patch_undone_after_the_test_puts_nothing_on_a_module(repo, form):
    """Only ``test_patch`` (which imports ``tests.fake_registry``) sees the
    new ``VALUE`` on ``pkg.registry``: the other lookups do not follow it."""
    plan = _module_plan(repo, form)
    assert _outside_patch_module(plan) == set()
    assert selected(plan)  # test_patch imports tests.fake_registry


@pytest.mark.parametrize("form", sorted(KEPT))
def test_w26_a_store_that_outlives_the_test_puts_objects_on_a_module(repo, form):
    """The stored ``VALUE`` may stay on ``pkg.registry`` for the tests that
    run later, whose lookups then find ``tests.fake_registry``'s object:
    they follow the writer's module and its imports."""
    plan = _module_plan(repo, form)
    assert {"tests/test_b.py::test_b", "bench.time_get"} <= selected(plan)
    assert "tests/test_c.py::test_c" not in selected(plan)


# ``sys.modules`` entries.
SWAPPED = {
    "monkeypatch": "def test_patch(monkeypatch):\n    {put}\n",
    "with-dict": (
        "def test_patch():\n    with patch.dict(sys.modules, {{'pkg.registry': fake}}):\n"
        "        pass\n"
    ),
}
INSTALLED = {
    "monkeypatch-object": "def test_patch():\n    mp = pytest.MonkeyPatch()\n    {put}\n",
    "dict-start": (
        "def test_patch():\n    patch.dict(sys.modules, {{'pkg.registry': fake}}).start()\n"
    ),
    "receiver": "def test_patch(mp):\n    {put}\n",
}


def _modules_plan(repo, body: str, receiver: str):
    put = f"{receiver}.setitem(sys.modules, 'pkg.registry', fake)"
    files = {
        "pkg/__init__.py": "",
        "pkg/registry.py": "VALUE = 0\n",
        "pkg/lib.py": (
            "def get(name):\n    from pkg import registry\n\n    return getattr(registry, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/fake_registry.py": "VALUE = 1\n",
        "tests/conftest.py": (
            "import pytest\n\n\n@pytest.fixture\ndef mp():\n    return pytest.MonkeyPatch()\n"
        ),
        "tests/test_b.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def test_b():\n    assert get(os.environ.get('W26', 'VALUE')) == 1\n"
        ),
        "tests/test_c.py": "def test_c():\n    pass\n",
        "tests/test_patch.py": (
            "import sys\nfrom unittest.mock import patch\n\nimport pytest\n\n"
            "import tests.fake_registry as fake\n\n\n" + body.format(put=put)
        ),
    }
    base = repo.commit(files)
    head = repo.commit({"tests/fake_registry.py": "VALUE = 2\n"})
    return repo.plan(base, head, [], discover_runners=["pytest"])


@pytest.mark.parametrize("form", sorted(SWAPPED))
def test_w26_a_module_swapped_for_one_test(repo, form):
    """``sys.modules['pkg.registry']`` replaced for ``test_patch`` alone:
    no later import gets ``tests.fake_registry``."""
    plan = _modules_plan(repo, SWAPPED[form], "monkeypatch")
    assert "tests/test_b.py::test_b" not in selected(plan)


@pytest.mark.parametrize("form", sorted(INSTALLED))
def test_w26_a_module_installed_beyond_the_test(repo, form):
    """Installed by a ``MonkeyPatch`` nothing undoes (or a ``patch.dict``
    started and kept): a later ``from pkg import registry`` may get
    ``tests.fake_registry``, so ``test_b`` follows it."""
    plan = _modules_plan(repo, INSTALLED[form], "mp")
    assert "tests/test_b.py::test_b" in selected(plan)
    assert "tests/test_c.py::test_c" not in selected(plan)


# ``mock.patch.multiple`` with a dotted string depends on the module and the
# names it patches, as every other patcher form does.
Q19_BASE = {
    "pkg/__init__.py": "",
    "pkg/config.py": "TIMEOUT = 5\nOTHER = 1\n",
    "pkg/api.py": "def fetch():\n    return 1\n",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
Q19_TEST = (
    "import contextlib\nfrom unittest import mock\n\nimport pytest\n\n"
    "from pkg.api import fetch\n\n\n"
    "def test_fetch():\n    with {call}:\n        assert fetch() == 1\n"
)
FETCH = py_target("tests/test_x.py::test_fetch", "tests.test_x.test_fetch")


@pytest.mark.parametrize(
    "call",
    [
        'mock.patch("pkg.config.TIMEOUT", 0)',
        'mock.patch.multiple("pkg.config", TIMEOUT=0)',
        'mock.patch.multiple("pkg.config", TIMEOUT=0, create=False)',
        'contextlib.nullcontext(pytest.MonkeyPatch().setattr("pkg.config.TIMEOUT", 0))',
    ],
    ids=["patch", "multiple", "multiple-options", "monkeypatch-value"],
)
def test_q19_patch_multiple_by_dotted_string_depends_on_its_names(repo, call):
    """``mock.patch.multiple("pkg.config", TIMEOUT=0)`` fails once
    ``TIMEOUT`` is gone, as ``mock.patch("pkg.config.TIMEOUT")`` does: the
    test depends on the names the call patches."""
    base = repo.commit({**Q19_BASE, "tests/test_x.py": Q19_TEST.format(call=call)})
    head = repo.commit({"pkg/config.py": "OTHER = 1\n"})
    plan = repo.plan(base, head, [FETCH, NOOP], source_roots=["."])
    assert selected(plan) == {FETCH.runner_id}


# ------------------------------------------------------------------------- the isolation assumption
# A store into module state made by code that runs only inside one test (the
# test, a function-scoped fixture, a helper of the test module only tests
# call) is seen by that test alone, so it is no dependency of the readers in
# other tests or benchmarks. Code that runs at collection (a test module's top
# level), a wider-scoped fixture, project code (a cache filled from a test), a
# doctest's or a manifest's entry on project code, and an entry that
# module-level code calls stay writers.

# A test or function-scoped fixture that saves a module attribute, stores into
# it and puts it back leaves nothing for later tests, as
# ``monkeypatch.setattr`` does. When the shape does not guarantee the restore
# (none, code that may raise before it, a wider scope), the store lasts.
STATE_FILES = {
    "pkg/__init__.py": "",
    "pkg/state.py": "STATE = None\n\n\ndef get():\n    return STATE\n",
    "pkg/impl.py": "def make():\n    return {0}\n",
    "tests/__init__.py": "",
    "tests/test_b.py": "from pkg.state import get\n\n\ndef test_b():\n    assert get() is None\n",
}
SAVE = "    orig = m.STATE\n    m.STATE = pkg.impl.make()\n"


def _restore_plan(repo, test_a: str):
    files = {**ASV_BENCH, **STATE_FILES, "tests/test_a.py": test_a}
    base = repo.commit({k: v.replace("{0}", "1") for k, v in files.items()})
    head = repo.commit({"pkg/impl.py": STATE_FILES["pkg/impl.py"].replace("{0}", "2")})
    return repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])


FIXTURE = "import pytest\n\nimport pkg.impl\n\n\n@pytest.fixture{scope}\ndef client():\n{body}"
TEST_A = "\n\ndef test_a(client):\n    pass\n"
LOCAL = "    import pkg.state as m\n\n"


@pytest.mark.parametrize(
    "test_a",
    [
        FIXTURE.format(
            scope="",
            body=LOCAL + SAVE + "    try:\n        yield 1\n    finally:\n        m.STATE = orig\n",
        )
        + TEST_A,
        FIXTURE.format(scope="", body=LOCAL + SAVE + "    yield\n    m.STATE = orig\n") + TEST_A,
        FIXTURE.format(
            scope="(autouse=True)",
            body=LOCAL + SAVE + "    try:\n        yield client_of()\n    finally:\n"
            "        m.STATE = orig\n        cleanup()\n",
        )
        + TEST_A,
        "import pkg.impl\nimport pkg.state as m\n\n\ndef test_a():\n"
        + SAVE
        + "    try:\n        assert m.STATE\n    finally:\n        m.STATE = orig\n",
    ],
    ids=["fixture_try_finally", "fixture_plain_yield", "fixture_finally_first", "test_try_finally"],
)
def test_l6_a_store_put_back_before_the_test_ends_lasts_for_no_later_test(repo, test_a):
    plan = _restore_plan(repo, test_a)
    assert selected(plan) == {"tests/test_a.py::test_a"}


@pytest.mark.parametrize(
    "test_a",
    [
        FIXTURE.format(scope="", body=LOCAL + SAVE + "    yield 1\n") + TEST_A,
        FIXTURE.format(
            scope="", body=LOCAL + SAVE + "    yield 1\n    cleanup()\n    m.STATE = orig\n"
        )
        + TEST_A,
        FIXTURE.format(scope="", body=LOCAL + SAVE + "    yield client_of()\n    m.STATE = orig\n")
        + TEST_A,
        FIXTURE.format(
            scope="(scope='module')",
            body=LOCAL + SAVE + "    try:\n        yield 1\n    finally:\n        m.STATE = orig\n",
        )
        + TEST_A,
        FIXTURE.format(
            scope="",
            body=LOCAL + SAVE + "    m.OTHER = pkg.impl.make()\n    try:\n        yield 1\n"
            "    finally:\n        m.STATE = orig\n",
        )
        + TEST_A,
        "import pkg.impl\nimport pkg.state as m\n\n\ndef helper():\n"
        + SAVE
        + "    try:\n        pass\n    finally:\n        m.STATE = orig\n\n\n"
        "def test_a():\n    helper()\n",
    ],
    ids=[
        "not_put_back",
        "code_before_the_restore",
        "yield_value_may_raise",
        "module_scope",
        "second_store_may_raise",
        "not_a_test",
    ],
)
def test_l6_a_store_not_surely_put_back_lasts(repo, test_a, request):
    """A store not surely put back lasts. Made by code that runs only inside
    the test (a function-scoped fixture, a helper only the test calls), it
    lasts for that test alone under the isolation assumption (L7); made by
    a module-scoped fixture, it lasts for every later test."""
    plan = _restore_plan(repo, test_a)
    if request.node.callspec.id == "module_scope":
        assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    else:
        assert selected(plan) == {"tests/test_a.py::test_a"}


# Stores into a server module's state from test code of each kind, and a
# project cache filled from a test.
ISO = {
    "pkg/__init__.py": "",
    "pkg/server.py": "_state = None\n\n\ndef state():\n    return _state\n",
    "pkg/impl.py": "def make():\n    return {0}\n",
    "pkg/cache.py": (
        "import pkg.impl\n\n_CACHE = {}\n\n\ndef fill():\n    _CACHE['k'] = pkg.impl.make()\n\n\n"
        "def get():\n    return _CACHE.get('k')\n"
    ),
    "tests/__init__.py": "",
    "tests/test_other.py": (
        "from pkg.server import state\n\n\ndef test_other():\n    assert state() is None\n"
    ),
    "tests/test_reads_cache.py": (
        "from pkg.cache import get\n\n\ndef test_reads_cache():\n    assert get() is None\n"
    ),
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.server import state\n\n\ndef time_state():\n    state()\n",
}
ISO_BENCH = asv_target("bench.time_state", "benchmarks.bench.time_state")
ISO_HEAD = "import pytest\n\nimport pkg.cache\nimport pkg.impl\nimport pkg.server\n\n\n"
OTHERS = {"tests/test_other.py::test_other", "bench.time_state"}


def _iso_plan(repo, test_a: str, evidence: bool = False):
    files = {**ISO, "tests/test_a.py": test_a}
    if evidence:
        files[".gitignore"] = "__pycache__/\n.diffcone/\n"
    base = repo.commit({**files, "pkg/impl.py": ISO["pkg/impl.py"].format("1")})
    recorded = repo.collect() if evidence else None
    head = repo.commit({"pkg/impl.py": ISO["pkg/impl.py"].format("2")})
    return repo.plan(base, head, [ISO_BENCH], discover_runners=["pytest"], evidence=recorded)


@pytest.mark.parametrize(
    "test_a",
    [
        ISO_HEAD + "def _put():\n    pkg.server._state = pkg.impl.make()\n\n\n"
        "def test_a():\n    _put()\n",
        ISO_HEAD + "def test_a():\n    pkg.server._state = pkg.impl.make()\n",
        ISO_HEAD + "@pytest.fixture\ndef put():\n    pkg.server._state = pkg.impl.make()\n\n\n"
        "def test_a(put):\n    pass\n",
        ISO_HEAD
        + "class TestA:\n    def _put(self):\n        pkg.server._state = pkg.impl.make()\n\n"
        "    def test_a(self):\n        self._put()\n",
    ],
    ids=["helper", "test", "function_fixture", "method_helper"],
)
def test_l7_a_store_by_isolated_test_code_reaches_only_its_test(repo, test_a):
    plan = _iso_plan(repo, test_a)
    selected_ids = selected(plan)
    assert {t for t in selected_ids if "test_a" in t} and not selected_ids & OTHERS


@pytest.mark.parametrize(
    "test_a",
    [
        ISO_HEAD + "pkg.server._state = pkg.impl.make()\n\n\ndef test_a():\n    pass\n",
        ISO_HEAD + "@pytest.fixture(scope='session')\ndef put():\n"
        "    pkg.server._state = pkg.impl.make()\n\n\ndef test_a(put):\n    pass\n",
        ISO_HEAD + "SCOPE = 'function'\n\n\n@pytest.fixture(scope=SCOPE)\ndef put():\n"
        "    pkg.server._state = pkg.impl.make()\n\n\ndef test_a(put):\n    pass\n",
        ISO_HEAD + "def _put():\n    pkg.server._state = pkg.impl.make()\n\n\n_put()\n\n\n"
        "def test_a():\n    _put()\n",
    ],
    ids=["module_level", "session_fixture", "computed_scope", "helper_also_at_module_level"],
)
def test_l7_a_store_by_code_that_outlives_the_test_reaches_every_reader(repo, test_a):
    plan = _iso_plan(repo, test_a)
    assert OTHERS <= selected(plan)


def test_l7_a_project_cache_filled_from_a_test_still_reaches_its_readers(repo):
    plan = _iso_plan(repo, ISO_HEAD + "def test_a():\n    pkg.cache.fill()\n")
    assert "tests/test_reads_cache.py::test_reads_cache" in selected(plan)


# Entries on project code, entries that module-level code calls, and a
# ``monkeypatch`` overridden with a wider scope.
BARE_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _discovered_plan(repo, base, head, targets=(), evidence=None, **kwargs):
    return repo.plan(
        base, head, list(targets), discover_runners=["pytest"], evidence=evidence, **kwargs
    )


ISOLATION = {
    "pkg/reg.py": "REG = {}\n\n\ndef current():\n    return REG.get('k')\n",
    "pkg/impl.py": "def make():\n    return 1\n",
    "tests/test_r.py": (
        "from pkg.reg import current\n\n\ndef test_r():\n    assert current() == 1\n"
    ),
}
IMPL2 = {"pkg/impl.py": "def make():\n    return 2\n"}
FILLER = (
    "import pkg.impl\nimport pkg.reg\n\n\n"
    "def fill():\n    '''Fill the registry.\n\n    >>> fill()\n    '''\n"
    "    pkg.reg.REG['k'] = pkg.impl.make()\n"
)


def test_r23_a_doctest_entry_is_no_isolated_test_code(repo):
    """With ``--doctest-modules`` the project function ``fill`` is a target's
    entry. It fills the registry when a test module imports and calls it at
    collection; test_r reads the registry."""
    base = repo.commit(
        {
            **BARE_BASE,
            **ISOLATION,
            "pkg/mod.py": FILLER,
            "tests/test_x.py": "import pkg.mod\n\npkg.mod.fill()\n\n\ndef test_x():\n    pass\n",
        }
    )
    head = repo.commit(IMPL2)
    options = DiscoveryOptions(runner_args=("--doctest-modules",))
    plan = _discovered_plan(repo, base, head, discovery_options=options)
    assert selected(plan) == {
        "pkg/mod.py::pkg.mod.fill",
        "tests/test_x.py::test_x",
        "tests/test_r.py::test_r",
    }
    assert rules(plan, "tests/test_r.py::test_r") == {"dependency"}


def test_r23_a_manifest_entry_on_project_code_is_no_isolated_test_code(repo):
    """An ASV manifest target whose entry is the project's ``fill``: the
    function stays a writer for the pytest reader."""
    base = repo.commit(
        {
            **BARE_BASE,
            **ISOLATION,
            "pkg/mod.py": FILLER,
            "tests/test_x.py": "import pkg.mod\n\npkg.mod.fill()\n\n\ndef test_x():\n    pass\n",
        }
    )
    head = repo.commit(IMPL2)
    bench = asv_target("pkg/mod.py::fill", "pkg.mod.fill")
    plan = _discovered_plan(repo, base, head, [bench])
    assert selected(plan) == {
        "pkg/mod.py::fill",
        "tests/test_x.py::test_x",
        "tests/test_r.py::test_r",
    }


def test_r26_a_test_called_at_module_level_is_no_isolated_writer(repo):
    """``test_a`` stores into the registry; another test module imports and
    calls it at collection, so its store outlives it."""
    base = repo.commit(
        {
            **BARE_BASE,
            **ISOLATION,
            "tests/test_a.py": (
                "import pkg.impl\n"
                "import pkg.reg\n"
                "\n"
                "\n"
                "def test_a():\n"
                "    pkg.reg.REG['k'] = pkg.impl.make()\n"
            ),
            "tests/test_b.py": (
                "from tests.test_a import test_a\n\ntest_a()\n\n\ndef test_b():\n    pass\n"
            ),
        }
    )
    head = repo.commit(IMPL2)
    plan = _discovered_plan(repo, base, head)
    assert "tests/test_r.py::test_r" in selected(plan)
    assert rules(plan, "tests/test_r.py::test_r") == {"dependency"}


def test_r26_setup_called_by_module_level_code_is_no_isolated_writer(repo):
    base = repo.commit(
        {
            **BARE_BASE,
            **ISOLATION,
            "tests/test_a.py": (
                "import unittest\n"
                "\n"
                "import pkg.impl\n"
                "import pkg.reg\n"
                "\n"
                "\n"
                "class TestA(unittest.TestCase):\n"
                "    def setUp(self):\n"
                "        pkg.reg.REG['k'] = pkg.impl.make()\n"
                "\n"
                "    def test_a(self):\n"
                "        pass\n"
            ),
            "tests/test_b.py": (
                "from tests.test_a import TestA\n"
                "\n"
                "TestA.setUp(TestA())\n"
                "\n"
                "\n"
                "def test_b():\n"
                "    pass\n"
            ),
        }
    )
    head = repo.commit(IMPL2)
    plan = _discovered_plan(repo, base, head)
    assert "tests/test_r.py::test_r" in selected(plan)


def test_r26_an_uncalled_setup_stays_isolated(repo):
    """Guard (precision): the class's own edge to ``setUp`` saying unittest
    may call it is the runner's per-test call, not a referrer; a ``setUp``
    nothing else calls stays isolated."""
    base = repo.commit(
        {
            **BARE_BASE,
            **ISOLATION,
            "tests/test_a.py": (
                "import unittest\n\nimport pkg.impl\nimport pkg.reg\n\n\n"
                "class TestA(unittest.TestCase):\n    def setUp(self):\n"
                "        pkg.reg.REG['k'] = pkg.impl.make()\n\n"
                "    def test_a(self):\n        pass\n"
            ),
        }
    )
    head = repo.commit(IMPL2)
    plan = _discovered_plan(repo, base, head)
    assert selected(plan) == {"tests/test_a.py::TestA::test_a"}


def test_r26_an_uncalled_test_stays_isolated(repo):
    """Guard (precision): a test nothing else refers to is isolated; what
    it stores is its own."""
    base = repo.commit(
        {
            **BARE_BASE,
            **ISOLATION,
            "tests/test_a.py": (
                "import pkg.impl\n"
                "import pkg.reg\n"
                "\n"
                "\n"
                "def test_a():\n"
                "    pkg.reg.REG['k'] = pkg.impl.make()\n"
            ),
        }
    )
    head = repo.commit(IMPL2)
    plan = _discovered_plan(repo, base, head)
    assert selected(plan) == {"tests/test_a.py::test_a"}


SERVER = {
    "pkg/server.py": "_state = None\n\n\ndef state():\n    return _state\n",
    "pkg/impl.py": "def make():\n    return 1\n",
    "tests/test_other.py": (
        "from pkg.server import state\n\n\ndef test_other():\n    assert state() == 1\n"
    ),
}


def test_r27_a_session_scoped_monkeypatch_override_stores_for_good(repo):
    """A conftest overrides ``monkeypatch`` with a session-scoped
    ``MonkeyPatch``; a session fixture stores through it; test_other reads
    the stored value later."""
    base = repo.commit(
        {
            **BARE_BASE,
            **SERVER,
            "tests/conftest.py": (
                "import pytest\n\nimport pkg.impl\nimport pkg.server\n\n\n"
                "@pytest.fixture(scope='session')\ndef monkeypatch():\n"
                "    mp = pytest.MonkeyPatch()\n    yield mp\n    mp.undo()\n\n\n"
                "@pytest.fixture(scope='session')\ndef init(monkeypatch):\n"
                "    monkeypatch.setattr(pkg.server, '_state', pkg.impl.make())\n"
            ),
            "tests/test_a.py": "def test_a(init):\n    pass\n",
        }
    )
    head = repo.commit({"pkg/impl.py": "def make():\n    return 2\n"})
    plan = _discovered_plan(repo, base, head)
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_other.py::test_other"}


def test_r27_pytests_monkeypatch_in_a_test_is_undone(repo):
    """Guard (precision): a test storing through pytest's own
    function-scoped ``monkeypatch`` leaves nothing for a later test."""
    base = repo.commit(
        {
            **BARE_BASE,
            **SERVER,
            "tests/test_a.py": (
                "import pkg.impl\n"
                "import pkg.server\n"
                "\n"
                "\n"
                "def test_a(monkeypatch):\n"
                "    monkeypatch.setattr(pkg.server, '_state', pkg.impl.make())\n"
            ),
        }
    )
    head = repo.commit({"pkg/impl.py": "def make():\n    return 2\n"})
    plan = _discovered_plan(repo, base, head)
    assert selected(plan) == {"tests/test_a.py::test_a"}


# ------------------------------------------------------------------------ builtins called
# A builtin called by its own name is no function of ours: ``map(f, xs)``
# is not a project method named ``map``. The higher-order ones call ``f``
# on the items (roadmap item 19: pandas' ``validate_header_arg`` "wrote"
# ``is_integer`` through ``Styler.map``).

STYLER = (
    "class Styler:\n    def __init__(self):\n        self.done = []\n\n"
    "    def map(self, func):\n        self.done.append(func)\n        return self\n"
)
INFERENCE = "def _is_integer(x):\n    return isinstance(x, int)\n\n\nis_integer = _is_integer\n"
VALIDATE = (
    "from pkg.inference import is_integer\n\n\ndef validate(header):\n"
    "    return all(map(is_integer, header))\n"
)
BUILTIN_FILES = {
    "pkg/__init__.py": "",
    "pkg/style.py": STYLER,
    "pkg/inference.py": INFERENCE,
    "pkg/io.py": VALIDATE,
    "tests/__init__.py": "",
    "tests/test_io.py": (
        "from pkg.io import validate\n\n\ndef test_validate():\n    assert validate([1, 2])\n"
    ),
    "tests/test_inference.py": (
        "from pkg.inference import is_integer\n\n\ndef test_is_integer():\n"
        "    assert is_integer(3)\n"
    ),
}


def _mutated_by(repo, variable):
    index = build_index(read_snapshot(repo.path, "HEAD", source_roots=["."]))
    return {e.target for e in index.edges if e.source == variable and e.detail == "mutated_by"}


def test_a_function_handed_to_the_builtin_map_is_not_written(repo):
    repo.commit(BUILTIN_FILES)
    assert _mutated_by(repo, "pkg.inference.is_integer") == set()


def test_a_change_to_a_map_caller_does_not_reach_the_mapped_functions_readers(repo):
    base = repo.commit(BUILTIN_FILES)
    head = repo.commit({"pkg/io.py": VALIDATE.replace("all(", "bool(all(").replace("))", ")))")})
    plan = repo.plan(base, head, [BENCH_INFERENCE], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_io.py::test_validate"}


BENCH_INFERENCE = asv_target(
    "benchmarks.TimeInference.time_is_integer", "pkg.inference._is_integer"
)


@pytest.mark.parametrize(
    "call", ["map(fill, REGS)", "filter(fill, REGS)", "sorted(REGS, key=fill)"]
)
def test_a_function_a_higher_order_builtin_calls_on_items_writes_them(repo, call):
    """``map(fill, REGS)`` calls ``fill`` on each item, and ``fill`` writes
    its parameter: what ``REGS`` holds changes, by ``setup``."""
    repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/reg.py": (
                "REGS = [{}]\n\n\ndef fill(d):\n    d['k'] = 1\n    return d\n\n\n"
                f"def setup():\n    return list({call})\n"
            ),
        }
    )
    assert "pkg.reg.setup" in _mutated_by(repo, "pkg.reg.REGS")

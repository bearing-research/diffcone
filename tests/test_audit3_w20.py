"""Regression scenarios for W20-W24 (internal/audit.md, "Known after round
3").

- W20: an attribute read off a value of unknown type (``v.real``) finds a
  module-level name only through the module object, so it is name-matched
  to the module-level symbols of the modules such a value may be (escaped,
  held through a handle, what those reach, what the test runner hands out),
  and to class members whatever the receiver.
- W21: a method called on an instance attribute (``self._client.get()``)
  is one of what every write of the attribute makes, when they all say.
- W22: an unpickler and a loader of a path nothing bounds are imports of
  anything, unless the code says what the data or path is.
- W23: in evidence mode, they are module getters that can obtain a test
  module.
- W24: a lookup on a module sees what other code puts there.
- W25: an object installed in ``sys.modules`` for good under a name nothing
  bounds may replace any module (static planning only).
"""

from __future__ import annotations

import sys

import pytest

from diffcone.testing import asv_target, py_target, rules, selected

# ---------------------------------------------------------------- W20: static

VALUES = "def real():\n    return 1\n\n\nclass Num:\n    def imag(self):\n        return 2\n"
VALUES_CHANGED = VALUES.replace("return 1", "return 10")


def _w20_plan(repo, use: str, extra: dict[str, str] | None = None, change=None):
    """``pkg.values.real`` changes; ``tests/test_use.py::test_get`` runs
    ``pkg.use.get`` (``use``), ``bench.time_real`` calls ``real`` itself."""
    files = {
        "pkg/__init__.py": "",
        "pkg/values.py": VALUES,
        "pkg/use.py": use,
        "tests/__init__.py": "",
        "tests/test_use.py": (
            "from pkg.use import get\n\n\ndef test_get():\n    assert get(complex(1, 2))\n"
        ),
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": (
            "from pkg.values import real\n\n\ndef time_real():\n    real()\n\n\n"
            "def time_nothing():\n    pass\n"
        ),
        **(extra or {}),
    }
    base = repo.commit(files)
    head = repo.commit(change or {"pkg/values.py": VALUES_CHANGED})
    targets = [
        py_target("t::test_get", "tests.test_use.test_get"),
        asv_target("bench.time_real", "benchmarks.bench.time_real"),
        asv_target("bench.time_nothing", "benchmarks.bench.time_nothing"),
    ]
    return repo.plan(base, head, targets)


DIRECT = {"bench.time_real"}
BOTH = {"t::test_get", "bench.time_real"}


def test_w20_attribute_on_a_value_is_no_module_level_name(repo):
    """``v.real`` on a value nothing types: no module hands ``pkg.values``
    on, so it cannot be ``pkg.values.real``."""
    plan = _w20_plan(repo, "def get(v):\n    return v.real\n")
    assert selected(plan) == DIRECT


def test_w20_a_class_member_still_matches(repo):
    """A method of that name is matched whatever the receiver."""
    plan = _w20_plan(
        repo,
        "def get(v):\n    return v.imag()\n",
        change={"pkg/values.py": VALUES.replace("return 2", "return 20")},
    )
    assert selected(plan) == {"t::test_get"}
    assert rules(plan, "t::test_get") == {"unresolved_name_match"}


def test_w20_a_bare_name_still_matches(repo):
    """A bare name nothing binds may be any symbol of that name (code
    ``exec`` or a star import put there)."""
    plan = _w20_plan(repo, "def get(v):\n    return real()\n")
    assert selected(plan) == BOTH


@pytest.mark.parametrize(
    "use",
    [
        # handed on (W9)
        "from pkg import values\n\n\ndef read(m):\n    return m.real()\n\n\n"
        "def get(v):\n    return read(values)\n",
        # a handle read in place, by a literal and by a name nothing bounds
        "import importlib\n\n\ndef get(v):\n"
        "    m = importlib.import_module('pkg.values')\n    return m.real()\n",
        "import importlib\n\n\ndef get(v, name='pkg.values'):\n"
        "    m = importlib.import_module(name)\n    return m.real()\n",
        "import sys\n\n\ndef get(v):\n    return sys.modules['pkg.values'].real()\n",
        "import sys\n\n\ndef get(v, name='pkg.values'):\n    return sys.modules[name].real()\n",
        "import pytest\n\n\ndef get(v):\n    return pytest.importorskip('pkg.values').real()\n",
        # held at module level, read by another function
        "import importlib\n\n_m = importlib.import_module('pkg.values')\n\n\n"
        "def get(v):\n    return _m.real()\n",
        # a graph walk
        "import gc\n\n\ndef get(v):\n    for o in gc.get_objects():\n"
        "        if getattr(o, '__name__', '') == 'pkg.values':\n            return o.real()\n",
        # an unpickled object
        "import pickle\n\n\ndef get(v):\n    return pickle.loads(v).real()\n",
        # a lazy package export (PEP 562)
        "import pkg\n\n\ndef get(v):\n    return pkg.real()\n",
    ],
    ids=[
        "handed-on",
        "import-module",
        "import-module-any",
        "sys-modules",
        "sys-modules-any",
        "importorskip",
        "held",
        "gc",
        "pickle",
        "lazy-export",
    ],
)
def test_w20_a_module_held_as_a_value_still_matches(repo, use):
    extra = {
        "pkg/__init__.py": (
            "import importlib\n\n\ndef __getattr__(name):\n"
            "    return getattr(importlib.import_module('pkg.values'), name)\n"
        )
    }
    plan = _w20_plan(repo, use, extra)
    assert "t::test_get" in selected(plan)
    assert DIRECT <= selected(plan)
    assert "bench.time_nothing" not in selected(plan)


def test_w20_what_a_handed_on_module_imports_still_matches(repo):
    """``api`` is handed on, and ``api.real`` is ``pkg.values.real`` (W16)."""
    plan = _w20_plan(
        repo,
        "from pkg import api\n\n\ndef read(m):\n    return m.real()\n\n\n"
        "def get(v):\n    return read(api)\n",
        {"pkg/api.py": "from pkg.values import real\n"},
    )
    assert selected(plan) == BOTH


def test_w20_a_module_found_at_run_time_handed_on_matches_everywhere(repo):
    """``gc.get_objects()`` handed to another module: any module may be the
    value ``pkg.use.get`` reads ``real`` off."""
    plan = _w20_plan(
        repo,
        "from pkg.walk import objects\n\n\ndef get(v):\n"
        "    for o in objects():\n        return o.real()\n",
        {"pkg/walk.py": "import gc\n\n\ndef objects():\n    return gc.get_objects()\n"},
    )
    assert selected(plan) == BOTH


def test_w20_a_test_module_the_runner_hands_out_still_matches(repo):
    """``request.module.helper()`` in a conftest fixture: pytest hands the
    test module out, so its module-level ``helper`` stays matched."""
    files = {
        "tests/__init__.py": "",
        "tests/conftest.py": (
            "import pytest\n\n\n@pytest.fixture\ndef value(request):\n"
            "    return request.module.helper()\n"
        ),
        "tests/test_mod.py": (
            "def helper():\n    return 1\n\n\ndef test_value(value):\n    assert value == 1\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit(
        {"tests/test_mod.py": files["tests/test_mod.py"].replace("return 1", "return 2")}
    )
    targets = [
        py_target("t::test_value", "tests.test_mod.test_value", "tests.conftest.value"),
        asv_target("bench.none", "tests.conftest"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_value"}


REG = "REG = []\n\n\ndef install(x):\n    REG.append(x)\n"
READ = "from pkg.reg import REG\n\n\ndef count():\n    return len(REG)\n"


def _w20_caller_plan(repo, other: str):
    """A changed test calls ``install`` by name on an object of unknown type
    (``obj.install(...)``); ``pkg.reg.install`` writes ``REG``, which
    ``test_read`` reads: it is affected only if the call may be that
    ``install`` (``_add_caller_effects``, ``_import_call_effects``)."""
    files = {
        "pkg/__init__.py": "",
        "pkg/reg.py": REG,
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


# ---------------------------------------------------------------- W21: static

CACHE = (
    "class Cache:\n    def __init__(self):\n        self.items = {}\n\n"
    "    def get(self, k):\n        self.items[k] = 1\n        return 1\n"
)
STATE = (
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
        "pkg/state.py": STATE,
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
    head = repo.commit({"pkg/state.py": STATE.replace("ART.info()", "ART.info() or 0")})
    targets = [
        py_target("t::test_peek", "tests.test_read.test_peek"),
        py_target("t::test_describe", "tests.test_state.test_describe"),
        asv_target("bench.peek", "pkg.read.peek"),
    ]
    return repo.plan(base, head, targets)


INFO = "\n    def info(self):\n        return self._client.get('x')\n"
CHANGED_ONLY = {"t::test_describe"}
WRITER = {"t::test_describe", "t::test_peek", "bench.peek"}


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
    assert selected(plan) == WRITER


# ---------------------------------------------------------------- W22: static

WORK = "def job():\n    return 1\n"
BOX = "\n\nclass Box:\n    pass\n"
# Another class's pickling hook: a round trip may run it.
OTHER = "class Other:\n    def __setstate__(self, state):\n        self.x = 1\n"


def _w22_plan(repo, use: str):
    """``pkg.work.job`` changes; ``test_use`` calls ``pkg.use.run``, which
    obtains a function at run time (``use``) and calls it."""
    files = {
        "pkg/__init__.py": "",
        "pkg/work.py": WORK,
        "pkg/use.py": use,
        "tests/__init__.py": "",
        "tests/test_use.py": (
            "from pkg.use import run\n\n\ndef test_use(tmp_path):\n    run(tmp_path)\n"
        ),
        "tests/test_other.py": "def test_other():\n    pass\n",
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/work.py": WORK.replace("1", "2")})
    targets = [
        py_target("t::test_use", "tests.test_use.test_use"),
        py_target("t::test_other", "tests.test_other.test_other"),
        asv_target("bench.run", "pkg.use.run"),
    ]
    return repo.plan(base, head, targets)


@pytest.mark.parametrize(
    "use",
    [
        "import pickle\n\n\ndef run(d):\n    return pickle.loads((d / 'f').read_bytes())()\n",
        "import pickle\n\n\ndef run(d):\n    with open(d / 'f', 'rb') as f:\n"
        "        return pickle.load(f)()\n",
        "import pickle\n\n\ndef run(d):\n    with open(d / 'f', 'rb') as f:\n"
        "        return pickle.Unpickler(f).load()()\n",
        "import importlib.util\n\n\ndef run(d):\n"
        "    spec = importlib.util.spec_from_file_location('m', d / 'm.py')\n"
        "    m = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(m)\n"
        "    return m.entry()\n",
        "import importlib.util\n\n\ndef run(d):\n"
        "    spec = importlib.util.spec_from_file_location('m', d / 'm.py')\n"
        "    m = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(m)\n"
        "    f = m.entry\n    return f()\n",
    ],
    ids=["loads", "load", "unpickler", "spec", "spec-held"],
)
def test_w22_a_function_obtained_at_run_time_is_any(repo, use):
    """What an unpickler returns may be any module's function by reference;
    a module loaded from a path nothing bounds may run (or hand back) any
    code: both are imports by a name nothing bounds."""
    plan = _w22_plan(repo, use)
    assert selected(plan) == {"t::test_use", "bench.run"}
    assert rules(plan, "t::test_use") == {"dynamic_reference"}


# ``pkg.work.job`` by reference (protocol 0: a GLOBAL opcode).
PICKLED_JOB = b"cpkg.work\njob\np0\n."


@pytest.mark.parametrize(
    ("use", "change", "reached"),
    [
        # a round trip loads what was dumped: only pickling hooks matter
        (
            "import pickle\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    return pickle.loads(pickle.dumps(Box()))\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            False,
        ),
        (
            "import pickle\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    data = pickle.dumps(Box())\n    return pickle.loads(data)\n",
            {"pkg/other.py": OTHER.replace("1", "2")},
            True,
        ),
        # through a new temporary file the function dumps into
        (
            "import pickle\nimport tempfile\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    with tempfile.TemporaryFile() as f:\n"
            "        pickle.dump(Box(), f)\n        f.seek(0)\n        return pickle.load(f)\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            False,
        ),
        # a file that may hold anything
        (
            "import pickle\n\nfrom pkg.work import Box\n\n\n"
            "def run(d):\n    with open(d / 'f', 'rb') as f:\n"
            "        pickle.dump(Box(), f)\n        return pickle.load(f)\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            True,
        ),
        # bytes written out name what they load
        (
            f"import pickle\n\nDATA = {PICKLED_JOB!r}\n\n\n"
            "def run(d):\n    return pickle.loads(DATA)()\n",
            {"pkg/work.py": WORK.replace("1", "2") + BOX},
            True,
        ),
        (
            f"import pickle\n\nDATA = {PICKLED_JOB!r}\n\n\n"
            "def run(d):\n    return pickle.loads(DATA)()\n",
            {"pkg/other.py": OTHER.replace("1", "2")},
            False,
        ),
    ],
    ids=[
        "round-trip-other",
        "round-trip-hook",
        "temporary-file",
        "any-file",
        "bytes-named",
        "bytes-other",
    ],
)
def test_w22_what_an_unpickler_loads_when_the_code_says(repo, use, change, reached):
    files = {
        "pkg/__init__.py": "",
        "pkg/work.py": WORK + BOX,
        "pkg/other.py": OTHER,
        "pkg/use.py": use,
        "tests/__init__.py": "",
        "tests/test_use.py": (
            "from pkg.use import run\n\n\ndef test_use(tmp_path):\n    run(tmp_path)\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit(change)
    targets = [
        py_target("t::test_use", "tests.test_use.test_use"),
        asv_target("bench.run", "pkg.use.run"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == ({"t::test_use", "bench.run"} if reached else set())


@pytest.mark.parametrize("protocol", [0, 2, 4, 5])
def test_w22_the_globals_a_pickle_names(protocol):
    import collections
    import pickle

    from diffcone.indexer.references import _pickled_globals

    data = pickle.dumps(collections.OrderedDict, protocol=protocol)
    assert _pickled_globals(data) == [("collections", "OrderedDict")]
    assert _pickled_globals(b"not a pickle") is None


def test_w22_a_loader_bounded_by_a_literal_prefix(repo):
    """``spec_from_file_location`` of a path under ``plugins/``: the
    modules there, not ``pkg.work``."""
    use = (
        "import importlib.util\n\n\ndef run(d, name='a'):\n"
        "    spec = importlib.util.spec_from_file_location(name, f'plugins/{name}.py')\n"
        "    m = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(m)\n"
        "    return m.entry()\n"
    )
    files = {"plugins/__init__.py": "", "plugins/a.py": "def entry():\n    return 1\n"}
    plan = _w22_plan(repo, use)
    assert selected(plan) == set()
    base = repo.commit(files)
    head = repo.commit({"plugins/a.py": "def entry():\n    return 2\n"})
    targets = [py_target("t::test_use", "tests.test_use.test_use")]
    assert selected(repo.plan(base, head, targets)) == {"t::test_use"}


# ---------------------------------------------------------------- W23: evidence

evidence = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")


@evidence
@pytest.mark.parametrize(
    "obtain",
    [
        "pickle.loads(data)",
        "importlib.util.module_from_spec(importlib.util.spec_from_file_location('m', data))",
    ],
    ids=["unpickler", "loader"],
)
def test_w23_an_unpickler_or_loader_may_obtain_a_test_module(repo, obtain):
    """``peek`` looks a name nothing bounds up on what an unpickler or a
    loader of a path nothing bounds handed it: that may be a test module
    (or one of its functions), so a change to a test module's name is seen
    there."""
    files = {
        "pkg/__init__.py": "",
        "pkg/lib.py": (
            "import importlib.util\nimport pickle\n\n\ndef peek(data, name):\n"
            f"    obj = {obtain}\n    return getattr(obj, name, None)\n"
        ),
        "tests/__init__.py": "",
        "tests/test_a.py": "VALUE = 1\n\n\ndef test_a():\n    assert VALUE\n",
        "tests/test_b.py": (
            "import pickle\n\nfrom pkg.lib import peek\n\n\ndef test_b(tmp_path):\n"
            "    path = tmp_path / 'm.py'\n    path.write_text('VALUE = 1\\n')\n"
            "    data = pickle.dumps(1) if 'pickle' in peek.__code__.co_names else str(path)\n"
            "    peek(data, 'VALUE')\n"
        ),
        "tests/test_c.py": "def test_c():\n    pass\n",
    }
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"tests/test_a.py": files["tests/test_a.py"].replace("1", "2")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert "tests/test_b.py::test_b" in selected(plan)
    assert "tests/test_c.py::test_c" not in selected(plan)


# ---------------------------------------------------------------- W20: evidence


@evidence
@pytest.mark.parametrize("handed", [False, True], ids=["unknown-value", "module-handed-on"])
def test_w20_evidence_a_variable_read_off_a_value(repo, handed):
    """``pkg.conf.LIMIT`` changes; ``get`` reads ``v.LIMIT`` off a value
    of unknown type. It reads ``pkg.conf.LIMIT`` only if ``pkg.conf`` can
    be that value: a test handing the module on."""
    use = "from pkg import conf\n\n\ndef make():\n    return conf\n" if handed else ""
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": "LIMIT = 1\n",
        "pkg/lib.py": f"{use}\n\ndef get(v):\n    return v.LIMIT\n",
        "tests/__init__.py": "",
        "tests/test_get.py": (
            "import types\n\nfrom pkg.lib import get\n\n\n"
            "def test_get():\n    assert get(types.SimpleNamespace(LIMIT=1)) == 1\n"
        ),
        "tests/test_conf.py": (
            "from pkg.conf import LIMIT\n\n\ndef test_conf():\n    assert LIMIT\n"
        ),
    }
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"pkg/conf.py": "LIMIT = 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    expected = {"tests/test_conf.py::test_conf"}
    if handed:
        expected.add("tests/test_get.py::test_get")
    assert selected(plan) == expected


@evidence
@pytest.mark.parametrize("holder", [False, True], ids=["no-holder", "request-module"])
def test_w20_evidence_a_test_module_name_read_off_a_value(repo, holder):
    """A test module's ``LIMIT`` changes; ``get`` reads ``v.LIMIT``. pytest
    may hand the test module out (``request.module``), so the reader is
    guarded: it counts in a test that also ran code obtaining a module."""
    fixture = (
        "import pytest\n\n\n@pytest.fixture\ndef mod(request):\n    return request.module\n"
        if holder
        else ""
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/lib.py": "def get(v):\n    return getattr(v, 'LIMIT', 0)\n",
        "tests/__init__.py": "",
        "tests/conftest.py": fixture,
        "tests/test_m.py": "LIMIT = 1\n\n\ndef test_m():\n    assert LIMIT\n",
        "tests/test_n.py": (
            "import types\n\nfrom pkg.lib import get\n\n\n"
            + (
                "def test_n(mod):\n    assert get(mod) is not None\n"
                if holder
                else "def test_n():\n    assert get(types.SimpleNamespace(LIMIT=1))\n"
            )
        ),
    }
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"tests/test_m.py": files["tests/test_m.py"].replace("1", "2")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    expected = {"tests/test_m.py::test_m"}
    if holder:
        expected.add("tests/test_n.py::test_n")
    assert selected(plan) == expected


# ---------------------------------------------------------------- W24: evidence

W24_PUT = {
    "installed": (
        "import sys\n\nimport tests.fake_registry\n\n"
        "sys.modules['pkg.registry'] = tests.fake_registry\n"
    ),
    "assigned": "import pkg\nimport tests.fake_registry\n\npkg.registry = tests.fake_registry\n",
    "setattr": (
        "import pkg.registry\nimport tests.fake_registry as fake\n\n"
        "for name in dir(fake):\n    setattr(pkg.registry, name, getattr(fake, name))\n"
    ),
}


@pytest.mark.parametrize("mode", ["static", "evidence"])
@pytest.mark.parametrize("put", sorted(W24_PUT))
def test_w24_a_lookup_on_a_module_sees_what_others_put_there(repo, put, mode):
    """``get`` looks a name nothing bounds up on ``pkg.registry``; a helper
    module another test imports puts another module's objects there (or
    that module in its place) for good, so a change to that module's
    ``VALUE`` is seen."""
    files = {
        "pkg/__init__.py": "",
        "pkg/registry.py": "",
        "pkg/lib.py": (
            "def get(name):\n    from pkg import registry\n\n    return getattr(registry, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/fake_registry.py": "VALUE = 1\n",
        "tests/helpers.py": W24_PUT[put],
        "tests/test_b.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def test_b():\n    assert get(os.environ.get('W24', 'VALUE')) == 1\n"
        ),
        "tests/test_a.py": "import tests.helpers  # noqa: F401\n\n\ndef test_a():\n    pass\n",
        "tests/test_c.py": "def test_c():\n    pass\n",
    }
    base = repo.commit(files)
    if mode == "evidence" and sys.version_info < (3, 12):
        pytest.skip("needs sys.monitoring")
    ev = repo.collect() if mode == "evidence" else None
    head = repo.commit({"tests/fake_registry.py": "VALUE = 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert "tests/test_b.py::test_b" in selected(plan)
    if put != "installed":  # installing in sys.modules at import is process state (W12)
        assert "tests/test_c.py::test_c" not in selected(plan)


# ---------------------------------------------------------------- W25

INSTALLER = (
    "import sys\nimport types\n\n\ndef install(name):\n"
    "    sys.modules[name] = types.SimpleNamespace(X=1)\n"
)
W25_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "X = 1\n",
    "pkg/inst.py": INSTALLER,
    "tests/__init__.py": "",
    "tests/test_core.py": "import pkg.core\n\n\ndef test_core():\n    assert pkg.core.X\n",
    "tests/test_inst.py": (
        "from pkg.inst import install\n\n\ndef test_inst():\n    install('w25_' + 'name')\n"
    ),
}
W25_CHANGE = {"pkg/inst.py": INSTALLER.replace("X=1", "X=2")}


@pytest.mark.parametrize(
    ("name", "reached"),
    [("name", True), ("'pkg.' + name", True), ("'other.' + name", False), ("'pkg.x'", False)],
    ids=["unbounded", "prefix", "other-prefix", "literal"],
)
def test_w25_an_install_under_a_name_nothing_bounds(repo, name, reached):
    """``install`` puts an object in ``sys.modules`` for good under a name
    nothing bounds: an import of ``pkg.core`` may get it, so importers of
    every in-scope module (the prefix allowing) depend on the installer."""
    files = {**W25_FILES, "pkg/inst.py": INSTALLER.replace("[name]", f"[{name}]")}
    base = repo.commit(files)
    head = repo.commit({"pkg/inst.py": files["pkg/inst.py"].replace("X=1", "X=2")})
    targets = [
        py_target("t::test_core", "tests.test_core.test_core", "tests.test_core"),
        py_target("t::test_inst", "tests.test_inst.test_inst", "tests.test_inst"),
        asv_target("bench.core", "pkg.core"),
    ]
    plan = repo.plan(base, head, targets)
    expected = {"t::test_inst"} | ({"t::test_core", "bench.core"} if reached else set())
    assert selected(plan) == expected


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_w25_evidence_mode_follows_no_install(repo):
    """Evidence mode records what an installed object ran: ``test_core``
    never ran ``install`` and is not selected."""
    base = repo.commit(W25_FILES)
    ev = repo.collect()
    head = repo.commit(W25_CHANGE)
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {"tests/test_inst.py::test_inst"}
    static = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_core.py::test_core" in selected(static)

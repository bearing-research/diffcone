"""Regression scenarios for the fifth audit round, batch D: the execution
evidence recorder and planning (R28, R22, R29, R15).

Each test names the finding it guards and failed before its fix. Evidence is
recorded for real in a fixture repository (``diffcone collect`` runs its
suite under the recorder); assertions check exact target sets and the rules
behind them. ASV targets come from a manifest and keep their static
decision, which proves the engine is runner-independent.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

import pytest

from diffcone.cli import main
from diffcone.evidence import FLAG_SUBPROCESS, FLAG_TEXT, load_store
from diffcone.testing import asv_target, rules, selected

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
}
A, B, C = "tests/test_a.py::test_a", "tests/test_b.py::test_b", "tests/test_c.py::test_c"


def _plan(repo, base, head, evidence, targets=()):
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], evidence=evidence)


# --------------------------------------------------------------------------- R28

REG = """\
REG = {}


def register(f):
    REG[f.__name__] = f()
    return f


@register
def one():
    return 1


def two():
    return 2
"""
TEST_REG = """\
from pkg.reg import REG, two


def test_one():
    assert REG["one"] == 1


def test_two():
    assert two() == 2
"""
BENCH_REG = """\
from pkg.reg import one, two


class TimeReg:
    def time_one(self):
        one()

    def time_two(self):
        two()
"""
BENCH_ONE = asv_target("bench.TimeReg.time_one", "benchmarks.bench.TimeReg.time_one")
BENCH_TWO = asv_target("bench.TimeReg.time_two", "benchmarks.bench.TimeReg.time_two")
ONE, TWO = "tests/test_reg.py::test_one", "tests/test_reg.py::test_two"
THREE = "tests/test_other.py::test_three"
TEST_OTHER = "from pkg.other import three\n\n\ndef test_three():\n    assert three() == 3\n"
PLUGIN = {"tests/plugin.py": "import pkg.reg  # noqa: F401\n"}


@pytest.mark.parametrize("how", ["addopts", "PYTEST_ADDOPTS", "command", "sitecustomize"], ids=str)
def test_r28_code_imported_before_pytest_loads_the_plugin_is_recorded(repo, monkeypatch, how):
    """``@register def one()`` runs when ``pkg.reg`` is imported, which a
    ``-p tests.plugin`` plugin (from ``addopts``, ``PYTEST_ADDOPTS`` or the
    command) or a project ``sitecustomize.py`` does before pytest loads
    ``-p diffcone_collect``. The recorder now starts from its own
    ``sitecustomize`` before any of them, so the import is recorded: a body
    change to ``one`` escalates ``pkg.reg`` (it ran at import), which
    reaches both tests of the module, while a change to ``two``, which no
    import runs, stays with the test that executed it."""
    files = {
        **BASE,
        "pkg/reg.py": REG,
        "tests/test_reg.py": TEST_REG,
        "benchmarks/bench.py": BENCH_REG,
    }
    command = None
    if how == "addopts":
        files = {**files, **PLUGIN, "pytest.ini": "[pytest]\naddopts = -p tests.plugin\n"}
    elif how == "PYTEST_ADDOPTS":
        files = {**files, **PLUGIN}
        monkeypatch.setenv("PYTEST_ADDOPTS", "-p tests.plugin")
    elif how == "command":
        files = {**files, **PLUGIN}
        command = f"{sys.executable} -m pytest -p tests.plugin"
    else:
        files = {**files, "sitecustomize.py": "import pkg.reg  # noqa: F401\n"}
    base = repo.commit(files)
    evidence = repo.collect(command=command)
    assert "pkg.reg.one" in evidence.import_phase
    assert evidence.import_by["pkg.reg.one"] == {"pkg.reg"}
    assert evidence.preloaded == frozenset()
    head = repo.commit({"pkg/reg.py": REG.replace("return 1", "return 2")})
    plan = _plan(repo, base, head, evidence, [BENCH_ONE, BENCH_TWO])
    assert selected(plan) == {ONE, TWO, BENCH_ONE.runner_id, BENCH_TWO.runner_id}
    assert "escalated" in rules(plan, ONE) and rules(plan, TWO) == {"escalated"}
    assert plan.evidence["preloaded_modules"] == []
    head = repo.commit({"pkg/reg.py": REG.replace("return 2", "return 3")})
    plan = _plan(repo, base, head, evidence, [BENCH_ONE, BENCH_TWO])
    assert selected(plan) == {TWO, BENCH_TWO.runner_id}
    assert rules(plan, TWO) == {"executed_changed"}


def test_r28_modules_imported_before_the_recorder_are_planned_from_the_code(
    repo, monkeypatch, capsys
):
    """Without ``site`` (``python -S``) the recorder's ``sitecustomize``
    cannot run, so ``-p tests.plugin`` from ``addopts`` imports ``pkg.reg``
    before the plugin starts: nothing records what ran then. The modules
    already imported are noted (``preloaded``), ``collect`` warns, and any
    change in one is planned from the code: a change to ``two``, which no
    import runs, still reaches both tests of the module's importers."""
    files = {
        **BASE,
        **PLUGIN,
        "pytest.ini": "[pytest]\naddopts = -p tests.plugin\n",
        "pkg/reg.py": REG,
        "pkg/other.py": "def three():\n    return 3\n",
        "tests/test_reg.py": TEST_REG,
        "tests/test_other.py": TEST_OTHER,
        "benchmarks/bench.py": BENCH_REG,
    }
    base = repo.commit(files)
    monkeypatch.setenv("PYTHONPATH", sysconfig.get_path("purelib"))
    command = f"{sys.executable} -S -m pytest"
    argv = ["collect", "--repo", str(repo.path), "--command", command, "--no-cache"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    assert "imported before the recorder started" in err
    assert "pkg.reg" in err and "tests.plugin" in err
    evidence = load_store(Path(out.strip()))
    assert evidence.preloaded == {"pkg", "pkg.reg", "tests", "tests.plugin"}
    assert "pkg.reg.one" not in evidence.import_phase
    head = repo.commit({"pkg/reg.py": REG.replace("return 2", "return 3")})
    plan = _plan(repo, base, head, evidence, [BENCH_ONE, BENCH_TWO])
    # ``tests.plugin``, loaded for the session, imports ``pkg.reg``: its
    # import-time state reaches every test, ``test_three`` included.
    assert selected(plan) == {ONE, TWO, THREE, BENCH_TWO.runner_id}
    assert all("escalated" in rules(plan, t) for t in (ONE, TWO, THREE))
    assert plan.evidence["preloaded_modules"] == ["pkg", "pkg.reg", "tests", "tests.plugin"]
    # ``pkg.other`` was imported under the recorder: its change is precise.
    head = repo.commit({"pkg/other.py": "def three():\n    return 4\n", "pkg/reg.py": REG})
    plan = _plan(repo, base, head, evidence, [BENCH_ONE, BENCH_TWO])
    assert selected(plan) == {THREE}


# --------------------------------------------------------------------------- R22

READERS = {
    "tests/test_a.py": "from pkg.thing import OBJ\n\n\ndef test_a():\n    assert OBJ.get(1) == 2\n",
    "tests/test_b.py": "from pkg.thing import OBJ\n\n\ndef test_b():\n    assert OBJ.get(1) == 2\n",
    "tests/test_c.py": "from pkg.thing import OBJ, other\n\n\ndef test_c():\n"
    "    assert OBJ.get(1) == 2\n    assert other() == 10\n",
}
OTHER = "\n\ndef other():\n    return 10\n"
CACHED_PROPERTY = {
    "pkg/thing.py": """\
import functools


def compute():
    return 1


class Thing:
    @functools.cached_property
    def value(self):
        return compute()

    def get(self, x):
        return self.value + x


OBJ = Thing()
"""
    + OTHER
}
CLOSURE_MEMO = {
    "pkg/thing.py": """\
def memo(f):
    cache = {}

    def wrapper(x):
        if x not in cache:
            cache[x] = f(x)
        return cache[x]

    return wrapper


def compute(x):
    return x + 1


@memo
def cached(x):
    return compute(x)


class Thing:
    def get(self, x):
        return cached(x)


OBJ = Thing()
"""
    + OTHER
}
CLASS_CACHE = {
    "pkg/thing.py": """\
def compute(x):
    return x + 1


class Thing:
    _cache = {}

    @classmethod
    def get(cls, x):
        if x not in cls._cache:
            cls._cache[x] = compute(x)
        return cls._cache[x]


OBJ = Thing
"""
    + OTHER
}
INSTANCE_CACHE = {
    "pkg/thing.py": """\
def compute(x):
    return x + 1


class Thing:
    def __init__(self):
        self._cache = {}

    def get(self, x):
        if x not in self._cache:
            self._cache[x] = compute(x)
        return self._cache[x]


OBJ = Thing()
"""
    + OTHER
}
FUNCTION_ATTRIBUTE = {
    "pkg/thing.py": """\
def compute(x):
    return x + 1


def get(x):
    if x not in get.cache:
        get.cache[x] = compute(x)
    return get.cache[x]


get.cache = {}


class Thing:
    def get(self, x):
        return get(x)


OBJ = Thing()
"""
    + OTHER
}
FIXTURE_HELD = {
    "pkg/thing.py": CACHED_PROPERTY["pkg/thing.py"].replace("\n\nOBJ = Thing()\n", "\n"),
    "tests/conftest.py": "import pytest\n\nfrom pkg.thing import Thing\n\n\n"
    "@pytest.fixture(scope='session')\ndef thing():\n    return Thing()\n",
    "tests/test_a.py": "def test_a(thing):\n    assert thing.get(1) == 2\n",
    "tests/test_b.py": "def test_b(thing):\n    assert thing.get(1) == 2\n",
    "tests/test_c.py": "from pkg.thing import other\n\n\ndef test_c(thing):\n"
    "    assert thing.get(1) == 2\n    assert other() == 10\n",
}
BENCH_THING = asv_target("bench.TimeThing.time_get", "benchmarks.bench.TimeThing.time_get")
BENCH_OTHER = asv_target("bench.TimeThing.time_other", "benchmarks.bench.TimeThing.time_other")
BENCH_THING_SRC = """\
from pkg.thing import compute, other


class TimeThing:
    def time_get(self):
        compute

    def time_other(self):
        other()
"""


@pytest.mark.parametrize(
    "files",
    [CACHED_PROPERTY, CLOSURE_MEMO, CLASS_CACHE, INSTANCE_CACHE, FUNCTION_ATTRIBUTE, FIXTURE_HELD],
    ids=[
        "cached_property on a module singleton",
        "closure memo decorator",
        "class attribute cache",
        "instance cache on a singleton",
        "function attribute cache",
        "cached_property on a session fixture's object",
    ],
)
def test_r22_a_cache_outside_a_module_variable_reaches_every_reader(repo, files):
    """The first test fills the cache by running ``compute``; the others
    read the stored value and never run it, so only the first test's record
    holds ``compute``. The code storing it (through ``self``, ``cls``, a
    closure or a function attribute) is a cache every test that executed it
    may read from; a ``cached_property`` serves its value to readers that
    run nothing of it, so its readers are followed. All three tests fail
    after the change and all three are selected."""
    base = repo.commit({**BASE, **READERS, **files, "benchmarks/bench.py": BENCH_THING_SRC})
    evidence = repo.collect()
    head = repo.commit(
        {
            "pkg/thing.py": files["pkg/thing.py"]
            .replace("x + 1", "x + 2")
            .replace("return 1\n", "return 2\n", 1)
        }
    )
    plan = _plan(repo, base, head, evidence, [BENCH_THING, BENCH_OTHER])
    # Statically, ``get.cache = {}`` at module level refers to ``get``, so
    # its callee's change is import-time state for every importer.
    benches = {BENCH_THING.runner_id} | (
        {BENCH_OTHER.runner_id} if files is FUNCTION_ATTRIBUTE else set()
    )
    assert selected(plan) == {A, B, C} | benches
    assert rules(plan, B) == {"executed_reader"}
    # Precision: a change to code the cache cannot hold is still read only
    # by the test that ran it.
    head = repo.commit({"pkg/thing.py": files["pkg/thing.py"].replace("return 10", "return 11")})
    plan = _plan(repo, base, head, evidence, [BENCH_THING, BENCH_OTHER])
    assert selected(plan) == {C, BENCH_OTHER.runner_id}


# --------------------------------------------------------------------------- R29

HERE = "import os\n\nHERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
SCANDIR = {
    "pkg/files.py": "import os\n\n\ndef total(directory):\n"
    "    return sum(e.stat().st_size for e in os.scandir(directory) if e.is_file())\n",
    "tests/test_a.py": HERE + "from pkg.files import total\n\n\ndef test_a():\n"
    "    assert total(os.path.join(HERE, 'data')) == 6\n",
}
READLINK = {
    "pkg/files.py": "import os\n\n\ndef target(p):\n    return os.readlink(p)\n",
    "tests/test_a.py": HERE + "from pkg.files import target\n\n\ndef test_a():\n"
    "    assert target(os.path.join(HERE, 'data', 'link')) == 'a.txt'\n",
}
SQLITE_URI = {
    "pkg/files.py": "import sqlite3\n\n\ndef value(path):\n"
    "    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)\n"
    "    try:\n        return c.execute('select v from t').fetchone()[0]\n"
    "    finally:\n        c.close()\n",
    "tests/test_a.py": HERE + "from pkg.files import value\n\n\ndef test_a():\n"
    "    assert value(os.path.join(HERE, 'data', 'x.db')) == 1\n",
}
DIR_FD = {
    "pkg/files.py": "import os\n\n\ndef size(directory, name):\n"
    "    fd = os.open(directory, os.O_RDONLY)\n    try:\n"
    "        return os.stat(name, dir_fd=fd).st_size\n    finally:\n        os.close(fd)\n",
    "tests/test_a.py": HERE + "from pkg.files import size\n\n\ndef test_a():\n"
    "    os.chdir(os.path.join(HERE, 'tests'))\n"
    "    assert size(os.path.join(HERE, 'data'), 'a.txt') == 6\n",
}
LINKED_DIR = {
    "pkg/files.py": "def read(p):\n    with open(p) as f:\n        return f.read()\n",
    "tests/test_a.py": HERE + "from pkg.files import read\n\n\ndef test_a():\n"
    "    assert read(os.path.join(HERE, 'data', 'alias', 'a.txt')) == 'hello\\n'\n",
}
LISTDIR_FD = {
    "pkg/files.py": "import os\n\n\ndef names(directory):\n"
    "    fd = os.open(directory, os.O_RDONLY)\n    try:\n"
    "        return sorted(os.listdir(fd))\n    finally:\n        os.close(fd)\n",
    "tests/test_a.py": HERE + "from pkg.files import names\n\n\ndef test_a():\n"
    "    assert names(os.path.join(HERE, 'data')) == ['a.txt']\n",
}
UNRELATED = {"tests/test_b.py": "def test_b():\n    assert True\n"}
BENCH_FILES = asv_target("bench.TimeFiles.time_it", "benchmarks.bench.TimeFiles.time_it")
BENCH_FILES_SRC = (
    "import pkg.files\n\n\nclass TimeFiles:\n    def time_it(self):\n        pkg.files\n"
)
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file descriptors and symlinks")


def _db(value: int) -> bytes:
    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.db")
        db = sqlite3.connect(path)
        db.execute("create table t(v)")
        db.execute("insert into t values (?)", (value,))
        db.commit()
        db.close()
        return Path(path).read_bytes()


@posix_only
@pytest.mark.parametrize(
    ("files", "data", "change"),
    [
        (SCANDIR, {"data/a.txt": "hello\n"}, {"data/a.txt": "hello world\n"}),
        (READLINK, {"data/a.txt": "a\n", "data/b.txt": "b\n"}, "relink"),
        (SQLITE_URI, {"data/x.db": _db(1)}, {"data/x.db": _db(2)}),
        (DIR_FD, {"data/a.txt": "hello\n"}, {"data/a.txt": "hello world\n"}),
        (LINKED_DIR, {"data/real/a.txt": "hello\n"}, {"data/real/a.txt": "bye\n"}),
        (LISTDIR_FD, {"data/a.txt": "a\n"}, {"data/b.txt": "b\n"}),
    ],
    ids=[
        "DirEntry.stat of a scanned directory",
        "readlink of a tracked link",
        "sqlite file: URI",
        "stat with dir_fd after chdir",
        "a file read through a linked directory",
        "listdir of a descriptor",
    ],
)
def test_r29_file_touches_the_recorder_could_not_see(repo, files, data, change):
    """Each form read a file in a way the audit hooks and the plain
    wrappers did not record (or recorded under another path), so the
    outcome changed and nothing was selected."""
    base_files = {**BASE, **files, **data, **UNRELATED, "benchmarks/bench.py": BENCH_FILES_SRC}
    if files is LINKED_DIR:
        (repo.path / "data").mkdir(parents=True, exist_ok=True)
        os.symlink("real", repo.path / "data" / "alias")
    if files is READLINK:
        (repo.path / "data").mkdir(parents=True, exist_ok=True)
        os.symlink("a.txt", repo.path / "data" / "link")
    base = repo.commit(base_files)
    evidence = repo.collect()
    assert evidence.tests[A].flags == 0  # a known descriptor flags nothing
    if change == "relink":
        os.unlink(repo.path / "data" / "link")
        os.symlink("b.txt", repo.path / "data" / "link")
        change = {}
    head = repo.commit(change)
    plan = _plan(repo, base, head, evidence, [BENCH_FILES])
    # The benchmark keeps its static decision: a data file changed.
    assert selected(plan) == {A, BENCH_FILES.runner_id}
    assert rules(plan, A) == {"touched_file"}


@posix_only
def test_r29_descriptor_based_cleanup_of_a_temporary_directory_flags_nothing(repo):
    """``shutil.rmtree`` lists and stats through descriptors (``os.scandir(fd)``,
    ``dir_fd=``): with the descriptors ``os.open`` returned known, a
    temporary directory's cleanup outside the checkout records nothing and
    flags nothing, so the test stays precise."""
    files = {
        **BASE,
        "pkg/ops.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        "tests/test_a.py": "import os\nimport tempfile\n\nfrom pkg.ops import add\n\n\n"
        "def test_a():\n    with tempfile.TemporaryDirectory() as d:\n"
        "        os.mkdir(os.path.join(d, 'sub'))\n"
        "        with open(os.path.join(d, 'sub', 'x'), 'w') as f:\n            f.write('x')\n"
        "        assert add(1, 2) == 3\n",
        "tests/test_b.py": "from pkg.ops import mul\n\n\ndef test_b():\n"
        "    assert mul(2, 3) == 6\n",
    }
    base = repo.commit(files)
    evidence = repo.collect()
    assert not evidence.tests[A].flags & FLAG_SUBPROCESS
    head = repo.commit({"pkg/ops.py": files["pkg/ops.py"].replace("a * b", "b * a")})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == {B}


@posix_only
def test_r29_a_child_cleaning_a_temporary_directory_flags_nothing(repo):
    """A Python process a test starts records itself (child.py) and resolves
    descriptors the same way: its ``shutil.rmtree`` of a temporary directory
    is no reason to flag the test that started it."""
    cleanup = (
        "import os\nimport shutil\nimport tempfile\n\n"
        "d = tempfile.mkdtemp()\n"
        "os.mkdir(os.path.join(d, 's'))\n"
        "open(os.path.join(d, 's', 'x'), 'w').close()\n"
        "shutil.rmtree(d)\n"
    )
    files = {
        **BASE,
        "pkg/ops.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        "tests/cleanup.py": cleanup,
        "tests/test_a.py": "import os\nimport subprocess\nimport sys\n\nfrom pkg.ops import add\n"
        "\n\ndef test_a():\n"
        "    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cleanup.py')\n"
        "    subprocess.run([sys.executable, script], check=True)\n"
        "    assert add(1, 2) == 3\n",
        "tests/test_b.py": "from pkg.ops import mul\n\n\ndef test_b():\n"
        "    assert mul(2, 3) == 6\n",
    }
    base = repo.commit(files)
    evidence = repo.collect()
    assert not evidence.tests[A].flags & FLAG_SUBPROCESS
    head = repo.commit({"pkg/ops.py": files["pkg/ops.py"].replace("a * b", "b * a")})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == {B}


# --------------------------------------------------------------------------- R15

OPS = (
    "def value():\n    return 1\n\n\ndef other():\n    return 10\n\n\n"
    "def flag():\n    return False\n"
)
T_OTHER = "from pkg import ops\n\n\ndef test_b():\n    assert ops.other() == 10\n"


@pytest.mark.parametrize(
    ("test_a", "text"),
    [
        (
            "import sys\n\nimport pytest\n\n\n@pytest.mark.skipif('sys.platform == \"nothing\"')\n"
            "def test_a():\n    assert True\n",
            False,
        ),
        (
            "import pytest\n\n\n@pytest.mark.skipif('not config.getoption(\"-k\")')\n"
            "def test_a():\n    assert True\n",
            False,
        ),
        (
            "import pytest\n\nfrom pkg import ops\n\n\n@pytest.mark.skipif('ops.flag()')\n"
            "def test_a():\n    assert True\n",
            True,
        ),
        (
            "import pytest\n\n\n@pytest.mark.skipif('__import__(\"pkg.ops\").ops.flag()')\n"
            "def test_a():\n    assert True\n",
            True,
        ),
        (
            "from unittest import mock\n\nfrom pkg import ops\n\n\ndef test_a():\n"
            "    with mock.patch.object(ops, 'other', autospec=True) as m:\n        ops.other()\n"
            "    m.assert_called_once_with()\n",
            False,
        ),
    ],
    ids=[
        "skipif string on the platform",
        "skipif string on pytest's config",
        "skipif string calling project code",
        "skipif string importing project code",
        "autospec mock",
    ],
)
def test_r15_text_pytest_and_mock_compile_is_text_code_only_when_it_can_read_the_project(
    repo, test_a, text
):
    """A string ``skipif`` condition is evaluated by ``_pytest.skipping`` in
    the test module's globals, and ``autospec=True`` makes ``unittest.mock``
    compile a signature-checking wrapper: neither reads the project unless
    the condition names something of it, so the test is text code (always
    selected) only then."""
    base = repo.commit(
        {**BASE, "pkg/ops.py": OPS, "tests/test_a.py": test_a, "tests/test_b.py": T_OTHER}
    )
    evidence = repo.collect()
    assert bool(evidence.tests[A].flags & FLAG_TEXT) is text
    head = repo.commit({"pkg/ops.py": OPS.replace("return 1\n", "return 2\n", 1)})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == ({A} if text else set())
    head = repo.commit({"pkg/ops.py": OPS.replace("return False", "return True")})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == ({A} if text else set())

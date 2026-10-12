"""Execution evidence, the recorder: what a recording sees a test do.

Most scenarios record the fixture repository's suite for real (``diffcone
collect``) and plan with the evidence, asserting the exact tests selected:
the processes a test starts, the files it reads, code that runs across test
boundaries or from text, and the environment the recording runs in. The
rest call the recorders themselves (``diffcone.collect``, ``diffcone.child``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from diffcone import child
from diffcone.evidence import FLAG_SUBPROCESS, FLAG_TEXT, EvidenceError
from diffcone.testing import asv_target, executed, rules, selected, touched

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
# Symlinks, ``sh`` and shell scripts as commands: POSIX only.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
# Children record themselves on POSIX only (roadmap item 15).
posix_children = pytest.mark.skipif(os.name == "nt", reason="children record on POSIX")
posix_descriptors = pytest.mark.skipif(
    os.name == "nt", reason="POSIX file descriptors and symlinks"
)

# Each scenario records at base, changes one thing, and checks that the
# test whose outcome changes is selected.


def _evidence_selects(repo, files, change, roots=None):
    base = repo.commit({".gitignore": "__pycache__/\n.diffcone/\n", **files})
    evidence = repo.collect(source_roots=roots)
    head = repo.commit(change)
    plan = repo.plan(
        base, head, [], source_roots=roots, discover_runners=["pytest"], evidence=evidence
    )
    return selected(plan)


def _plan(repo, base, head, evidence, targets=()):
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], evidence=evidence)


IGNORE = {".gitignore": "__pycache__/\n.diffcone/\ngen.txt\n"}
WORK = "def handle(x):\n    return x + 1\n"
HANDLED = {"pkg/work.py": WORK.replace("x + 1", "1 + x")}
OTHER = "def g():\n    return 1\n"
OTHER_CHANGED = {"pkg/other.py": OTHER.replace("1", "2")}
BASE = {
    **IGNORE,
    "pkg/__init__.py": "",
    "pkg/work.py": WORK,
    "pkg/other.py": OTHER,
    "pkg/tool.py": (
        'from pkg.work import handle\n\nif __name__ == "__main__":\n    print(handle(1))\n'
    ),
    "tests/__init__.py": "",
    "tests/test_other.py": "from pkg.other import g\n\n\ndef test_other():\n    assert g() == 1\n",
}
OTHER_ID = "tests/test_other.py::test_other"


def _plans(repo, files, *changes, manifest=(), command=None):
    """Plans from evidence recorded on ``files``, one per change."""
    base = repo.commit(files)
    evidence = repo.collect(command=command)
    plans = []
    for change in changes:
        repo.git("checkout", "-q", base)
        head = repo.commit(change)
        plans.append(
            repo.plan(base, head, list(manifest), discover_runners=["pytest"], evidence=evidence)
        )
    return plans


SKELETON = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
}

A, B = "tests/test_a.py::test_a", "tests/test_b.py::test_b"

# --------------------------------------------------------------------------- the recording session

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

PLUGIN = {"tests/plugin.py": "import pkg.reg  # noqa: F401\n"}


@needs_monitoring
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
        **SKELETON,
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


OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_OPS = (
    "from pkg.ops import add, mul\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)
FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n.pytest_cache/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TEST_OPS,
}
BOTH = {"tests/test_ops.py::test_add", "tests/test_ops.py::test_mul"}


@needs_monitoring
def test_e6_a_recording_runs_a_suite_whose_addopts_need_the_cache_plugin(repo):
    """``--lf --ff`` are options of pytest's cache plugin: a recording
    disabled it, and pytest refused the run as a usage error (coverage.py's
    ``--failed-first``). The plugin stays, on an empty cache: the recorded
    failure neither narrows the recording (``--lf`` would run test_mul
    alone) nor is the repository's cache written."""
    repo.commit({**FILES, "pytest.ini": "[pytest]\naddopts = --lf --ff\n"})
    cache = repo.path / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    recorded = json.dumps({"tests/test_ops.py::test_mul": True})
    (cache / "lastfailed").write_text(recorded, encoding="utf-8")
    ev = repo.collect()
    assert set(ev.tests) == BOTH
    assert (cache / "lastfailed").read_text(encoding="utf-8") == recorded
    assert sorted(p.name for p in cache.iterdir()) == ["lastfailed"]


@needs_monitoring
def test_e6_a_recording_whose_cache_could_not_be_moved_is_refused(repo):
    """Moving the cache uses pytest internals; should that stop taking
    effect (here a conftest puts the cache back), the recording is refused
    rather than left for ``--lf`` to narrow."""
    conftest = (
        "import pytest\nfrom _pytest.cacheprovider import Cache\n\n\n"
        "@pytest.hookimpl(trylast=True)\ndef pytest_configure(config):\n"
        "    config.cache = Cache(config.rootpath / '.elsewhere', config, _ispytest=True)\n"
    )
    repo.commit({**FILES, "tests/conftest.py": conftest})
    with pytest.raises(EvidenceError, match="could not point pytest's cache"):
        repo.collect()


@needs_monitoring
def test_e6_a_command_that_disables_the_cache_plugin_still_records(repo):
    repo.commit({**FILES, "pytest.ini": "[pytest]\naddopts = --strict-config\n"})
    ev = repo.collect(command=f"{sys.executable} -m pytest -p no:cacheprovider")
    assert set(ev.tests) == BOTH


@needs_monitoring
def test_e8_a_body_change_of_a_test_selects_that_test_alone(repo):
    """pytest works out each test's location as it logs its start, which
    stats the test's file inside the test's window: every test's record
    held its own file, and an edit to one test selected every test of the
    file."""
    base = repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"tests/test_ops.py": TEST_OPS.replace("add(1, 2) == 3", "3 == add(1, 2)")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {"tests/test_ops.py::test_add"}


@needs_monitoring
def test_e8_a_test_that_reads_its_own_file_still_counts(repo):
    """Quiet only for pytest's own lookup: a test that reads its file is
    selected by an edit to it."""
    reads = TEST_OPS + (
        "\n\ndef test_reads_itself():\n"
        "    with open(__file__, encoding='utf-8') as f:\n        assert 'def' in f.read()\n"
    )
    base = repo.commit({**FILES, "tests/test_ops.py": reads})
    ev = repo.collect()
    head = repo.commit({"tests/test_ops.py": reads.replace("add(1, 2) == 3", "3 == add(1, 2)")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {"tests/test_ops.py::test_add", "tests/test_ops.py::test_reads_itself"}


# --------------------------------------------------------------------------- recorder records


def test_a_recorder_window_is_merged_and_written_whole():
    from diffcone import collect

    window = collect._window()
    window.update(
        codes={1},
        paths={"a.txt"},
        dirs={"data"},
        fixtures={"k"},
        spawns={(1, 2, 3, ("python", "-c", "pass"), False)},
        flags=collect.FLAG_TEXT,
    )
    merged = collect._window()
    collect._merge(merged, window)
    assert merged == window
    data = collect._serialised(window)
    assert set(data) == set(window)
    assert data["spawns"] == [[1, 2, 3, ["python", "-c", "pass"], False]]


def test_the_recorders_share_their_constants():
    from diffcone import child, collect

    for name in (
        "TOOL",
        "SUBPROCESS_EVENTS",
        "C_OPEN_EVENTS",
        "LISTING_EVENTS",
        "IGNORED_DIRS",
        "INSTALLED",
        "CYTHON_SUFFIXES",
        "DATA_READERS",
        "METADATA_MODULES",
    ):
        assert getattr(collect, name) is getattr(child, name), name
    assert collect._INTERPRETER is child.INTERPRETER


def test_e13_recorded_paths_use_forward_slashes(monkeypatch):
    from diffcone import collect

    monkeypatch.setattr(collect.os, "sep", "\\")
    # As the recorder stores them: case-folded where the platform folds case.
    monkeypatch.setattr(collect, "ROOTS", (collect.os.path.normcase("C:\\repo\\"),))
    assert collect._relative("C:\\repo\\lib\\m.py") == "lib/m.py"


# A record whose write failed.


@needs_monitoring
def test_rec9_a_failed_write_leaves_a_record_the_fold_cannot_use(tmp_path, monkeypatch):
    directory = tmp_path / child.CHILDREN / "10"
    directory.mkdir(parents=True)
    path = directory / "11-5.jsonl"
    header = {"pid": 11, "ppid": 10, "argv": ["python", "-m", "pkg.tool"], "start": 5}
    path.write_text(json.dumps(header) + "\n")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    monkeypatch.setattr(child, "_fd", fd)
    monkeypatch.setattr(child, "_path", str(path))

    def full(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(child.os, "write", full)
    child._write(["c", "pkg/tool.py", 1, "<module>"])
    monkeypatch.undo()
    assert not path.exists()  # renamed out of what the fold reads
    tree = child.resolve(tmp_path, 10, 11, 5, ["python", "-m", "pkg.tool"])
    assert tree.records == [] and tree.problems


@needs_monitoring
def test_rec9_a_short_write_counts_as_failed(tmp_path, monkeypatch):
    directory = tmp_path / child.CHILDREN / "10"
    directory.mkdir(parents=True)
    path = directory / "11-5.jsonl"
    path.write_text(json.dumps({"pid": 11, "ppid": 10, "argv": ["python"], "start": 5}) + "\n")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    monkeypatch.setattr(child, "_fd", fd)
    monkeypatch.setattr(child, "_path", str(path))
    monkeypatch.setattr(child.os, "write", lambda fd, data: len(data) - 1)
    child._write(["c", "pkg/tool.py", 1, "<module>"])
    monkeypatch.undo()
    assert child.resolve(tmp_path, 10, 11, 5, ["python"]).problems


@needs_monitoring
def test_rec9_an_emptied_record_under_a_launcher_is_a_problem(tmp_path):
    """A failed write that could not rename the record empties it: one of a
    launcher's Python children, which nothing else vouches for."""
    uv = ["/bin/uv", "run", "python", "harness.py"]
    directory = tmp_path / child.CHILDREN / "20"
    directory.mkdir(parents=True)
    header = {"pid": 21, "ppid": 20, "argv": ["python", "harness.py"], "start": 5}
    (directory / "21-5.jsonl").write_text(json.dumps(header) + "\n")
    assert child.resolve(tmp_path, 10, 20, 5, uv).problems == []
    (directory / "22-6.jsonl").write_text("")
    assert child.resolve(tmp_path, 10, 20, 5, uv).problems


# --------------------------------------------------------------------------- across tests

COUNTER = (
    "def _counter():\n    n = 0\n    while True:\n        n += {step}\n        yield n\n\n\n"
    "_ids = _counter()\n\n\ndef next_id():\n    return next(_ids)\n"
)


@needs_monitoring
def test_e5_a_generator_resumed_in_a_later_test_is_credited_to_it(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/gen.py": COUNTER.format(step=1),
            "tests/__init__.py": "",
            "tests/test_a.py": "from lib.gen import next_id\n\n\ndef test_a():\n    next_id()\n",
            "tests/test_b.py": (
                "from lib.gen import next_id\n\n\ndef test_b():\n"
                "    assert next_id() + 1 == next_id()\n"
            ),
        },
        {"lib/gen.py": COUNTER.format(step=2)},
    )
    assert "tests/test_b.py::test_b" in chosen


@needs_monitoring
def test_e16_a_generator_reentered_by_throw_is_credited(repo):
    worker = (
        "STATE = []\n\n\ndef _worker():\n    while True:\n        try:\n            yield\n"
        "        except ValueError:\n            STATE.append({})\n\n\nW = _worker()\n"
    )
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/w.py": worker.format(1),
            "tests/__init__.py": "",
            "tests/test_a.py": "from lib.w import W\n\n\ndef test_start():\n    next(W)\n",
            "tests/test_b.py": (
                "from lib.w import STATE, W\n\n\n"
                "def test_throw():\n    W.throw(ValueError)\n    assert STATE == [1]\n"
            ),
        },
        {"lib/w.py": worker.format(2)},
    )
    assert "tests/test_b.py::test_throw" in chosen


@needs_monitoring
def test_e15_a_test_beside_a_looping_thread_is_credited_with_the_loop(repo):
    """test_a starts a thread that stays inside one frame; test_b runs while
    it loops and reads what the loop writes. The loop raises no event in
    test_b's window, so only the frames credited at its start name it."""
    loop = (
        "import time\n\nSTATE = [0]\nSTOP = []\n\n\n"
        "def loop():\n    while not STOP:\n        STATE[0] = {}\n        time.sleep(0.005)\n"
    )
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/bg.py": loop.format(1),
            "lib/other.py": "def g():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_a.py": (
                "import threading\nimport time\n\nfrom lib.bg import loop\n\n\n"
                "def test_a():\n    threading.Thread(target=loop, daemon=True).start()\n"
                "    time.sleep(0.05)\n"
            ),
            "tests/test_b.py": (
                "import time\n\nfrom lib.bg import STATE, STOP\n\n\n"
                "def test_b():\n    time.sleep(0.05)\n    assert STATE[0] == 1\n"
                "    STOP.append(1)\n    time.sleep(0.05)  # the loop ends before test_c\n"
            ),
            "tests/test_c.py": "from lib.other import g\n\n\ndef test_c():\n    g()\n",
        },
        {"lib/bg.py": loop.format(2)},
    )
    assert {"tests/test_a.py::test_a", "tests/test_b.py::test_b"} <= chosen
    assert "tests/test_c.py::test_c" not in chosen


@needs_monitoring
def test_a_thread_left_running_does_not_make_its_test_always_selected(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/other.py": "def g():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_t.py": (
                "import threading\nimport time\n\n\ndef test_t():\n"
                "    threading.Thread(target=time.sleep, args=(5,), daemon=True).start()\n"
            ),
            "tests/test_u.py": "from lib.other import g\n\n\ndef test_u():\n    g()\n",
        },
        {"lib/other.py": "def g():\n    return 2\n"},
    )
    assert chosen == {"tests/test_u.py::test_u"}


# --------------------------------------------------------------------------- files a test reads


@needs_monitoring
def test_e7_a_file_read_through_pkgutil_selects_its_readers(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/data.json": '{"v": 1}\n',
            "lib/cfg.py": (
                "import json\nimport pkgutil\n\n\ndef load():\n"
                "    return json.loads(pkgutil.get_data('lib', 'data.json'))['v']\n"
            ),
            "tests/__init__.py": "",
            "tests/test_cfg.py": "from lib.cfg import load\n\n\ndef test_load():\n    load()\n",
        },
        {"lib/data.json": '{"v": 2}\n'},
    )
    assert "tests/test_cfg.py::test_load" in chosen


@needs_monitoring
def test_e18_a_source_file_read_as_data_selects_its_reader(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/version.py": "VERSION = '1'\n",
            "tests/__init__.py": "",
            "tests/test_v.py": (
                "from pathlib import Path\n\nimport lib\n\n\ndef test_v():\n"
                "    text = (Path(lib.__file__).parent / 'version.py').read_text()\n"
                "    assert \"'1'\" in text\n"
            ),
            "tests/test_u.py": "def test_u():\n    pass\n",
        },
        {"lib/version.py": "VERSION = '2'\n"},
    )
    assert chosen == {"tests/test_v.py::test_v"}


@needs_monitoring
@pytest.mark.parametrize(
    "body, change",
    [
        (
            "import sqlite3\n\n\ndef test_t():\n    sqlite3.connect('data/app.db').close()\n",
            {"data/app.db": "changed\n"},
        ),
        (
            "import os\n\n\ndef test_t():\n    assert not os.access('data/flag', os.R_OK)\n",
            {"data/flag": "on\n"},
        ),
    ],
)
def test_e20_files_opened_by_c_code_are_touches(repo, body, change):
    chosen = _evidence_selects(
        repo,
        {
            "data/app.db": "",
            "tests/__init__.py": "",
            "tests/test_t.py": body,
            "tests/test_u.py": "def test_u():\n    pass\n",
        },
        change,
    )
    assert chosen == {"tests/test_t.py::test_t"}


@needs_monitoring
def test_a_module_that_stats_its_own_file_is_not_escalated_on_every_edit(repo):
    """``HERE = Path(__file__).resolve()`` stats the module's own file while
    it is imported. Recorded as a read, every edit of the module escalated
    it and selected all its importers (5 747 of strata's tests for one edit
    of ``server.py``)."""
    module = (
        "from pathlib import Path\n\nHERE = Path(__file__).resolve().parent\n\n\n"
        "def f():\n    return {}\n\n\ndef g():\n    return 1\n"
    )
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/m.py": module.format(1),
            "tests/__init__.py": "",
            "tests/test_f.py": "from lib.m import f\n\n\ndef test_f():\n    assert f()\n",
            "tests/test_g.py": "from lib.m import g\n\n\ndef test_g():\n    assert g()\n",
        },
        {"lib/m.py": module.format(2)},
    )
    assert chosen == {"tests/test_f.py::test_f"}


@needs_monitoring
def test_a_source_file_a_test_checks_for_is_seen_when_it_appears(repo):
    """The stat is still a name seen: adding the file selects the test."""
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "tests/__init__.py": "",
            "tests/test_x.py": (
                "import os\n\n\ndef test_x():\n    assert not os.path.exists('lib/plugin.py')\n"
            ),
            "tests/test_y.py": "def test_y():\n    pass\n",
        },
        {"lib/plugin.py": "X = 1\n"},
    )
    assert chosen == {"tests/test_x.py::test_x"}


@needs_monitoring
def test_a_second_getsource_of_a_file_is_still_a_read(repo):
    """``inspect.getsource`` only stats a file linecache already holds: the
    second test reading it recorded no read, so an edit missed it."""
    reader = (
        "import inspect\n\nfrom lib.core import f\n\n\n"
        "def test_{0}():\n    assert 'return 1' in inspect.getsource(f)\n"
    )
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/core.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_a.py": reader.format("a"),
            "tests/test_b.py": reader.format("b"),
        },
        {"lib/core.py": "def f():\n    return 22\n"},
    )
    assert {"tests/test_a.py::test_a", "tests/test_b.py::test_b"} <= chosen


@needs_monitoring
def test_a_test_checking_a_source_files_size_is_selected_when_it_changes(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/core.py": "X = 1\n",
            "tests/__init__.py": "",
            "tests/test_size.py": (
                "import os\n\n\ndef test_size():\n    assert os.path.getsize('lib/core.py') == 6\n"
            ),
            "tests/test_other.py": "def test_other():\n    pass\n",
        },
        {"lib/core.py": "X = 22\n"},
    )
    assert chosen == {"tests/test_size.py::test_size"}


# A library's top level that an import ran is not the import system.

LIB = (
    'import os\n\nCONFIG = None\nif os.path.exists("plotrc"):\n'
    '    with open("plotrc") as f:\n        CONFIG = f.read()\n'
)


@pytest.fixture
def fakeplot(tmp_path, monkeypatch):
    site = tmp_path / "site"
    (site / "fakeplot").mkdir(parents=True)
    (site / "fakeplot" / "__init__.py").write_text(LIB)
    monkeypatch.setenv("PYTHONPATH", str(site))


@needs_monitoring
def test_rec3_a_library_reading_a_file_as_it_is_imported_in_a_test(repo, fakeplot):
    test = "def test_style():\n    import fakeplot\n\n    assert fakeplot.CONFIG == 'a'\n"
    files = {**BASE, "plotrc": "a", "tests/test_style.py": test}
    (changed,) = _plans(repo, files, {"plotrc": "b"})
    assert selected(changed) == {"tests/test_style.py::test_style"}
    assert rules(changed, "tests/test_style.py::test_style") == {"touched_file"}


@needs_monitoring
def test_rec3_a_library_reading_a_file_as_a_conftest_imports_it(repo, fakeplot):
    files = {
        **BASE,
        "plotrc": "a",
        "tests/conftest.py": "import fakeplot\n\nSTYLE = fakeplot.CONFIG\n",
        "tests/test_style.py": "from tests.conftest import STYLE\n\n\n"
        "def test_style():\n    assert STYLE == 'a'\n",
    }
    (changed,) = _plans(repo, files, {"plotrc": "b"})
    # The conftest's import read it: escalated to the conftest's scope.
    assert selected(changed) == {"tests/test_style.py::test_style", OTHER_ID}
    assert changed.fallbacks == []


# A file opened in another case on a file system that folds case.

CASE_TEST = """\
import os

HERE = os.path.dirname(__file__)


def test_reads():
    with open(os.path.join(HERE, "Data", "Expected.TXT")) as f:
        assert f.read() == "2"


def test_probes():
    assert not os.path.exists(os.path.join(HERE, "Data", "NEW.TXT"))
"""


@pytest.fixture
def folds_case(tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("the file system is case-sensitive")


@needs_monitoring
def test_rec6_a_file_opened_in_another_case_is_the_indexed_file(repo, folds_case):
    files = {**BASE, "tests/data/expected.txt": "2", "tests/test_case.py": CASE_TEST}
    edited, added = _plans(
        repo, files, {"tests/data/expected.txt": "3"}, {"tests/data/new.txt": "x"}
    )
    assert selected(edited) == {"tests/test_case.py::test_reads"}
    assert rules(edited, "tests/test_case.py::test_reads") == {"touched_file"}
    # Probed for in another case while absent.
    assert selected(added) == {"tests/test_case.py::test_probes"}


@needs_monitoring
def test_rec6_the_recording_says_its_file_system_folds_case(repo, folds_case):
    base = repo.commit({**BASE, "tests/data/expected.txt": "2", "tests/test_case.py": CASE_TEST})
    evidence = repo.collect()
    assert evidence.case_insensitive
    assert "tests/data/expected.txt" in touched(evidence, "tests/test_case.py::test_reads")
    assert base


# --------------------------------------------------------------------------- descriptors and links

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


@needs_monitoring
@posix_descriptors
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
    base_files = {**SKELETON, **files, **data, **UNRELATED, "benchmarks/bench.py": BENCH_FILES_SRC}
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


@needs_monitoring
@posix_descriptors
def test_r29_descriptor_based_cleanup_of_a_temporary_directory_flags_nothing(repo):
    """``shutil.rmtree`` lists and stats through descriptors (``os.scandir(fd)``,
    ``dir_fd=``): with the descriptors ``os.open`` returned known, a
    temporary directory's cleanup outside the checkout records nothing and
    flags nothing, so the test stays precise."""
    files = {
        **SKELETON,
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


@needs_monitoring
@posix_descriptors
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
        **SKELETON,
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


# --------------------------------------------------------------------------- roots and environments


@needs_monitoring
def test_e1_e6_code_reached_through_dotdot_paths_and_files_outside_the_roots(repo):
    chosen = _evidence_selects(
        repo,
        {
            "src/lib/__init__.py": "",
            "src/lib/core.py": "def f():\n    return 1\n",
            "conftest.py": "import pytest\n\n\n@pytest.fixture\ndef val():\n    return 1\n",
            "tests/conftest.py": (
                "import os\nimport sys\n\n"
                "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            ),
            "tests/test_x.py": (
                "from lib.core import f\n\n\ndef test_x():\n    f()\n\n\n"
                "def test_val(val):\n    pass\n"
            ),
        },
        {"src/lib/core.py": "def f():\n    return 22\n"},
        roots=["src", "tests"],
    )
    assert "tests/test_x.py::test_x" in chosen


def test_f4_the_projects_editable_install_is_not_environment_under_collect_rev(
    tmp_path, monkeypatch
):
    from diffcone import collect

    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    monkeypatch.setenv("DIFFCONE_COLLECT_ROOT", str(worktree))
    monkeypatch.setenv("DIFFCONE_COLLECT_REPO", str(repo))
    assert collect._in_checkout(str((repo / "src").resolve()))
    assert collect._in_checkout(str((worktree / "src").resolve()))
    assert not collect._in_checkout(str((tmp_path / "sibling").resolve()))


# An environment made in a directory that holds source.

SVC_IGNORE = {
    ".gitignore": "__pycache__/\n.diffcone/\nsvc/bin/\nsvc/lib/\nsvc/lib64\nsvc/include/\n"
    "svc/Scripts/\nsvc/Lib/\nsvc/Include/\nsvc/pyvenv.cfg\n"
}


def _venv_at(path: Path) -> str:
    """A virtual environment at ``path`` that sees this interpreter's
    packages; the command running pytest in it."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)], check=True)
    scripts = path / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    outer = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    (Path(purelib) / "outer.pth").write_text("".join(f"{p}\n" for p in sorted(outer)))
    return f'"{python}" -m pytest'


CORE = "def f():\n    return 1\n"


@needs_monitoring
def test_rec4_source_beside_an_environment_is_recorded(repo):
    files = {
        **SVC_IGNORE,
        "pytest.ini": "[pytest]\npythonpath = svc\n",
        "svc/app/__init__.py": "",
        "svc/app/core.py": CORE,
        "tests/test_core.py": "from app.core import f\n\n\ndef test_f():\n    assert f() == 1\n",
    }
    base = repo.commit(files)
    evidence = repo.collect(command=_venv_at(repo.path / "svc"))
    # Missed before: everything under svc/ was the environment's.
    assert "svc.app.core.f" in executed(evidence, "tests/test_core.py::test_f")
    head = repo.commit({"svc/app/core.py": CORE.replace("1", "2")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/test_core.py::test_f"}
    assert rules(plan, "tests/test_core.py::test_f") == {"executed_changed"}


@needs_monitoring
def test_rec4_a_package_beside_an_environment_is_not_an_installed_copy(repo):
    files = {
        **SVC_IGNORE,
        "svc/__init__.py": "",
        "svc/app/__init__.py": "",
        "svc/app/core.py": CORE,
        "tests/__init__.py": "",
        "tests/test_core.py": (
            "from svc.app.core import f\n\n\ndef test_f():\n    assert f() == 1\n"
        ),
    }
    repo.commit(files)
    evidence = repo.collect(command=_venv_at(repo.path / "svc"))  # refused before
    assert "tests/test_core.py::test_f" in evidence.tests


@needs_monitoring
@posix_children
def test_rec4_a_project_file_in_what_an_environment_owns_is_refused(repo):
    """Nothing under the environment's own directories is recorded: one
    holding a file of the project would hide what ran from it."""
    files = {
        ".gitignore": "__pycache__/\n.diffcone/\nsvc/bin/python*\nsvc/bin/pip*\nsvc/bin/activate*\n"
        "svc/bin/Activate*\nsvc/lib/\nsvc/lib64\nsvc/include/\nsvc/pyvenv.cfg\n",
        "pytest.ini": "[pytest]\npythonpath = svc/bin\n",
        "svc/bin/tool.py": CORE,
        "tests/test_tool.py": "from tool import f\n\n\ndef test_f():\n    assert f() == 1\n",
    }
    repo.commit(files)
    command = _venv_at(repo.path / "svc")
    with pytest.raises(EvidenceError, match="environment"):
        repo.collect(command=command)


@needs_monitoring
def test_rec4_a_windows_environment_is_found_whatever_the_case(monkeypatch):
    """Windows roots are case-folded; the interpreter's prefix must be too,
    or the environment's own scripts count as the project's."""
    import ntpath

    monkeypatch.setattr(child.os, "name", "nt")
    monkeypatch.setattr(child.os, "path", ntpath)
    monkeypatch.setattr(child.os, "sep", "\\")
    for name in ("prefix", "exec_prefix", "base_prefix"):
        monkeypatch.setattr(child.sys, name, "C:\\Users\\Me\\Repo\\.venv")
    owned = child.environments(("c:\\users\\me\\repo\\",))
    monkeypatch.undo()
    assert ".venv\\scripts\\" in owned and ".venv\\lib\\site-packages\\" in owned


# --------------------------------------------------------------------------- outside every test

# A process started outside every test window ran as part of the phase
# that started it.

GEN = (
    "import sys\n\nfrom pkg.work import handle\n\n"
    "with open(sys.argv[1], 'w') as f:\n    f.write(str(handle(1)))\n"
)

READS = """\
import os


def test_reads():
    with open(os.path.join(os.path.dirname(__file__), "gen.txt")) as f:
        assert f.read() == "2"
"""

RUN_GEN = (
    'subprocess.run([sys.executable, "-m", "pkg.gen", '
    'os.path.join(os.path.dirname(__file__), "gen.txt")], check=True)'
)
READS_ID = "tests/test_reads.py::test_reads"
BENCH = "import pkg.other\n\n\nclass TimeOther:\n    def time_g(self):\n        pkg.other.g()\n"
ASV = [asv_target("bench.TimeOther.time_g", "benchmarks.bench.TimeOther.time_g")]


@needs_monitoring
@posix_children
def test_rec1_a_child_started_at_conftest_import_is_that_import(repo):
    conftest = f"import os\nimport subprocess\nimport sys\n\n{RUN_GEN}\n"
    files = {
        **BASE,
        "pkg/gen.py": GEN,
        "tests/conftest.py": conftest,
        "tests/test_reads.py": READS,
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": BENCH,
    }
    handled, other = _plans(repo, files, HANDLED, OTHER_CHANGED, manifest=ASV)
    # The conftest's import ran handle (in its child) and built what the
    # tests in its scope read: escalated, so every test there is selected.
    assert selected(handled) == {READS_ID, OTHER_ID}
    assert rules(handled, READS_ID) == {"escalated"}
    assert handled.fallbacks == []
    # Not flagged for every change: what the child ran is known.
    assert selected(other) == {OTHER_ID, "bench.TimeOther.time_g"}


@needs_monitoring
@posix_children
def test_rec1_a_child_started_in_a_hook_selects_everything_for_what_it_ran(repo):
    conftest = (
        f"import os\nimport subprocess\nimport sys\n\n\n"
        f"def pytest_sessionstart(session):\n    {RUN_GEN}\n"
    )
    files = {**BASE, "pkg/gen.py": GEN, "tests/conftest.py": conftest, "tests/test_reads.py": READS}
    handled, other = _plans(repo, files, HANDLED, OTHER_CHANGED)
    assert selected(handled) == {READS_ID, OTHER_ID}
    # What the child ran is the hook's, so its change also selects everything
    # as hook code (audit round 3, W5).
    assert [f.rule for f in handled.fallbacks] == [
        "pytest_hook_changed",
        "unobserved_file_changed",
    ]
    assert selected(other) == {OTHER_ID}


@needs_monitoring
@posix_children
def test_rec1_a_child_that_cannot_record_outside_every_test_selects_everything(repo):
    conftest = (
        "import os\nimport subprocess\nimport sys\n\n\n"
        "def pytest_sessionstart(session):\n"
        "    # Another program: what it runs is not seen.\n"
        '    subprocess.run(["sh", "-c", "true"], check=True)\n'
    )
    files = {**BASE, "tests/conftest.py": conftest}
    (other,) = _plans(repo, files, OTHER_CHANGED)
    assert selected(other) == {OTHER_ID}
    assert [f.rule for f in other.fallbacks] == ["subprocess"]


@needs_monitoring
@posix_children
def test_rec1_a_pytest_process_started_outside_every_test_is_its_own_record(repo):
    """An xdist controller starts its workers outside every test window; a
    worker records itself as a test process. Neither flags anything."""
    conftest = (
        "import os\nimport subprocess\nimport sys\n\n"
        "if not os.environ.get('INNER'):\n"
        "    subprocess.run(\n"
        "        [sys.executable, '-m', 'pytest', '-q', '-p', 'diffcone_collect', 'inner'],\n"
        "        env={**os.environ, 'INNER': '1'}, check=True,\n"
        "    )\n"
    )
    files = {
        **BASE,
        "conftest.py": conftest,
        "inner/__init__.py": "",
        "inner/test_inner.py": "from pkg.work import handle\n\n\ndef test_inner():\n"
        "    assert handle(1) == 2\n",
    }
    command = f"{sys.executable} -m pytest tests"
    handled, other = _plans(repo, files, HANDLED, OTHER_CHANGED, command=command)
    assert selected(handled) == {"inner/test_inner.py::test_inner"}
    assert selected(other) == {OTHER_ID}


# What the standard library runs to ask about the machine.


@needs_monitoring
@pytest.mark.parametrize(
    "argv, problem",
    [
        (["uname", "-p"], False),
        (["/usr/bin/uname", "-m"], False),
        (["file", "-b", "/opt/py/bin/python3.13"], False),
        (["/sbin/ldconfig", "-p"], False),
        (["file", "-b", "data/table.bin"], True),
        (["file", "-b", "/repo/data/table.bin"], True),
        (["ldconfig"], True),
        (["make", "data"], True),
    ],
)
def test_rec1_a_system_query_outside_every_test_is_no_problem(tmp_path, argv, problem):
    # An absolute path as the platform has them (``/opt`` has no drive on Windows).
    argv = [str(tmp_path / a.lstrip("/")) if a.startswith("/opt/") else a for a in argv]
    """pytest-xdist workers run ``platform.processor()`` and
    ``platform.architecture()`` as they start: ``uname -p`` and ``file -b
    <python>`` outside every test made every change of strata select every
    test once spawns outside tests counted."""
    (tmp_path / child.CHILDREN).mkdir()
    assert bool(child.resolve(tmp_path, 10, 11, 5, argv).problems) is problem


# --------------------------------------------------------------------------- child processes


@needs_monitoring
def test_e4_a_spawned_multiprocessing_child_flags_its_test(repo):
    work = (
        "import multiprocessing as mp\n\n\ndef square(x):\n    return x * {k}\n\n\n"
        "def run():\n    with mp.get_context('spawn').Pool(1) as pool:\n"
        "        return pool.map(square, [3])[0]\n"
    )
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/work.py": work.format(k="x"),
            "tests/__init__.py": "",
            "tests/test_w.py": "from lib.work import run\n\n\ndef test_spawn():\n    run()\n",
        },
        {"lib/work.py": work.format(k="2")},
    )
    assert "tests/test_w.py::test_spawn" in chosen


@needs_monitoring
@posix_only
def test_e14_a_fixture_computed_in_another_process_is_credited(repo, tmp_path):
    cache = tmp_path / "cache.json"
    conftest = (
        "import json\nimport os\n\nimport pytest\n\nfrom lib.compute import compute\n\n"
        f"CACHE = {str(cache)!r}\n\n\n"
        "@pytest.fixture(scope='session')\ndef table():\n"
        "    if os.path.exists(CACHE):\n        with open(CACHE) as f:\n"
        "            return json.load(f)\n"
        "    value = compute()\n    with open(CACHE, 'w') as f:\n        json.dump(value, f)\n"
        "    return value\n"
    )
    compute = "def compute():\n    return [{}]\n"
    base = repo.commit(
        {
            ".gitignore": "__pycache__/\n.diffcone/\n",
            "lib/__init__.py": "",
            "lib/compute.py": compute.format(1),
            "lib/other.py": "def g():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/conftest.py": conftest,
            "tests/test_a.py": "def test_a(table):\n    assert table\n",
            "tests/test_b.py": "def test_b(table):\n    assert table\n",
            "tests/test_c.py": "from lib.other import g\n\n\ndef test_c():\n    g()\n",
        }
    )
    # Two pytest processes, as two xdist workers: the first computes.
    py = sys.executable
    command = (
        f'sh -c \'{py} -m pytest "$@" tests/test_a.py tests/test_c.py && '
        f'{py} -m pytest "$@" tests/test_b.py\' sh'
    )
    evidence = repo.collect(command=command)
    head = repo.commit({"lib/compute.py": compute.format(2)})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


@needs_monitoring
@pytest.mark.parametrize(
    "test",
    [
        # E17: a subinterpreter.
        "import pytest\n\n\ndef test_t():\n"
        "    interpreters = pytest.importorskip('concurrent.interpreters')\n"
        "    interpreters.create().close()\n",
        # A subprocess running a script (not an inert probe, roadmap 10).
        "import subprocess\nimport sys\n\n\ndef test_t():\n"
        "    subprocess.run([sys.executable, '-c', 'import lib.other'], check=True)\n",
    ],
)
def test_e17_code_the_recording_cannot_see_flags_its_test(repo, test):
    if "interpreters" in test and sys.version_info < (3, 14):
        pytest.skip("subinterpreters need Python 3.14")
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/other.py": "def g():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_t.py": test,
            "tests/test_u.py": "def test_u():\n    pass\n",
        },
        {"lib/other.py": "def g():\n    return 2\n"},
    )
    assert chosen == {"tests/test_t.py::test_t"}


# A pool's workers serve later tests.

PAR = """\
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from pkg.work import handle

_POOL = []


def compute(x):
    if not _POOL:
        context = multiprocessing.get_context({method!r})
        _POOL.append(ProcessPoolExecutor(max_workers=1, mp_context=context))
    return _POOL[0].submit(handle, x).result()
"""

POOL_TESTS = """\
from pkg.par import compute


def test_a():
    assert compute(1) == 2


def test_b():
    assert compute(1) == 2
"""


@needs_monitoring
@posix_children
@pytest.mark.parametrize("method", ["spawn", "forkserver", "fork"])
def test_rec2_a_test_using_a_running_pool_is_flagged(repo, method):
    files = {**BASE, "pkg/par.py": PAR.format(method=method), "tests/test_par.py": POOL_TESTS}
    (handled,) = _plans(repo, files, HANDLED)
    # test_b started nothing: its work ran in test_a's worker.
    assert {"tests/test_par.py::test_a", "tests/test_par.py::test_b"} <= selected(handled)
    assert "subprocess" in rules(handled, "tests/test_par.py::test_b")


UNSEEN = """\
import subprocess
import sys

WORKER = []


class Unseen(subprocess.Popen):
    # Started where no wrapper of the recorder sees it, as loky's workers
    # are (through _posixsubprocess directly).
    _execute_child = getattr(subprocess.Popen._execute_child, "__wrapped__",
                             subprocess.Popen._execute_child)


def test_start():
    WORKER.append(Unseen([sys.executable, "-m", "pkg.serve"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True))


def test_use():
    WORKER[0].stdin.write("1\\n")
    WORKER[0].stdin.flush()
    assert WORKER[0].stdout.readline().strip() == "2"
    WORKER[0].stdin.close()
    WORKER[0].wait()


def test_after():
    pass
"""

SERVE = (
    "import sys\n\nfrom pkg.work import handle\n\n"
    "for line in sys.stdin:\n    print(handle(int(line)), flush=True)\n"
)


@needs_monitoring
@posix_children
def test_rec2_a_python_child_no_wrapper_saw_flags_the_tests_it_ran_beside(repo):
    """Its own record shows it ran: every test while it runs is flagged."""
    files = {**BASE, "pkg/serve.py": SERVE, "tests/test_unseen.py": UNSEEN}
    (other,) = _plans(repo, files, OTHER_CHANGED)
    unseen = {f"tests/test_unseen.py::test_{n}" for n in ("start", "use")}
    assert selected(other) == {OTHER_ID, *unseen}
    assert rules(other, "tests/test_unseen.py::test_use") == {"subprocess"}


@needs_monitoring
@posix_children
def test_rec2_a_fork_that_has_ended_flags_no_later_test(repo):
    forks = (
        "import os\n\n\ndef test_fork():\n    pid = os.fork()\n    if pid == 0:\n"
        "        os._exit(0)\n    os.waitpid(pid, 0)\n\n\ndef test_after():\n    pass\n"
    )
    (other,) = _plans(repo, {**BASE, "tests/test_fork.py": forks}, OTHER_CHANGED)
    assert selected(other) == {OTHER_ID, "tests/test_fork.py::test_fork"}


# The project's own sitecustomize.


@needs_monitoring
@posix_children
def test_rec8_a_project_sitecustomize_does_not_hide_the_child_recorder(repo):
    test = (
        "import subprocess\nimport sys\n\n\ndef test_child():\n"
        "    out = subprocess.run([sys.executable, '-m', 'pkg.tool'], capture_output=True, "
        "text=True)\n"
        "    assert out.stdout.strip() == '2', out.stderr\n"
        "    # The project's own sitecustomize still ran.\n"
        "    out = subprocess.run([sys.executable, '-m', 'pkg.site'], capture_output=True, "
        "text=True)\n"
        "    assert out.stdout.strip() == 'True', out.stderr\n"
    )
    files = {
        **BASE,
        "sitecustomize.py": "import builtins\n\nbuiltins.PROJECT_SITE = True\n",
        "pkg/site.py": "import builtins\n\nprint(builtins.PROJECT_SITE)\n",
        "tests/test_child.py": test,
    }
    handled, other = _plans(repo, files, HANDLED, OTHER_CHANGED)
    assert selected(handled) == {"tests/test_child.py::test_child"}
    assert rules(handled, "tests/test_child.py::test_child") == {"executed_changed"}
    assert selected(other) == {OTHER_ID}


# --------------------------------------------------------------------------- interpreter probes

WINDOWS = "windows"  # flagged only where children do not record themselves


def _flags(flagged) -> bool:
    return flagged is True or (flagged == WINDOWS and os.name == "nt")


@needs_monitoring
@pytest.mark.parametrize(
    "probe, flagged",
    [
        ("['-c', 'import sys; print(sys.version_info[:2])']", False),
        ("['-I', '-c', 'import sys; print(sys.version_info[:2])']", False),
        ("['-c', 'import lib.other']", True),
        # A child records itself (roadmap item 15), except on Windows: these
        # run no project code, and a file they open is recorded.
        ("['-c', 'exec(open(\"x.py\").read())']", WINDOWS),
        ("['-m', 'json.tool', '--help']", WINDOWS),
        # Not recorded: an isolated interpreter skips sitecustomize.
        ("['-I', '-c', 'import lib.other']", True),
    ],
)
def test_an_interpreter_probe_that_runs_no_project_code_does_not_flag(repo, probe, flagged):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/other.py": "def g():\n    return 1\n",
            "lib/core.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_p.py": (
                "import subprocess\nimport sys\n\nfrom lib.core import f\n\n\ndef test_p():\n"
                f"    subprocess.run([sys.executable] + {probe}, capture_output=True)\n"
                "    f()\n"
            ),
        },
        {"lib/other.py": "def g():\n    return 2\n"},
    )
    assert ("tests/test_p.py::test_p" in chosen) is _flags(flagged)


@needs_monitoring
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
@pytest.mark.parametrize(
    "command, flagged",
    [
        # strata caches this probe; the recorder clears project caches before
        # each test, so every test that reached it was flagged.
        ("['uv', 'python', 'list', '--only-installed']", False),
        # strata's own call: on Windows the path ends in ``uv.EXE`` and the
        # process raises a second (``_winapi.CreateProcess``) event.
        (
            "[shutil.which('uv'), 'python', 'list', '--only-installed', '--output-format', 'json']",
            False,
        ),
        # A launcher: the Python it runs records itself, except on Windows.
        ("['uv', 'run', '--no-project', 'python', '-c', 'pass']", WINDOWS),
        ("[shutil.which('uv'), 'run', '--no-project', 'python', '-c', 'pass']", WINDOWS),
        ("['uv', 'run', '--no-project', 'python', '-I', '-c', 'import lib.other']", True),
    ],
)
def test_a_uv_interpreter_query_does_not_flag(repo, command, flagged):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/other.py": "def g():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_p.py": (
                "import shutil\nimport subprocess\n\n\ndef test_p():\n"
                f"    subprocess.run({command}, capture_output=True)\n"
            ),
        },
        {"lib/other.py": "def g():\n    return 2\n"},
    )
    assert ("tests/test_p.py::test_p" in chosen) is _flags(flagged)


def test_a_uv_query_by_its_windows_name_is_inert(monkeypatch):
    """``shutil.which("uv")`` names ``uv.EXE`` on Windows: 779 of strata's
    2 606 Windows tests were flagged for ``uv python list``."""
    from diffcone import collect

    argv = ["/opt/tools/uv.EXE", "python", "list", "--only-installed"]
    assert collect._inert_probe("subprocess.Popen", (argv[0], argv, None, None))
    assert not collect._inert_probe(
        "subprocess.Popen", (argv[0], ["/opt/tools/uv.EXE", "run", "x"], None, None)
    )


@needs_monitoring
def test_a_program_named_like_python_is_not_an_interpreter(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/core.py": "def f():\n    return 1\n",
            "bin/python-tool": "#!/bin/sh\nexit 0\n",
            "tests/__init__.py": "",
            "tests/test_tool.py": (
                "import subprocess\n\n\ndef test_tool():\n"
                "    subprocess.run(['bin/python-tool', '-c', 'x'], executable='sh')\n"
            ),
            "tests/test_other.py": "from lib.core import f\n\n\ndef test_other():\n    f()\n",
        },
        {"lib/core.py": "def f():\n    return 2\n"},
    )
    assert "tests/test_tool.py::test_tool" in chosen


@needs_monitoring
@pytest.mark.parametrize(
    "call",
    [
        # Project code found through the child's working directory, or
        # through PYTHONPATH: the snippet itself names no project package.
        "subprocess.run([sys.executable, '-c', 'import helper; assert helper.VALUE == 1'], "
        "cwd=HERE, check=True)",
        "subprocess.run([sys.executable, '-c', 'import helper; assert helper.VALUE == 1'], "
        "env={**os.environ, 'PYTHONPATH': HERE}, check=True)",
        # A standard-library module that runs code it reads from a file.
        "subprocess.run([sys.executable, '-c', 'import doctest, sys; "
        'sys.exit(doctest.testfile("t.txt", module_relative=False).failed)\'], '
        "cwd=HERE, check=True)",
    ],
)
def test_a_snippet_that_can_run_project_code_still_flags(repo, call):
    chosen = _evidence_selects(
        repo,
        {
            "tests/__init__.py": "",
            "tests/helper.py": "VALUE = 1\n",
            "tests/t.txt": ">>> import helper\n>>> helper.VALUE\n1\n",
            "tests/test_p.py": (
                "import os\nimport subprocess\nimport sys\n\nHERE = os.path.dirname(__file__)\n\n\n"
                f"def test_p():\n    {call}\n"
            ),
            "tests/test_q.py": "def test_q():\n    pass\n",
        },
        {"tests/helper.py": "VALUE = 22\n"},
    )
    assert "tests/test_p.py::test_p" in chosen


@needs_monitoring
def test_a_version_probe_in_another_directory_stays_inert(repo):
    """strata's probe: a working directory, ``import sys`` only."""
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/core.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_v.py": (
                "import os\nimport subprocess\nimport sys\n\n\ndef test_v():\n"
                "    code = \"import sys; print(f'{sys.version_info.major}')\"\n"
                "    subprocess.run([sys.executable, '-c', code],"
                " cwd=os.path.dirname(__file__), check=True)\n"
            ),
            "tests/test_f.py": "from lib.core import f\n\n\ndef test_f():\n    f()\n",
        },
        {"lib/core.py": "def f():\n    return 2\n"},
    )
    assert chosen == {"tests/test_f.py::test_f"}


# An inert-looking probe is judged by its own record.

PROBE_TEST = """\
import subprocess
import sys


def test_child():
    out = subprocess.run([sys.executable, {flags}"-c", {snippet!r}], capture_output=True, text=True)
    assert out.stdout.strip() == "{expected}", out.stderr
"""


@needs_monitoring
@posix_children
@pytest.mark.parametrize(
    "snippet",
    [
        "import posix, sys; "
        "pid = posix.posix_spawn(sys.executable, [sys.executable, '-m', 'pkg.tool'], "
        "posix.environ); "
        "posix.waitpid(pid, 0)",
        "b = globals()['__builtins__']; "
        "print(getattr(b, '__imp' + 'ort__')('pkg.work', fromlist=['x']).handle(1))",
    ],
    ids=["posix_spawn", "assembled_import"],
)
def test_rec7_a_probe_that_runs_project_code_flags(repo, snippet):
    test = PROBE_TEST.format(flags="", snippet=snippet, expected="2")
    (other,) = _plans(repo, {**BASE, "tests/test_child.py": test}, OTHER_CHANGED)
    # Judged inert by its command line, it was neither flagged nor followed.
    assert selected(other) == {"tests/test_child.py::test_child", OTHER_ID}
    assert rules(other, "tests/test_child.py::test_child") == {"subprocess"}


@needs_monitoring
@posix_children
@pytest.mark.parametrize("flags", ["", '"-I", '], ids=["recorded", "isolated"])
def test_rec7_a_version_probe_still_flags_nothing(repo, flags):
    snippet = "import sys; print(sys.version_info[0])"
    test = PROBE_TEST.format(flags=flags, snippet=snippet, expected="3")
    (other,) = _plans(repo, {**BASE, "tests/test_child.py": test}, OTHER_CHANGED)
    assert selected(other) == {OTHER_ID}


# --------------------------------------------------------------------------- text code

# Text code the index does not hold.

CONST = "CONST = 1\n"
DOC_CONST = ">>> from pkg.const import CONST\n>>> CONST\n1\n"


@needs_monitoring
@posix_children
@pytest.mark.parametrize(
    "argv",
    [
        "[sys.executable, '-m', 'doctest', 'docs/usage.txt']",
        "[sys.executable, '-m', 'timeit', '-n', '1', '-r', '1', '-s', "
        "'from pkg.const import CONST', 'assert CONST == 1']",
    ],
    ids=["doctest", "timeit"],
)
def test_rec5_a_child_running_text_by_a_module_outside_the_project_flags(repo, argv):
    test = (
        "import subprocess\nimport sys\n\n\ndef test_docs():\n"
        f"    out = subprocess.run({argv}, capture_output=True, text=True)\n"
        "    assert out.returncode == 0, out.stdout + out.stderr\n"
    )
    files = {**BASE, "pkg/const.py": CONST, "docs/usage.txt": DOC_CONST, "tests/test_docs.py": test}
    changed, other = _plans(repo, files, {"pkg/const.py": "CONST = 2\n"}, OTHER_CHANGED)
    assert selected(changed) == {"tests/test_docs.py::test_docs"}
    assert "subprocess" in rules(changed, "tests/test_docs.py::test_docs")
    assert selected(other) == {"tests/test_docs.py::test_docs", OTHER_ID}


@needs_monitoring
@pytest.mark.parametrize(
    "body",
    [
        "    assert doctest.testfile('../docs/usage.txt').failed == 0\n",
        "    timeit.timeit('assert CONST == 1', setup='from pkg.const import CONST', number=1)\n",
    ],
    ids=["doctest", "timeit"],
)
def test_rec5_text_run_in_the_test_process_flags(repo, body):
    test = f"import doctest\nimport timeit\n\n\ndef test_docs():\n{body}"
    files = {**BASE, "pkg/const.py": CONST, "docs/usage.txt": DOC_CONST, "tests/test_docs.py": test}
    changed, other = _plans(repo, files, {"pkg/const.py": "CONST = 2\n"}, OTHER_CHANGED)
    assert selected(changed) == {"tests/test_docs.py::test_docs"}
    # ``timeit``'s text is also read statically since audit round 5 (R12):
    # the test is a reader of ``CONST`` beside running text no file holds.
    assert rules(changed, "tests/test_docs.py::test_docs") in (
        {"text_code"},
        {"text_code", "executed_reader"},
    )
    # Always: the text may read any name.
    assert selected(other) == {"tests/test_docs.py::test_docs", OTHER_ID}


@needs_monitoring
def test_rec5_generated_methods_and_a_literal_flag_nothing(repo, tmp_path, monkeypatch):
    """What dataclasses and namedtuple generate, a library's ``eval`` of a
    literal, and code a library generates as it is imported (numpy 1.x's
    dispatch wrappers), read nothing of the project."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "fakelib.py").write_text(
        "def _helper():\n    return 0\n\n\n"
        "exec(compile('def wrapped():\\n    return _helper()\\n', '<generated>', 'exec'))\n\n\n"
        "def compute():\n    return eval('1 + 2') + wrapped()\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(site))
    test = (
        "import collections\nimport dataclasses\n\n\ndef test_gen():\n"
        "    import fakelib\n\n"
        "    @dataclasses.dataclass\n    class A:\n        x: int = 1\n\n"
        "    P = collections.namedtuple('P', 'a b')\n"
        "    assert A().x + P(1, 2).a + fakelib.compute() == 5\n"
    )
    (other,) = _plans(repo, {**BASE, "tests/test_gen.py": test}, OTHER_CHANGED)
    assert selected(other) == {OTHER_ID}


FLAG_OPS = (
    "def value():\n    return 1\n\n\ndef other():\n    return 10\n\n\n"
    "def flag():\n    return False\n"
)
T_OTHER = "from pkg import ops\n\n\ndef test_b():\n    assert ops.other() == 10\n"


@needs_monitoring
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
        {**SKELETON, "pkg/ops.py": FLAG_OPS, "tests/test_a.py": test_a, "tests/test_b.py": T_OTHER}
    )
    evidence = repo.collect()
    assert bool(evidence.tests[A].flags & FLAG_TEXT) is text
    head = repo.commit({"pkg/ops.py": FLAG_OPS.replace("return 1\n", "return 2\n", 1)})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == ({A} if text else set())
    head = repo.commit({"pkg/ops.py": FLAG_OPS.replace("return False", "return True")})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == ({A} if text else set())


# --------------------------------------------------------------------------- annotations from text

ADD_OPS = "def add(a, b):\n    return a + b\n"

TYPED = """\
from pathlib import Path

from pkg.ops import add


def test_typed(tmp_path: Path) -> None:
    assert add(1, 2) == 3


def test_plain():
    assert add(2, 2) == 4
"""

TYPED_OTHER = "from pkg.ops import add\n\n\ndef test_other():\n    assert add(0, 0) == 0\n"
TYPED_FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": ADD_OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TYPED,
    "tests/test_other.py": TYPED_OTHER,
}

# Annotations evaluated from text (``from __future__ import annotations``,
# forward references) by a library outside the checkout, as pydantic and
# FastAPI do: before Python 3.14 nothing ran a code object of the project,
# and the evaluation was exempt from the text-code flag, so a test whose
# outcome depended on the annotations had no record of them (E10, a miss);
# ``inspect`` evaluating them instead flagged every such test (E9).

FRAMEWORK = """\
import inspect
import typing


def check(mod):
    for name in sorted(vars(mod)):
        obj = vars(mod)[name]
        if getattr(obj, "__module__", None) == mod.__name__ and callable(obj):
            {how}
"""

STRING_API = (
    "from __future__ import annotations\n\nfrom pkg import models\n\n\n"
    "def handler(x: models.Item) -> None:\n    pass\n\n\n"
    "class Model:\n    item: models.Item\n"
)


def _framework_repo(repo, how):
    site = repo.path.parent / "site"
    site.mkdir(exist_ok=True)
    (site / "fw.py").write_text(FRAMEWORK.format(how=how), encoding="utf-8")
    return {
        **TYPED_FILES,
        "pkg/models.py": "class Item:\n    pass\n\n\nclass Other:\n    pass\n",
        "pkg/api.py": STRING_API,
        "tests/conftest.py": f"import sys\n\nsys.path.insert(0, {str(site)!r})\n",
        "tests/test_fw.py": (
            "import fw\nimport pkg.api\n\n\ndef test_check():\n    fw.check(pkg.api)\n"
        ),
    }


@needs_monitoring
@pytest.mark.parametrize(
    "how", ["typing.get_type_hints(obj)", "inspect.get_annotations(obj, eval_str=True)"]
)
def test_e10_a_changed_annotation_a_library_evaluated_from_text(repo, how):
    base = repo.commit(_framework_repo(repo, how))
    ev = repo.collect()
    head = repo.commit({"pkg/api.py": STRING_API.replace("x: models.Item", "x: models.Missing")})
    assert "tests/test_fw.py::test_check" in selected(_plan(repo, base, head, ev))


@needs_monitoring
def test_e10_a_deleted_class_a_class_annotation_named(repo):
    """``Model.item: models.Item`` evaluated by the library: the test read
    ``Item`` through the text; deleting it fails the test."""
    base = repo.commit(_framework_repo(repo, "typing.get_type_hints(obj)"))
    ev = repo.collect()
    head = repo.commit({"pkg/models.py": "class Other:\n    pass\n"})
    assert "tests/test_fw.py::test_check" in selected(_plan(repo, base, head, ev))


@needs_monitoring
def test_e9_annotations_evaluated_from_text_are_not_text_code(repo):
    """``inspect`` evaluating string annotations (before Python 3.14) is an
    annotation evaluator like ``typing``: what it reads is credited, and
    the test is no longer always selected."""
    base = repo.commit(_framework_repo(repo, "inspect.signature(obj, eval_str=True)"))
    ev = repo.collect()
    assert ev.tests["tests/test_fw.py::test_check"].flags == 0
    head = repo.commit({"pkg/ops.py": ADD_OPS.replace("a + b", "b + a")})
    assert "tests/test_fw.py::test_check" not in selected(_plan(repo, base, head, ev))

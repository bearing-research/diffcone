"""Regression scenarios for the third audit round's recorder findings (REC,
internal/audit.md, round 3).

Each records real evidence in a fixture repository and plans with it, as
test_evidence_scenarios.py does; each test names the finding it guards and
failed before its fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from diffcone import child
from diffcone.evidence import EvidenceError
from diffcone.testing import asv_target, executed, rules, selected, touched

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
# Children record themselves on POSIX only (roadmap item 15).
posix_only = pytest.mark.skipif(os.name == "nt", reason="children record on POSIX")

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


# REC-1: a process started outside every test window ran as part of the
# phase that started it.

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


@posix_only
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


@posix_only
def test_rec1_a_child_started_in_a_hook_selects_everything_for_what_it_ran(repo):
    conftest = (
        f"import os\nimport subprocess\nimport sys\n\n\n"
        f"def pytest_sessionstart(session):\n    {RUN_GEN}\n"
    )
    files = {**BASE, "pkg/gen.py": GEN, "tests/conftest.py": conftest, "tests/test_reads.py": READS}
    handled, other = _plans(repo, files, HANDLED, OTHER_CHANGED)
    assert selected(handled) == {READS_ID, OTHER_ID}
    assert [f.rule for f in handled.fallbacks] == ["unobserved_file_changed"]
    assert selected(other) == {OTHER_ID}


@posix_only
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


@posix_only
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


# REC-2: a pool's workers serve later tests.

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


@posix_only
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


@posix_only
def test_rec2_a_python_child_no_wrapper_saw_flags_the_tests_it_ran_beside(repo):
    """Its own record shows it ran: every test while it runs is flagged."""
    files = {**BASE, "pkg/serve.py": SERVE, "tests/test_unseen.py": UNSEEN}
    (other,) = _plans(repo, files, OTHER_CHANGED)
    unseen = {f"tests/test_unseen.py::test_{n}" for n in ("start", "use")}
    assert selected(other) == {OTHER_ID, *unseen}
    assert rules(other, "tests/test_unseen.py::test_use") == {"subprocess"}


@posix_only
def test_rec2_a_fork_that_has_ended_flags_no_later_test(repo):
    forks = (
        "import os\n\n\ndef test_fork():\n    pid = os.fork()\n    if pid == 0:\n"
        "        os._exit(0)\n    os.waitpid(pid, 0)\n\n\ndef test_after():\n    pass\n"
    )
    (other,) = _plans(repo, {**BASE, "tests/test_fork.py": forks}, OTHER_CHANGED)
    assert selected(other) == {OTHER_ID, "tests/test_fork.py::test_fork"}


# REC-3: a library's top level that an import ran is not the import system.

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


def test_rec3_a_library_reading_a_file_as_it_is_imported_in_a_test(repo, fakeplot):
    test = "def test_style():\n    import fakeplot\n\n    assert fakeplot.CONFIG == 'a'\n"
    files = {**BASE, "plotrc": "a", "tests/test_style.py": test}
    (changed,) = _plans(repo, files, {"plotrc": "b"})
    assert selected(changed) == {"tests/test_style.py::test_style"}
    assert rules(changed, "tests/test_style.py::test_style") == {"touched_file"}


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


# REC-4: an environment made in a directory that holds source.

SVC_IGNORE = {
    ".gitignore": "__pycache__/\n.diffcone/\nsvc/bin/\nsvc/lib/\nsvc/include/\nsvc/pyvenv.cfg\n"
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


@posix_only
def test_rec4_a_project_file_in_what_an_environment_owns_is_refused(repo):
    """Nothing under the environment's own directories is recorded: one
    holding a file of the project would hide what ran from it."""
    files = {
        ".gitignore": "__pycache__/\n.diffcone/\nsvc/bin/python*\nsvc/bin/pip*\nsvc/bin/activate*\n"
        "svc/bin/Activate*\nsvc/lib/\nsvc/include/\nsvc/pyvenv.cfg\n",
        "pytest.ini": "[pytest]\npythonpath = svc/bin\n",
        "svc/bin/tool.py": CORE,
        "tests/test_tool.py": "from tool import f\n\n\ndef test_f():\n    assert f() == 1\n",
    }
    repo.commit(files)
    command = _venv_at(repo.path / "svc")
    with pytest.raises(EvidenceError, match="environment"):
        repo.collect(command=command)


# REC-5: text code the index does not hold.

DOC = ">>> from pkg.work import handle\n>>> handle(1)\n2\n"
CONST = "CONST = 1\n"
DOC_CONST = ">>> from pkg.const import CONST\n>>> CONST\n1\n"


@posix_only
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
    assert rules(changed, "tests/test_docs.py::test_docs") == {"text_code"}
    # Always: the text may read any name.
    assert selected(other) == {"tests/test_docs.py::test_docs", OTHER_ID}


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


# REC-6: a file opened in another case on a file system that folds case.

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


def test_rec6_a_file_opened_in_another_case_is_the_indexed_file(repo, folds_case):
    files = {**BASE, "tests/data/expected.txt": "2", "tests/test_case.py": CASE_TEST}
    edited, added = _plans(
        repo, files, {"tests/data/expected.txt": "3"}, {"tests/data/new.txt": "x"}
    )
    assert selected(edited) == {"tests/test_case.py::test_reads"}
    assert rules(edited, "tests/test_case.py::test_reads") == {"touched_file"}
    # Probed for in another case while absent.
    assert selected(added) == {"tests/test_case.py::test_probes"}


def test_rec6_the_recording_says_its_file_system_folds_case(repo, folds_case):
    base = repo.commit({**BASE, "tests/data/expected.txt": "2", "tests/test_case.py": CASE_TEST})
    evidence = repo.collect()
    assert evidence.case_insensitive
    assert "tests/data/expected.txt" in touched(evidence, "tests/test_case.py::test_reads")
    assert base


# REC-7: an inert-looking probe is judged by its own record.

PROBE_TEST = """\
import subprocess
import sys


def test_child():
    out = subprocess.run([sys.executable, {flags}"-c", {snippet!r}], capture_output=True, text=True)
    assert out.stdout.strip() == "{expected}", out.stderr
"""


@posix_only
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


@posix_only
@pytest.mark.parametrize("flags", ["", '"-I", '], ids=["recorded", "isolated"])
def test_rec7_a_version_probe_still_flags_nothing(repo, flags):
    snippet = "import sys; print(sys.version_info[0])"
    test = PROBE_TEST.format(flags=flags, snippet=snippet, expected="3")
    (other,) = _plans(repo, {**BASE, "tests/test_child.py": test}, OTHER_CHANGED)
    assert selected(other) == {OTHER_ID}


# REC-8: the project's own sitecustomize.


@posix_only
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


# REC-9: a record whose write failed.


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

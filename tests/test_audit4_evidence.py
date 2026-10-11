"""Regression scenarios for the fourth audit round (the code-quality pass),
planning from execution evidence (Q3, Q16, Q17).

Each test names the finding it guards and failed before its fix. Evidence is
recorded for real in a fixture repository (``diffcone collect`` runs its
suite under the recorder); assertions check exact target sets.
"""

from __future__ import annotations

import sys
import time

import pytest

from diffcone.evidence import Evidence, TestRecord, list_stores, load_store, write_store
from diffcone.testing import asv_target, selected

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _plan(repo, base, head, evidence, targets=()):
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], evidence=evidence)


# --------------------------------------------------------------------------- Q3

STATE = """\
MODE = ["slow"]


def set_mode(m):
    _store(m)
    return m


def _store(m):
    MODE[0] = m


def mode():
    return MODE[0]
"""
READS_MODE = 'from pkg.state import mode\n\n\ndef test_b():\n    assert mode() == "slow"\n'
IMPORTS_CONFIG = "import pkg.config  # noqa: F401\n\n\ndef test_a():\n    pass\n"
# ``set_mode`` is named at both revisions, at the top level and in a
# ``def`` header (``ALIAS``, ``g``): the module's own dependencies do not
# change, only what its ``def`` statements run.
CONFIG = (
    "from pkg.state import set_mode\n\nALIAS = set_mode\n\n\ndef g(x: set_mode):\n"
    "    pass\n\n\ndef h(x: int):\n    return x\n\n\nY: int = 1\n"
)
BOTH = {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
# The benchmark keeps its static decision: ``set_mode`` runs at import with
# what changed.
BENCH = asv_target("benchmarks.TimeMode.time_set_mode", "pkg.state.set_mode")


@needs_monitoring
@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (CONFIG, CONFIG + '\n\ndef f(x: set_mode("fast")):\n    pass\n', BOTH),
        (CONFIG, CONFIG + '\n\ndef f() -> set_mode("fast"):\n    pass\n', BOTH),
        (CONFIG + '\n\ndef f(x: set_mode("slow")):\n    pass\n', CONFIG, BOTH),
        (
            CONFIG,
            CONFIG + '\n\nclass K:\n    def f(self, x: set_mode("fast")):\n        pass\n',
            BOTH,
        ),
        (CONFIG, CONFIG.replace("h(x: int)", 'h(x: set_mode("fast"))'), BOTH),
        # A variable's annotation is evaluated at import too; the variable's
        # readers and what its statement writes are reached, as for
        # ``X = set_mode("fast")`` (its module is not escalated).
        (CONFIG, CONFIG + '\n\nZ: set_mode("fast") = 1\n', {"tests/test_b.py::test_b"}),
        (
            CONFIG,
            CONFIG.replace("Y: int = 1", 'Y: set_mode("fast") = 1'),
            {"tests/test_b.py::test_b"},
        ),
    ],
    ids=[
        "parameter annotation added",
        "return annotation added",
        "annotated function deleted",
        "method annotation added",
        "annotation changed",
        "variable annotation added",
        "variable annotation changed",
    ],
)
def test_q3_an_annotation_that_calls_code_runs_at_import(repo, before, after, expected):
    """Before Python 3.14 a ``def``'s annotations are evaluated with it:
    ``def f(x: set_mode("fast"))`` writes ``MODE`` when ``pkg.config`` is
    imported, which ``test_b`` reads without running anything of
    ``pkg.config``."""
    base = repo.commit(
        {
            **BASE,
            "pkg/state.py": STATE,
            "pkg/config.py": before,
            "tests/test_a.py": IMPORTS_CONFIG,
            "tests/test_b.py": READS_MODE,
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/config.py": after})
    plan = _plan(repo, base, head, evidence, [BENCH])
    assert selected(plan) == expected | {BENCH.runner_id}


@needs_monitoring
@pytest.mark.parametrize(
    "after",
    [
        CONFIG + "\n\ndef f(x: int) -> str:\n    return str(x)\n",
        CONFIG.replace("h(x: int)", "h(x: str)"),
    ],
    ids=["added", "changed"],
)
def test_q3_an_annotation_that_calls_nothing_reaches_its_readers_only(repo, after):
    """Narrowing (F3, and now a definition change): an annotation that only
    names something runs no code at import, so the module is not escalated;
    the code reading the function's annotations still sees the change."""
    base = repo.commit(
        {
            **BASE,
            "pkg/state.py": STATE,
            "pkg/config.py": CONFIG,
            "tests/test_a.py": IMPORTS_CONFIG,
            "tests/test_b.py": READS_MODE,
            "tests/test_hints.py": (
                "import typing\n\nimport pkg.config\n\n\ndef test_hints():\n"
                "    assert typing.get_type_hints(pkg.config.h) == {'x': int}\n"
            ),
            "tests/test_calls.py": (
                "from pkg.config import h\n\n\ndef test_calls():\n    assert h(1) == 1\n"
            ),
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/config.py": after})
    plan = _plan(repo, base, head, evidence)
    # ``get_type_hints`` is a lookup by a name nothing bounds: it sees an
    # added name of the module it can see as well.
    expected = {"tests/test_hints.py::test_hints"}
    if "h(x: str)" in after:
        expected.add("tests/test_calls.py::test_calls")
    assert selected(plan) == expected


# --------------------------------------------------------------------------- Q16


@pytest.mark.parametrize(
    "name",
    [
        "we#ird",
        pytest.param(
            "what?now",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="? is not allowed in a path"),
        ),
        "pct%41",
        "a b",
    ],
)
def test_q16_a_store_path_with_uri_characters_opens(tmp_path, name):
    """The store is opened read-only through a URI: ``#``, ``?`` and ``%`` in
    the path are percent-encoded, not read as a fragment, query or escape."""
    evidence = Evidence(
        commit="c" * 40,
        source_roots=["."],
        environment={"python": "3.13.1 (main)"},
        environment_hash="e",
        command="pytest",
        created=time.time(),
        symbols=["pkg.lib.f"],
        paths=[],
        tests={"tests/test_a.py::test_a": TestRecord(frozenset({0}), frozenset())},
    )
    repo = tmp_path / name
    store = write_store(evidence, repo / ".diffcone" / "evidence")
    loaded = load_store(store)
    assert loaded.tests == evidence.tests
    assert loaded.symbols == ["pkg.lib.f"]
    (info,) = list_stores(repo)
    assert (info.commit, info.tests, info.python) == ("c" * 40, 1, "3.13.1")


def test_q16_a_store_without_a_python_version_lists(tmp_path):
    """A recorded environment with an empty ``python`` field is listed, not
    an IndexError (quality item 19)."""
    evidence = Evidence(
        commit="d" * 40,
        source_roots=["."],
        environment={"python": ""},
        environment_hash="e",
        command="pytest",
        created=time.time(),
        symbols=[],
        paths=[],
        tests={},
    )
    write_store(evidence, tmp_path / ".diffcone" / "evidence")
    (info,) = list_stores(tmp_path)
    assert info.python == ""


# --------------------------------------------------------------------------- Q17

BUILD_FILES = {
    **BASE,
    "pkg/build.py": "def make():\n    return 1\n",
    "pkg/other.py": "def g():\n    return 2\n",
    "tests/test_a.py": "from pkg.build import make\n\n\ndef test_a():\n    assert make() == 1\n",
    "tests/test_b.py": "from pkg.other import g\n\n\ndef test_b():\n    assert g() == 2\n",
}


@needs_monitoring
def test_q17_an_ordinary_build_module_selects_its_users(repo):
    """``pkg/build.py`` is an ordinary module (no ``pyproject.toml`` beside
    it): editing it selects the tests that ran it, as static planning does,
    not every test."""
    base = repo.commit(BUILD_FILES)
    evidence = repo.collect()
    head = repo.commit({"pkg/build.py": "def make():\n    x = 1\n    return x\n"})
    bench = asv_target("benchmarks.TimeOther.time_g", "pkg.other.g")
    plan = _plan(repo, base, head, evidence, [bench])
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert not plan.fallbacks


@needs_monitoring
@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("build.py", {}),
        ("pkg/build.py", {"pkg/pyproject.toml": "[project]\nname = 'pkg'\n"}),
        ("pkg/setup.py", {}),
    ],
    ids=["build.py at the root", "build.py beside pyproject.toml", "setup.py"],
)
def test_q17_a_build_script_still_selects_everything(repo, path, extra):
    """A real build script decides what is installed: every test, whether it
    ran it or not."""
    files = {**BUILD_FILES, **extra, path: "def make():\n    return 1\n"}
    files["tests/test_a.py"] = (
        f"from {path[:-3].replace('/', '.')} import make\n\n\ndef test_a():\n"
        "    assert make() == 1\n"
    )
    base = repo.commit(files)
    evidence = repo.collect()
    head = repo.commit({path: "def make():\n    x = 1\n    return x\n"})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}

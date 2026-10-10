"""Regression scenarios for the fourth audit round (the code-quality pass):
the planner's findings (Q1, Q2).

Each test names the finding it guards and failed before its fix, unless its
docstring says it is a guard.
"""

from __future__ import annotations

import pytest

from diffcone.testing import asv_target, py_target, reason, rules, selected

# A test and a benchmark on the code under test, and a benchmark on nothing
# that changes: a plan selecting all three selected everything.
TARGETS = [
    py_target("tests/test_use.py::test_use", "tests.test_use.test_use"),
    asv_target("bench_use.time_use", "benchmarks.bench_use.time_use"),
    asv_target("bench_other.time_other", "benchmarks.bench_other.time_other"),
]
AFFECTED = {"tests/test_use.py::test_use", "bench_use.time_use"}
RUNNERS = {
    "tests/test_use.py": "from pkg.use import f\n\n\ndef test_use():\n    assert f()\n",
    "benchmarks/bench_use.py": "from pkg.use import f\n\n\ndef time_use():\n    f()\n",
    "benchmarks/bench_other.py": "def time_other():\n    pass\n",
}


# Q1: an edge's detail is a mark (registers, rebinds, writes, mutated_by,
# an install) only on a ``references`` edge. A name match carries the
# unresolved name as its detail, so a bare call of a function named like a
# mark was planned as that mark.


@pytest.mark.parametrize("name", ["rebinds", "registers", "writes", "mutated_by"])
def test_q1_bare_call_named_like_a_mark_is_a_call(repo, name):
    """``f`` calls ``<name>()``, which nothing resolves, so it may be
    ``pkg.lib.<name>``, whose body changes. As ``rebinds`` the name match
    carried only a deletion: test and benchmark were missed. (``registers``,
    ``writes`` and ``mutated_by`` are guards here: from a function the call
    was planned right by luck.)"""
    lib = f"def {name}():\n    return 1\n"
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": lib,
            "pkg/use.py": f"def f():\n    return {name}()\n",
            **RUNNERS,
        }
    )
    head = repo.commit({"pkg/lib.py": lib.replace("1", "2")})
    plan = repo.plan(base, head, TARGETS, source_roots=["."])
    assert selected(plan) == AFFECTED
    assert rules(plan, "tests/test_use.py::test_use") == {"unresolved_name_match"}
    why = reason(plan, "tests/test_use.py::test_use", "unresolved_name_match")
    assert why.changed_symbol == f"pkg.lib.{name}"


@pytest.mark.parametrize("name", ["registers", "rebinds", "writes", "mutated_by"])
def test_q1_module_level_call_named_like_a_mark_runs_at_import(repo, name):
    """``pkg.use`` calls ``<name>()`` at import; ``pkg.lib.<name>`` changes,
    so importing ``pkg.use`` runs different code. As ``registers`` the
    module's call was taken for a registration, which runs nothing new at
    import, and as ``rebinds`` for a rebinding: both missed. (``writes``
    and ``mutated_by`` are guards here.)"""
    lib = f"def {name}():\n    return 1\n"
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": lib,
            "pkg/use.py": (
                f"if {name}() != 1:\n    raise RuntimeError\n\n\ndef f():\n    return 1\n"
            ),
            **RUNNERS,
        }
    )
    head = repo.commit({"pkg/lib.py": lib.replace("1", "2")})
    plan = repo.plan(base, head, TARGETS, source_roots=["."])
    assert selected(plan) == AFFECTED
    assert rules(plan, "bench_use.time_use") == {"unresolved_name_match"}


# What the tests read: MODE, which ``record`` fills in place.
STATE = {
    "pkg/__init__.py": "",
    "pkg/state.py": (
        "MODE = []\n\n\ndef record(value):\n    MODE.append(value)\n\n\n"
        "def current():\n    return list(MODE)\n"
    ),
    "tests/test_mode.py": "from pkg.state import current\n\n\ndef test_mode():\n    current()\n",
    "benchmarks/bench_mode.py": (
        "from pkg.state import current\n\n\ndef time_mode():\n    current()\n"
    ),
    "benchmarks/bench_other.py": "def time_other():\n    pass\n",
}
STATE_TARGETS = [
    py_target("tests/test_mode.py::test_mode", "tests.test_mode.test_mode"),
    asv_target("bench_mode.time_mode", "benchmarks.bench_mode.time_mode"),
    asv_target("bench_other.time_other", "benchmarks.bench_other.time_other"),
]


def test_q1_import_time_call_named_mutated_by_reaches_the_state(repo):
    """``pkg.config`` calls ``mutated_by("slow")`` at import, a name nothing
    resolves that may be ``pkg.state.mutated_by``, which writes ``MODE`` in
    place: ``"slow"`` -> ``"fast"`` changes what ``MODE`` holds. The name
    match (detail ``mutated_by``) was taken for a write in place, no call,
    so the readers of ``MODE`` were missed."""
    state = STATE["pkg/state.py"].replace("def record(", "def mutated_by(")
    config = 'mutated_by("slow")\n'
    base = repo.commit({**STATE, "pkg/state.py": state, "pkg/config.py": config})
    head = repo.commit({"pkg/config.py": config.replace("slow", "fast")})
    plan = repo.plan(base, head, STATE_TARGETS, source_roots=["."])
    assert selected(plan) == {"tests/test_mode.py::test_mode", "bench_mode.time_mode"}
    assert rules(plan, "tests/test_mode.py::test_mode") == {"unresolved_name_match"}


def test_q1_caller_of_a_writer_named_mutated_by_reaches_the_state(repo):
    """``pkg.config.setup`` calls ``mutated_by("slow")`` (unresolved, may be
    the writer of ``MODE``) and changes its argument: whatever runs it
    leaves ``MODE`` holding something else. The name match was no call, so
    ``calls:`` of the writer did not depend on ``setup``: missed."""
    state = STATE["pkg/state.py"].replace("def record(", "def mutated_by(")
    config = 'def setup():\n    mutated_by("slow")\n'
    base = repo.commit({**STATE, "pkg/state.py": state, "pkg/config.py": config})
    head = repo.commit({"pkg/config.py": config.replace("slow", "fast")})
    plan = repo.plan(base, head, STATE_TARGETS, source_roots=["."])
    assert selected(plan) == {"tests/test_mode.py::test_mode", "bench_mode.time_mode"}


# Q2: a dynamic reference that imports or runs code named at run time can
# reach any module; static planning read that off the word "import" in its
# detail, which ``runpy.run_module``, ``runpy.run_path`` and ``python -m
# <parameter>`` do not have, and bounded them by their module's imports.

RUNPY = "import os\nimport runpy\n\n\ndef f():\n"
DYNAMIC_RUNS = {
    "run_module": RUNPY + "    return runpy.run_module(os.environ['M'])\n",
    "run_module discarded": RUNPY + "    runpy.run_module(os.environ['M'])\n    return 1\n",
    "run_path": RUNPY + "    return runpy.run_path(os.environ['P'])\n",
    "python -m parameter": (
        "import os\nimport subprocess\nimport sys\n\n\n"
        "def launch(name):\n    subprocess.run([sys.executable, '-m', name])\n\n\n"
        "def f():\n    launch(os.environ['M'])\n    return 1\n"
    ),
}


@pytest.mark.parametrize("case", list(DYNAMIC_RUNS))
def test_q2_dynamic_run_of_a_module_named_at_run_time_is_unbounded(repo, case):
    """``f`` imports or runs a module named at run time, so a change in a
    module nothing it imports reaches (``pkg.other``) may run under it."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/use.py": DYNAMIC_RUNS[case],
            "pkg/other.py": "def g():\n    return 1\n",
            **RUNNERS,
        }
    )
    head = repo.commit({"pkg/other.py": "def g():\n    return 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["."])
    assert selected(plan) == AFFECTED
    assert rules(plan, "tests/test_use.py::test_use") == {"dynamic_reference"}
    why = reason(plan, "tests/test_use.py::test_use", "dynamic_reference")
    assert "named at runtime" in why.detail


def test_q2_one_classifier_of_dynamic_details():
    """Every detail the indexer gives a dynamic reference that imports or
    runs code named at run time reads as one; a lookup does not."""
    from diffcone.model import DISCARDED_IMPORT, RUNS_PROGRAM, RUNS_SCRIPT, imports_anything

    for detail in (
        "importlib.import_module(<non-literal>)",
        "__import__(<non-literal>)" + DISCARDED_IMPORT,
        "runpy.run_module(<non-literal>)",
        "runpy.run_module(<non-literal>)" + DISCARDED_IMPORT,
        "runpy.run_path()",
        f"{RUNS_PROGRAM}: an import of anything",
        f"{RUNS_PROGRAM} (python -m <parameter>)",
        f"{RUNS_SCRIPT}scripts/x.py, a Python file no module name maps to",
        "exec() of code read at run time: an import of anything",
        "pickle.loads(): an import of what the data names",
    ):
        assert imports_anything(detail), detail
    for detail in ("getattr(<non-literal>)", "eval()", "exec()", "globals()", "vars()"):
        assert not imports_anything(detail), detail


# Quality: reading diffcone.toml and the manifest.

TREE_AB = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def f():\n    return 1\n",
    "pkg/b.py": "def g():\n    return 1\n",
    "tests/test_a.py": "from pkg.a import f\n\n\ndef test_a():\n    f()\n",
    "benchmarks/bench_b.py": "from pkg.b import g\n\n\ndef time_b():\n    g()\n",
}
TARGETS_AB = [
    py_target("tests/test_a.py::test_a", "tests.test_a.test_a"),
    asv_target("bench_b.time_b", "benchmarks.bench_b.time_b"),
]


def test_unmerged_declaration_file_is_an_analysis_error(repo):
    """A ``diffcone.toml`` the index holds only unmerged (a conflicted merge)
    could not be read, which was taken for no file: its declarations
    silently did nothing. It is an analysis error now, so everything runs."""
    toml = '[[edges]]\nfrom = "pkg.a.f"\nto = "pkg.b.g"\nwhy = "{}"\n'
    base = repo.commit({**TREE_AB, "diffcone.toml": toml.format("one")})
    repo.git("checkout", "-q", "-b", "other")
    repo.commit({"diffcone.toml": toml.format("two")})
    repo.git("checkout", "-q", "main")
    repo.commit({"diffcone.toml": toml.format("three")})
    assert repo.try_git("merge", "-q", "other").returncode != 0
    plan = repo.plan(base, "INDEX", TARGETS_AB, source_roots=["."])
    assert [e.path for e in plan.errors] == ["diffcone.toml"]
    assert selected(plan) == {t.runner_id for t in TARGETS_AB}


def test_manifest_that_is_not_json_says_so(tmp_path):
    from diffcone.manifest import ManifestError, load_manifest

    path = tmp_path / "targets.json"
    path.write_text("{not json")
    with pytest.raises(ManifestError, match="is not valid JSON"):
        load_manifest(path)


# Q2 in discovery: code pytest runs while collecting that imports or runs a
# module named at run time may run any code writing process-global state.

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

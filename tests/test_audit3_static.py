"""Regression scenarios for the third audit round (internal/audit.md, round
3): static planning and snapshots (PLN-4, PLN-6, IDX-4, IDX-6, IDX-7, IDX-8,
the static half of EVP-6).

Each test names the finding it guards and failed before its fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from diffcone.execution import worktree_mismatch
from diffcone.manifest import Target
from diffcone.snapshot import read_snapshot
from diffcone.testing import (
    FixtureRepo,
    asv_target,
    changes,
    path_ids,
    py_target,
    reason,
    rules,
    selected,
)

# A small project: one test and one benchmark on separate code, so a plan
# that selects both selected everything.
TREE = {
    "src/pkg/__init__.py": "",
    "src/pkg/ops.py": "def add(a, b):\n    return a + b\n",
    "src/pkg/other.py": "def helper():\n    return 1\n",
    "tests/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    ),
    "benchmarks/bench_other.py": (
        "from pkg.other import helper\n\n\ndef time_helper():\n    helper()\n"
    ),
}
TARGETS = [
    py_target("tests/test_ops.py::test_add", "tests.test_ops.test_add"),
    asv_target("bench_other.time_helper", "benchmarks.bench_other.time_helper"),
]
EVERYTHING = {t.runner_id for t in TARGETS}
# Prefixed, so modules keep the names they have under the root ".".
SPLIT_ROOTS = ["src", "tests=tests", "benchmarks=benchmarks"]


# PLN-4: dependency, build and runner files a change to which selects
# everything. Outside the roots (src, tests, benchmarks) nothing else sees them.


@pytest.mark.parametrize(
    "path",
    [
        "dev-requirements.txt",
        "test-requirements.txt",
        "Requirements.txt",
        "requirements.pip",
        "ci/deps/actions-311.yaml",
        "hatch.toml",
        "poetry.toml",
        "pdm.toml",
        ".env",
        "zzz.pth",
        "sitecustomize.py",
        "noxfile.py",
        "toxfile.py",
        ".github/workflows/test.yml",
        "mypy.ini",
        "pyrightconfig.json",
    ],
)
def test_pln4_dependency_and_runner_files_outside_the_roots(repo, path):
    base = repo.commit({**TREE, path: "a = 1\n"})
    head = repo.commit({path: "a = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=SPLIT_ROOTS)
    assert not plan.degraded
    assert selected(plan) == EVERYTHING
    assert rules(plan, "tests/test_ops.py::test_add") == {"unanalysed_file_changed"}
    assert path in reason(plan, "bench_other.time_helper", "unanalysed_file_changed").detail


@pytest.mark.parametrize(
    "path",
    [
        "packages/api/setup.py",
        "src/pkg/sub/setup.py",
        "noxfile.py",
        "toxfile.py",
        "sitecustomize.py",
        "src/usercustomize.py",
        "packages/api/pdm_build.py",
    ],
)
def test_pln4_python_build_and_startup_scripts_under_a_root(repo, path):
    """Under a root a build script is an indexed module nobody imports: its
    change used to reach nothing unless it sat at the repository root."""
    base = repo.commit({**TREE, path: "x = 1\n"})
    head = repo.commit({path: "x = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["src", "."])
    assert selected(plan) == EVERYTHING
    assert rules(plan, "tests/test_ops.py::test_add") == {"unanalysed_file_changed"}


def test_pln4_build_py_counts_beside_a_project_file(repo):
    """``build.py`` is a build script beside a ``pyproject.toml`` (poetry's
    ``build`` setting); elsewhere it is an ordinary module, planned as one."""
    tree = {
        **TREE,
        "packages/api/pyproject.toml": "[project]\nname = 'api'\n",
        "packages/api/build.py": "x = 1\n",
        "src/pkg/build.py": "def build():\n    return 1\n",
    }
    base = repo.commit(tree)
    head = repo.commit({"packages/api/build.py": "x = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["src", "."])
    assert selected(plan) == EVERYTHING
    head2 = repo.commit({"src/pkg/build.py": "def build():\n    return 2\n"})
    plan = repo.plan(head, head2, TARGETS, source_roots=["src", "."])
    assert selected(plan) == set()


def test_pln4_lock_file_between_index_and_worktree(repo):
    """Neither side committed: the runner files outside the roots were not
    compared at all."""
    repo.commit({**TREE, "uv.lock": "v1\n"})
    (repo.path / "uv.lock").write_text("v2\n")
    plan = repo.plan("INDEX", "WORKTREE", TARGETS, source_roots=SPLIT_ROOTS)
    assert selected(plan) == EVERYTHING
    assert (
        "uv.lock" in reason(plan, "tests/test_ops.py::test_add", "unanalysed_file_changed").detail
    )
    # Staged, it is the same on both sides.
    repo.git("add", "uv.lock")
    plan = repo.plan("INDEX", "WORKTREE", TARGETS, source_roots=SPLIT_ROOTS)
    assert selected(plan) == set()


@pytest.mark.parametrize("path", ["packages/my-api/setup.py", "my-tools/noxfile.py"])
def test_pln4_build_script_no_module_name_maps_to(repo, path):
    """A build script in a directory that is no package name was neither
    indexed nor counted as a file the index does not read."""
    base = repo.commit({**TREE, path: "x = 1\n"})
    head = repo.commit({path: "x = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["src", "."])
    assert selected(plan) == EVERYTHING
    assert path in reason(plan, "tests/test_ops.py::test_add", "unanalysed_file_changed").detail


# PLN-6: an always_run entry naming a runner that does not exist.

ALWAYS_TREE = {
    **TREE,
    "tests/e2e/test_flow.py": "def test_flow():\n    pass\n",
}
ALWAYS_TARGETS = [
    *TARGETS,
    py_target("tests/e2e/test_flow.py::test_flow", "tests.e2e.test_flow.test_flow"),
]


def test_pln6_always_run_with_an_unknown_runner_is_an_analysis_error(repo):
    base = repo.commit(ALWAYS_TREE)
    head = repo.commit(
        {
            "diffcone.toml": '[[always_run]]\ntargets = "tests/e2e/*"\nrunner = "pyest"\n',
            "src/pkg/ops.py": "def add(a, b):\n    return b + a\n",
        }
    )
    plan = repo.plan(base, head, ALWAYS_TARGETS, source_roots=SPLIT_ROOTS)
    assert plan.degraded
    assert [e.path for e in plan.errors] == ["diffcone.toml"]
    assert "'pyest'" in plan.errors[0].message
    assert selected(plan) == {t.runner_id for t in ALWAYS_TARGETS}


def test_pln6_always_run_runner_of_a_manifest_target_or_absent_from_the_plan(repo):
    """A runner label a manifest target uses is a runner; so is ``asv`` in a
    job that plans no benchmarks (one file serves every job)."""
    targets = [
        *(t for t in ALWAYS_TARGETS if t.runner != "asv"),
        Target("nox", "lint", "tests.e2e.test_flow.test_flow", ()),
    ]
    base = repo.commit(ALWAYS_TREE)
    head = repo.commit(
        {
            "diffcone.toml": (
                '[[always_run]]\ntargets = "lint"\nrunner = "nox"\n\n'
                '[[always_run]]\ntargets = "*"\nrunner = "asv"\n'
            ),
            "src/pkg/ops.py": "def add(a, b):\n    return b + a\n",
        }
    )
    plan = repo.plan(base, head, targets, source_roots=SPLIT_ROOTS)
    assert not plan.degraded
    assert selected(plan) == {"tests/test_ops.py::test_add", "lint"}
    assert {a.runner: n for a, n in plan.always_run_matched.items()} == {"nox": 1, "asv": 0}


# IDX-4: PEP 695/696 type parameters.

TYPE_PARAM_CASES = [
    (
        "def entry[T: int]():\n    return entry.__type_params__[0].__bound__\n",
        "def entry[T: str]():\n    return entry.__type_params__[0].__bound__\n",
    ),
    (
        "def entry[T = int]():\n    return entry.__type_params__[0].__default__\n",
        "def entry[T = str]():\n    return entry.__type_params__[0].__default__\n",
    ),
    (
        "def entry[T: (int, str)]():\n    return entry.__type_params__[0].__constraints__\n",
        "def entry[T: (int, bytes)]():\n    return entry.__type_params__[0].__constraints__\n",
    ),
    (
        "def entry[T]():\n    return len(entry.__type_params__)\n",
        "def entry[T, U]():\n    return len(entry.__type_params__)\n",
    ),
    (
        "class C[T: int]:\n    pass\n\n\ndef entry():\n    return C.__type_params__[0].__bound__\n",
        "class C[T: str]:\n    pass\n\n\ndef entry():\n    return C.__type_params__[0].__bound__\n",
    ),
    (
        "class C[T]:\n    pass\n\n\ndef entry():\n    return len(C.__type_params__)\n",
        "class C[T, U]:\n    pass\n\n\ndef entry():\n    return len(C.__type_params__)\n",
    ),
    (
        "type A[T: int] = list[T]\n\n\ndef entry():\n    return A.__type_params__[0].__bound__\n",
        "type A[T: str] = list[T]\n\n\ndef entry():\n    return A.__type_params__[0].__bound__\n",
    ),
]


@pytest.mark.skipif(
    not hasattr(__import__("ast"), "TypeAlias"), reason="PEP 695 syntax needs Python 3.12"
)
@pytest.mark.parametrize("before, after", TYPE_PARAM_CASES)
def test_idx4_type_parameter_change_is_a_change(repo, before, after):
    base = repo.commit(
        {
            **TREE,
            "src/pkg/core.py": before,
            "tests/test_core.py": (
                "from pkg.core import entry\n\n\ndef test_core():\n    assert entry()\n"
            ),
        }
    )
    head = repo.commit({"src/pkg/core.py": after})
    targets = [*TARGETS, py_target("tests/test_core.py::test_core", "tests.test_core.test_core")]
    plan = repo.plan(base, head, targets, source_roots=SPLIT_ROOTS)
    assert changes(plan), "no changed symbol"
    assert selected(plan) == {"tests/test_core.py::test_core"}


@pytest.mark.skipif(
    not hasattr(__import__("ast"), "TypeAlias"), reason="PEP 695 syntax needs Python 3.12"
)
@pytest.mark.parametrize(
    "core",
    [
        "from pkg.make import make\n\n\ndef entry[T: make]():\n"
        "    return entry.__type_params__[0].__bound__()\n",
        "from pkg.make import make\n\n\ndef entry[T = make]():\n"
        "    return entry.__type_params__[0].__default__()\n",
        "from pkg.make import make\n\n\nclass C[T: make]:\n    pass\n\n\n"
        # (``C.__type_params__`` alone is no reference to C: an attribute
        # nothing defines, read off a class, is only a name match.)
        "def entry():\n    cls = C\n    return cls.__type_params__[0].__bound__()\n",
    ],
)
def test_idx4_type_parameter_bound_is_a_dependency(repo, core):
    """A bound or default naming project code depends on it, though Python
    evaluates it only when ``__bound__``/``__default__`` is read."""
    base = repo.commit(
        {
            **TREE,
            "src/pkg/make.py": "def make():\n    return 1\n",
            "src/pkg/core.py": core,
            "tests/test_core.py": (
                "from pkg.core import entry\n\n\ndef test_core():\n    assert entry()\n"
            ),
        }
    )
    head = repo.commit({"src/pkg/make.py": "def make():\n    return 2\n"})
    targets = [*TARGETS, py_target("tests/test_core.py::test_core", "tests.test_core.test_core")]
    plan = repo.plan(base, head, targets, source_roots=SPLIT_ROOTS)
    assert selected(plan) == {"tests/test_core.py::test_core"}
    assert "pkg.make.make" in path_ids(reason(plan, "tests/test_core.py::test_core"))


# IDX-6: files flagged assume-unchanged or skip-worktree, edited on disk.

DATA_TREE: dict[str, str | bytes | None] = {
    "pkg/__init__.py": "",
    "pkg/data.json": '{"v": 1}\n',
    "pkg/a.py": (
        "import json, pathlib\n\n\ndef load():\n"
        "    return json.loads((pathlib.Path(__file__).parent / 'data.json').read_text())\n"
    ),
    "tests/test_a.py": "from pkg.a import load\n\n\ndef test_a():\n    assert load()['v'] == 1\n",
    "benchmarks/bench_x.py": "def time_x():\n    pass\n",
}
DATA_TARGETS = [
    py_target("t::test_a", "tests.test_a.test_a"),
    asv_target("bench_x.time_x", "benchmarks.bench_x.time_x"),
]


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_idx6_flagged_data_file_edited_on_disk(repo, flag):
    repo.commit(DATA_TREE)
    repo.git("update-index", flag, "pkg/data.json")
    plan = repo.plan("HEAD", "WORKTREE", DATA_TARGETS)
    assert selected(plan) == set()
    (repo.path / "pkg/data.json").write_text('{"v": 2}\n')
    plan = repo.plan("HEAD", "WORKTREE", DATA_TARGETS)
    assert selected(plan) == {"t::test_a", "bench_x.time_x"}
    assert rules(plan, "t::test_a") == {"unanalysed_file_changed"}


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_idx6_flagged_runner_file_outside_the_roots(repo, flag):
    repo.commit({**TREE, "requirements.txt": "numpy\n"})
    repo.git("update-index", flag, "requirements.txt")
    (repo.path / "requirements.txt").write_text("numpy<2\n")
    plan = repo.plan("HEAD", "WORKTREE", TARGETS, source_roots=SPLIT_ROOTS)
    assert selected(plan) == EVERYTHING


def test_idx6_run_sees_a_flagged_python_file_edited_on_disk(repo):
    """``run`` refuses to execute a tree other than the one planned; git diff
    does not look at a flagged file, so the check let it through."""
    head = repo.commit(TREE)
    repo.git("update-index", "--assume-unchanged", "src/pkg/ops.py")
    plan = repo.plan("HEAD", head, TARGETS, source_roots=SPLIT_ROOTS)
    assert worktree_mismatch(repo.path, plan) is None
    (repo.path / "src/pkg/ops.py").write_text("def add(a, b):\n    return a - b\n")
    assert "src/pkg/ops.py" in (worktree_mismatch(repo.path, plan) or "")


# IDX-7: a submodule under the roots.


def _with_submodule(tmp_path: Path) -> tuple[FixtureRepo, FixtureRepo]:
    sub = FixtureRepo(tmp_path / "sub")
    sub.commit({"lib.py": "def f():\n    return 1\n"})
    repo = FixtureRepo(tmp_path / "repo", check_cache=True)
    repo.commit(DATA_TREE)
    repo.git(
        "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub.path), "vendor/sub"
    )
    repo.commit({})
    return repo, sub


def test_idx7_clean_worktree_with_a_submodule_selects_nothing(tmp_path):
    repo, _ = _with_submodule(tmp_path)
    for base, head in (("HEAD", "WORKTREE"), ("INDEX", "WORKTREE"), ("HEAD", "INDEX")):
        plan = repo.plan(base, head, DATA_TARGETS)
        assert selected(plan) == set(), (base, head, [f.detail for f in plan.fallbacks])


def test_idx7_submodule_at_another_commit_is_a_change(tmp_path):
    repo, sub = _with_submodule(tmp_path)
    checkout = repo.path / "vendor/sub"
    (checkout / "lib.py").write_text("def f():\n    return 2\n")
    # Uncommitted edits inside the submodule change what runs.
    plan = repo.plan("HEAD", "WORKTREE", DATA_TARGETS)
    assert selected(plan) == {"t::test_a", "bench_x.time_x"}
    FixtureRepo(checkout).commit({})
    plan = repo.plan("HEAD", "WORKTREE", DATA_TARGETS)
    assert selected(plan) == {"t::test_a", "bench_x.time_x"}
    assert "vendor/sub" in reason(plan, "t::test_a", "unanalysed_file_changed").detail


# IDX-8: odd paths.


def test_idx8_python_path_with_a_line_break(repo):
    """``cat-file --batch`` reads one name per line: the plan failed with
    "cannot read". The text file (read for doctests) is a file the index
    does not read, so its addition selects everything."""
    base = repo.commit(DATA_TREE)
    head = repo.commit(
        {"pkg/we\nird.py": "x = 1\n", "pkg/sp ace.py": "x = 1\n", "pkg/da\nta.txt": "x\n"}
    )
    for rev in (head, "INDEX"):
        plan = repo.plan(base, rev, DATA_TARGETS[1:], discover_runners=["pytest"])
        assert not plan.degraded, plan.errors
        assert selected(plan) == {"tests/test_a.py::test_a", "bench_x.time_x"}
        assert rules(plan, "bench_x.time_x") == {"unanalysed_file_changed"}


def test_idx8_non_ascii_test_path_is_listed_for_discovery(repo):
    """Without ``-z`` git quotes a non-ASCII name (``"tests/test_\\303\\251.py"``):
    the commit snapshot's list of Python files lost it."""
    repo.commit({**DATA_TREE, "tests/test_é.py": "def test_e():\n    pass\n"})
    for rev in ("HEAD", "INDEX", "WORKTREE"):
        snapshot = read_snapshot(repo.path, rev, ["pkg"], with_config=True)
        assert "tests/test_é.py" in snapshot.python_paths, rev


def test_idx8_run_sees_a_non_ascii_python_file_edited_on_disk(repo):
    head = repo.commit({**TREE, "src/pkg/é.py": "x = 1\n"})
    plan = repo.plan("HEAD", head, TARGETS, source_roots=SPLIT_ROOTS)
    assert worktree_mismatch(repo.path, plan) is None
    (repo.path / "src/pkg/é.py").write_text("x = 2\n")
    assert "src/pkg/é.py" in (worktree_mismatch(repo.path, plan) or "")


# EVP-6 (static): an import-time call whose arguments change.

STATE = (
    'MODE = ["slow"]\n\n\n'
    "def set_mode(m):\n    _store(m)\n    return m\n\n\n"
    "def _store(m):\n    MODE[0] = m\n\n\n"
    "def mode():\n    return MODE[0]\n"
)
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


def test_evp6_module_seed_is_explained_by_the_change_it_runs(repo):
    """A target reached through its module's import of a changed variable
    was explained as a dynamic reference with no detail."""
    sites = CALL_SITES["library variable"]
    base = repo.commit({**READERS, **sites})
    head = repo.commit({"pkg/config.py": sites["pkg/config.py"].replace("slow", "fast")})
    targets = [py_target("tests/test_a.py::test_a", "tests.test_a.test_a")]
    plan = repo.plan(base, head, targets, source_roots=["."])
    assert rules(plan, "tests/test_a.py::test_a") == {"dependency"}
    why = reason(plan, "tests/test_a.py::test_a")
    assert path_ids(why)[-2:] == ["pkg.config", "pkg.config.X"]
    assert why.path[-1].kind == "runs_at_import"
    assert why.changed_symbol == "pkg.config.X"

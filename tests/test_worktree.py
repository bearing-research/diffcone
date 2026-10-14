"""INDEX and WORKTREE snapshots: uncommitted state as the head of a plan."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from diffcone.cli import main
from diffcone.report import to_dict, to_text
from diffcone.snapshot import INDEX, WORKTREE, read_snapshot
from diffcone.testing import (
    FixtureRepo,
    asv_target,
    py_target,
    reason,
    rules,
    selected,
    unselected,
)

OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_OPS = (
    "from pkg.ops import add, mul\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)


def dirty_repo(repo):
    """Commit a base, then leave the working tree in a mixed state."""
    base = repo.commit(
        {
            ".gitignore": "ignored_*.py\nbuild/\n",
            "pkg/ops.py": OPS,
            "pkg/gone.py": "def gone():\n    return 0\n",
            "pkg/staged.py": "def staged():\n    return 1\n",
            "tests/test_ops.py": TEST_OPS,
            "tests/test_gone.py": (
                "from pkg.gone import gone\n\n\ndef test_gone():\n    assert gone() == 0\n"
            ),
            "pytest.ini": "[pytest]\n",
        }
    )
    # unstaged modification
    (repo.path / "pkg/ops.py").write_bytes((OPS.replace("a + b", "b + a")).encode())
    # staged modification (then a further unstaged edit on top)
    (repo.path / "pkg/staged.py").write_bytes(b"def staged():\n    return 2\n")
    repo.git("add", "pkg/staged.py")
    (repo.path / "pkg/staged.py").write_bytes(b"def staged():\n    return 3\n")
    # untracked test file, ignored file, deleted tracked file, changed config
    (repo.path / "tests/test_new.py").write_text(
        "from pkg.ops import mul\n\n\ndef test_new():\n    assert mul(1, 1) == 1\n"
    )
    (repo.path / "pkg/ignored_scratch.py").write_bytes(b"def scratch():\n    pass\n")
    # An ignored directory (a build tree, a virtualenv) is skipped whole.
    (repo.path / "build/lib").mkdir(parents=True)
    (repo.path / "build/lib/built.py").write_bytes(b"def built():\n    pass\n")
    (repo.path / "pkg/gone.py").unlink()
    (repo.path / "pytest.ini").write_bytes(b"[pytest]\npython_files = test_*.py\n")
    return base


def test_worktree_snapshot_contents(repo):
    base = dirty_repo(repo)
    snap = read_snapshot(repo.path, "WORKTREE", ["."], with_config=True)
    assert snap.kind == "worktree" and snap.revision == "WORKTREE" and snap.commit == base
    assert "uncommitted" in snap.description
    assert set(snap.files) == {
        "pkg/ops.py",
        "pkg/staged.py",
        "tests/test_ops.py",
        "tests/test_gone.py",
        "tests/test_new.py",  # untracked, not ignored
        "pkg/ignored_scratch.py",  # ignored, but beside tracked code: what runs imports it
    }
    assert "build/lib/built.py" not in snap.python_paths  # under an ignored directory
    assert b"b + a" in snap.files["pkg/ops.py"]
    assert b"return 3" in snap.files["pkg/staged.py"]  # disk content, not the index
    assert snap.config_files["pytest.ini"] == b"[pytest]\npython_files = test_*.py\n"

    # Source-root pathspecs apply to the working tree as well.
    scoped = read_snapshot(repo.path, "WORKTREE", ["tests"])
    assert set(scoped.files) == {"tests/test_ops.py", "tests/test_gone.py", "tests/test_new.py"}


def test_index_snapshot_contents(repo):
    base = dirty_repo(repo)
    snap = read_snapshot(repo.path, "INDEX", ["."], with_config=True)
    assert snap.kind == "index" and snap.commit == base
    assert set(snap.files) == {
        "pkg/ops.py",
        "pkg/gone.py",  # still in the index although deleted on disk
        "pkg/staged.py",
        "tests/test_ops.py",
        "tests/test_gone.py",
    }
    assert b"a + b" in snap.files["pkg/ops.py"]  # unstaged edit not included
    assert b"return 2" in snap.files["pkg/staged.py"]  # staged content
    assert snap.config_files["pytest.ini"] == b"[pytest]\n"  # index, not disk


def test_plan_against_worktree_and_index(repo):
    base = dirty_repo(repo)
    targets = [
        py_target("t::test_add", "tests.test_ops.test_add"),
        py_target("t::test_mul", "tests.test_ops.test_mul"),
        py_target("t::test_gone", "tests.test_gone.test_gone"),
        py_target("t::test_new", "tests.test_new.test_new"),
    ]
    # The edited pytest.ini is a file the analysis does not read: everything.
    plan = repo.plan(base, "WORKTREE", targets)
    assert selected(plan) == {"t::test_add", "t::test_mul", "t::test_gone", "t::test_new"}
    assert "pytest.ini" in reason(plan, "t::test_mul", "unanalysed_file_changed").detail
    # Put it back to see the Python changes alone.
    (repo.path / "pytest.ini").write_bytes(b"[pytest]\n")
    plan = repo.plan(base, "WORKTREE", targets)
    assert plan.uncommitted_analyzed
    assert {c.id: c.changes for c in plan.changes} == {
        "pkg.ops.add": ("body_changed",),
        "pkg.staged.staged": ("body_changed",),
        "pkg.gone": ("deleted",),
        "pkg.gone.gone": ("deleted",),
        "pkg.ignored_scratch": ("added",),
        "pkg.ignored_scratch.scratch": ("added",),
        "tests.test_gone": ("dependencies_changed",),
        "tests.test_gone.test_gone": ("dependencies_changed",),
        "tests.test_new": ("added",),
        "tests.test_new.test_new": ("added",),
    }
    assert selected(plan) == {"t::test_add", "t::test_gone", "t::test_new"}
    assert unselected(plan) == {"t::test_mul"}
    assert reason(plan, "t::test_new").changes == ("added",)

    report = to_dict(plan)
    assert report["analysis"]["head"] == {
        "revision": "WORKTREE",
        "commit": base,
        "kind": "worktree",
        "uncommitted": True,
        "description": plan.head.description,
        "excluded": [],
    }
    assert report["analysis"]["scope"]["analyzed"].startswith("UNCOMMITTED state was analyzed")
    assert report["analysis"]["base"]["kind"] == "commit"
    assert report["analysis"]["working_tree_analyzed"] is True
    assert report["analysis"]["uncommitted_analyzed"] is True
    text = to_text(plan)
    assert "scope: UNCOMMITTED state was analyzed as head" in text
    assert "head: working tree" in text

    index_plan = repo.plan(base, "INDEX", targets)
    assert {c.id for c in index_plan.changes} == {"pkg.staged.staged"}
    # The untracked test is in neither snapshot: unresolved entry, conservative.
    assert selected(index_plan) == {"t::test_new"}
    assert reason(index_plan, "t::test_new", "entry_symbol_unresolved")
    assert to_dict(index_plan)["analysis"]["working_tree_analyzed"] is False
    assert to_dict(index_plan)["analysis"]["uncommitted_analyzed"] is True


def test_committed_plan_still_says_so(repo):
    base = repo.commit({"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [py_target("t::test_add", "tests.test_ops.test_add")])
    assert not plan.uncommitted_analyzed
    report = to_dict(plan)
    assert report["analysis"]["working_tree_analyzed"] is False
    assert report["analysis"]["uncommitted_analyzed"] is False
    assert "the working tree was not analyzed" in to_text(plan)


def test_cli_worktree_with_discovery(repo, capsys):
    base = dirty_repo(repo)
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            "WORKTREE",
            "--discover",
            "pytest",
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["analysis"]["head"]["kind"] == "worktree"
    # Discovery ran on the working tree: the untracked test exists, pytest.ini is the disk one.
    assert report["discovery"][0]["config"]["source"] == "pytest.ini"
    assert report["discovery"][0]["config"]["python_files"] == ["test_*.py"]
    # The edited pytest.ini is a file the analysis does not read: everything.
    assert {t["runner_id"] for t in report["selected_targets"]} == {
        "tests/test_ops.py::test_add",
        "tests/test_ops.py::test_mul",
        "tests/test_gone.py::test_gone",
        "tests/test_new.py::test_new",
    }
    assert [f["rule"] for f in report["fallback_decisions"]] == ["unanalysed_file_changed"]

    code = main(["discover", "--repo", str(repo.path), "--rev", "WORKTREE", "--discover", "pytest"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["discovery"]["revision"] == "WORKTREE"
    assert "tests/test_new.py::test_new" in {t["runner_id"] for t in data["targets"]}


# --- regression tests added after code review ------------------------------


def test_uncommitted_base_snapshot(repo):
    base = dirty_repo(repo)
    targets = [
        py_target("t::test_add", "tests.test_ops.test_add"),
        py_target("t::test_new", "tests.test_new.test_new"),
    ]
    # Inverted direction: the working tree is the base, HEAD is the head.
    plan = repo.plan("WORKTREE", base, targets)
    assert plan.base.kind == "worktree" and plan.head.kind == "commit"
    assert plan.uncommitted_analyzed and plan.working_tree_analyzed
    assert {
        c.id: c.changes for c in plan.changes if c.id.startswith(("pkg.gone", "tests.test_new"))
    } == {
        "pkg.gone": ("added",),
        "pkg.gone.gone": ("added",),
        "tests.test_new": ("deleted",),
        "tests.test_new.test_new": ("deleted",),
    }
    assert selected(plan) == {"t::test_add", "t::test_new"}
    report = to_dict(plan)
    assert report["analysis"]["base"]["kind"] == "worktree"
    assert report["analysis"]["working_tree_analyzed"] is True
    assert report["analysis"]["scope"]["analyzed"] == (
        "UNCOMMITTED state was analyzed as base; see base/head"
    )
    assert "scope: UNCOMMITTED state was analyzed as base" in to_text(plan)

    both = repo.plan("INDEX", "WORKTREE", targets)
    assert to_dict(both)["analysis"]["scope"]["analyzed"] == (
        "UNCOMMITTED state was analyzed as base and head; see base/head"
    )
    assert to_dict(both)["analysis"]["working_tree_analyzed"] is True


def test_cli_discover_index_reports_snapshot_kind(repo, capsys):
    dirty_repo(repo)
    code = main(["discover", "--repo", str(repo.path), "--rev", "INDEX", "--discover", "pytest"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["discovery"]["snapshot"]["kind"] == "index"
    assert data["discovery"]["snapshot"]["uncommitted"] is True
    assert "staged content" in data["discovery"]["snapshot"]["description"]
    # The staged pytest.ini (no python_files override) is the config source.
    assert data["discovery"]["runners"][0]["config"]["source"] == "pytest.ini"
    assert data["discovery"]["runners"][0]["config"]["python_files"] == ["test_*.py", "*_test.py"]
    ids = {t["runner_id"] for t in data["targets"]}
    assert "tests/test_new.py::test_new" not in ids  # untracked: not in the index
    assert "tests/test_gone.py::test_gone" in ids  # deleted on disk, still staged


def test_skip_worktree_entries_are_read_from_the_index(repo):
    base = repo.commit(
        {
            "pkg/sparse.py": "def sparse():\n    return 1\n",
            "pkg/ops.py": OPS,
            "tests/test_sparse.py": (
                "from pkg.sparse import sparse\n\n\ndef test_sparse():\n    assert sparse() == 1\n"
            ),
        }
    )
    # Simulate a sparse checkout: the file is tracked but absent from disk.
    repo.git("update-index", "--skip-worktree", "pkg/sparse.py")
    (repo.path / "pkg/sparse.py").unlink()
    (repo.path / "pkg/ops.py").write_bytes((OPS.replace("a + b", "b + a")).encode())

    snap = read_snapshot(repo.path, "WORKTREE", ["."])
    assert snap.files["pkg/sparse.py"] == b"def sparse():\n    return 1\n"
    assert b"b + a" in snap.files["pkg/ops.py"]

    plan = repo.plan(
        base, "WORKTREE", [py_target("t::test_sparse", "tests.test_sparse.test_sparse")]
    )
    assert {c.id for c in plan.changes} == {"pkg.ops.add"}
    assert unselected(plan) == {"t::test_sparse"}


def test_unmerged_index_degrades_instead_of_failing(repo):
    base = repo.commit({"pkg/m.py": "def f():\n    return 0\n", "tests/test_m.py": TEST_M})
    repo.git("checkout", "-q", "-b", "other")
    repo.commit({"pkg/m.py": "def f():\n    return 1\n"})
    repo.git("checkout", "-q", "main")
    repo.commit({"pkg/m.py": "def f():\n    return 2\n"})
    assert repo.try_git("merge", "other").returncode != 0  # conflict expected

    index_plan = repo.plan(base, "INDEX", [py_target("t::test_f", "tests.test_m.test_f")])
    assert index_plan.degraded
    assert [(e.revision, e.path) for e in index_plan.errors] == [("INDEX", "pkg/m.py")]
    assert "unmerged" in index_plan.errors[0].message
    assert selected(index_plan) == {"t::test_f"}
    assert to_dict(index_plan)["status"] == "degraded"

    # The working tree holds conflict markers: a parse error, also degraded.
    wt_plan = repo.plan(base, "WORKTREE", [py_target("t::test_f", "tests.test_m.test_f")])
    assert wt_plan.degraded
    assert [e.path for e in wt_plan.errors] == ["pkg/m.py"]


TEST_M = "from pkg.m import f\n\n\ndef test_f():\n    assert f() >= 0\n"


# ------------------------------------------------------------- revisions and source roots as given


def test_f9_a_dot_slash_source_root_is_the_same_root():
    from diffcone.snapshot import split_root

    assert split_root("./src") == split_root("src") == ("src", "")


def test_r13_r14_an_unknown_revision_is_named_and_a_shallow_clone_says_so(repo, tmp_path, capsys):
    repo.commit({"lib/__init__.py": "", "lib/m.py": "X = 1\n"})
    code = main(["plan", "--repo", str(repo.path), "--base", "nosuch", "--head", "HEAD",
                 "--discover", "pytest"])  # fmt: skip
    assert code == 2
    assert "'nosuch'" in capsys.readouterr().err
    shallow = tmp_path / "shallow"
    repo.git("clone", "-q", "--depth", "1", f"file://{repo.path}", str(shallow))
    code = main(["plan", "--repo", str(shallow), "--base", "HEAD^1", "--head", "HEAD",
                 "--discover", "pytest"])  # fmt: skip
    assert code == 2
    err = capsys.readouterr().err
    assert "'HEAD^1'" in err and "shallow" in err


# ------------------------------------------------------------ files git does not report as changed


ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'


def test_r15_untracked_bytecode_is_not_a_change(repo):
    from diffcone.execution import _dirty

    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "lib/__init__.py": "",
            "lib/m.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "from lib.m import f\n\n\ndef test_f():\n    f()\n",
            "tests/test_n.py": "def test_n():\n    pass\n",
        }
    )
    (repo.path / "lib" / "__pycache__").mkdir()
    (repo.path / "lib" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0")
    plan = repo.plan(base, "WORKTREE", [], discover_runners=["pytest"])
    assert selected(plan) == set()
    # ASV's output beside its configuration does not make the tree dirty
    # for ``collect`` either.
    (repo.path / "results").mkdir()
    (repo.path / "results" / "benchmarks.json").write_text("{}")
    assert _dirty(repo.path) == []


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


# Files flagged assume-unchanged or skip-worktree, edited on disk.

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


PY = sys.executable
DIFFCONE = shutil.which("diffcone", path=str(Path(sys.executable).parent)) or str(
    Path(sys.executable).parent / "diffcone"
)
PYTEST = f"{PY} -m pytest"
ADD_OPS = "def add(a, b):\n    return a + b\n"
OPS_BROKEN = "def add(a, b):\n    return a + b + 1\n"
TEST_ADD = "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
# ASV benchmarks beside the tests, so every scenario mixes both runners.
BENCHES = {
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": (
        "from pkg.ops import add\n\n\nclass Ops:\n    def time_add(self):\n        add(1, 1)\n"
    ),
}
OPS_BENCH = asv_target("Ops.time_add", "benchmarks.bench.Ops.time_add", "pkg.ops.add")


def _cli(repo: FixtureRepo, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DIFFCONE, *args], cwd=cwd or repo.path, capture_output=True, text=True, check=False
    )


def _write(repo: FixtureRepo, rel: str, content: str) -> None:
    path = repo.path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8", newline="")


def test_r1_ignored_python_file_under_the_roots_is_read_by_worktree_plans(repo):
    """A git-ignored ``.py`` beside tracked code (a ``local_settings.py``, a
    generated ``_version.py``) is what runs on disk: the WORKTREE snapshot
    reads it, a change to it selects its importers, ``run`` executes them,
    and a committed plan refuses to run while it is on disk. An ignored
    directory (a build tree, a virtualenv) is still skipped whole."""
    base = repo.commit(
        {
            ".gitignore": "pkg/local.py\nbuild/\n",
            "pkg/__init__.py": "",
            "pkg/ops.py": ADD_OPS,
            "tests/__init__.py": "",
            "tests/test_ops.py": TEST_ADD,
            "tests/test_local.py": (
                "from pkg.local import limit\n\n\ndef test_local():\n    assert limit() == 1\n"
            ),
            **BENCHES,
            "benchmarks/bench_local.py": (
                "from pkg.local import limit\n\n\nclass Local:\n    def time_limit(self):\n"
                "        limit()\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": OPS_BROKEN})
    _write(repo, "pkg/local.py", "def limit():\n    return 2\n")
    _write(repo, "build/lib/built.py", "def built():\n    return 1\n")
    assert repo.git("status", "--porcelain", "--ignored").splitlines() == [
        "!! build/",
        "!! pkg/local.py",
    ]
    snap = read_snapshot(repo.path, "WORKTREE", ["."], with_config=True)
    assert "pkg/local.py" in snap.files
    assert not any(p.startswith("build/") for p in snap.files)
    assert not any(p.startswith("build/") for p in snap.python_paths)

    benches = [
        asv_target("Local.time_limit", "benchmarks.bench_local.Local.time_limit"),
        OPS_BENCH,
    ]
    plan = repo.plan(head, "WORKTREE", benches, discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_local.py::test_local", "Local.time_limit"}
    # The import that resolved to nothing at HEAD now resolves: a new dependency.
    assert reason(plan, "tests/test_local.py::test_local").changes == ("dependencies_added",)

    # run executes the selected test from the plan of the working tree.
    proc = _cli(
        repo,
        "run",
        "--base",
        head,
        "--head",
        "WORKTREE",
        "--discover",
        "pytest",
        "--command",
        PYTEST,
        "--",
        "-q",
    )
    assert proc.returncode == 1, proc.stderr
    assert "test_local" in proc.stdout and "1 failed, 1 deselected" in proc.stdout

    # A plan of the commit cannot see the file: run refuses while it is there.
    proc = _cli(
        repo,
        "run",
        "--base",
        base,
        "--head",
        "HEAD",
        "--discover",
        "pytest",
        "--command",
        PYTEST,
        "--dry-run",
    )
    assert proc.returncode == 2
    assert "pkg/local.py" in proc.stderr and "--allow-mismatched-worktree" in proc.stderr


# --------------------------------------------------------------------------- odd paths


@pytest.mark.skipif(sys.platform == "win32", reason="Windows allows no line break in a name")
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


# ----------------------------------------------------------- submodules, file modes and file types


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


@pytest.mark.skipif(os.name == "nt", reason="no executable bit")
def test_r9_a_file_mode_change_is_a_change(repo):
    """``chmod +x`` on a script a test runs by path flips the test; the
    mode is part of the file's identity in commits and the working tree,
    so the change selects what a content change would."""
    script = "#!/bin/sh\necho hi\n"
    test = (
        "import pathlib\nimport subprocess\n\n\ndef test_run():\n"
        "    p = pathlib.Path(__file__).parent.parent / 'tools' / 'hello.sh'\n"
        "    assert subprocess.run([str(p)], capture_output=True, text=True).stdout == 'hi\\n'\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": ADD_OPS,
            "tools/hello.sh": script,
            "tools/hello.py": "#!/usr/bin/env python3\nprint('hi')\n",
            "tests/__init__.py": "",
            "tests/test_run.py": test,
            "tests/test_ops.py": TEST_ADD,
            **BENCHES,
        }
    )
    benches = [OPS_BENCH]
    repo.git("update-index", "--chmod=+x", "tools/hello.sh")
    (repo.path / "tools/hello.sh").chmod(0o755)
    head = repo.commit({}, "chmod +x")
    assert repo.git("diff", "--name-only", base, head) == "tools/hello.sh"
    plan = repo.plan(base, head, benches, discover_runners=["pytest"])
    # The index cannot say who runs the script: everything, by the rule
    # for files it does not read.
    assert selected(plan) == {
        "tests/test_run.py::test_run",
        "tests/test_ops.py::test_add",
        "Ops.time_add",
    }
    assert (
        "tools/hello.sh"
        in reason(plan, "tests/test_run.py::test_run", "unanalysed_file_changed").detail
    )
    # The same for a Python file, and on disk (WORKTREE) as well as committed.
    (repo.path / "tools/hello.py").chmod(0o755)
    plan = repo.plan(head, "WORKTREE", benches, discover_runners=["pytest"])
    assert selected(plan) == {
        "tests/test_run.py::test_run",
        "tests/test_ops.py::test_add",
        "Ops.time_add",
    }
    assert "tools/hello.py" in reason(plan, "Ops.time_add", "unanalysed_file_changed").detail
    (repo.path / "tools/hello.py").chmod(0o644)
    assert selected(repo.plan(head, "WORKTREE", benches, discover_runners=["pytest"])) == set()


def test_r10_a_tracked_file_replaced_by_a_directory_on_disk(repo):
    """``git hash-object`` cannot hash a directory: the WORKTREE plan used to
    crash. The entry is a deletion (and the files inside are new untracked
    files), which selects the test reading it."""
    test = (
        "import pathlib\n\n\ndef test_data():\n"
        "    assert (pathlib.Path(__file__).parent / 'data').read_text() == 'x\\n'\n"
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": ADD_OPS,
            "tests/__init__.py": "",
            "tests/data": "x\n",
            "tests/test_data.py": test,
            "tests/test_ops.py": TEST_ADD,
            **BENCHES,
        }
    )
    (repo.path / "tests/data").unlink()
    (repo.path / "tests/data").mkdir()
    (repo.path / "tests/data/x.txt").write_text("y\n", "utf-8")
    plan = repo.plan(base, "WORKTREE", [OPS_BENCH], discover_runners=["pytest"])
    assert selected(plan) == {
        "tests/test_data.py::test_data",
        "tests/test_ops.py::test_add",
        "Ops.time_add",
    }
    detail = reason(plan, "tests/test_data.py::test_data", "unanalysed_file_changed").detail
    assert "tests/data" in detail
    assert not plan.errors


EXCLUDE = '[index]\nexclude = ["tests/data/**"]\n'
INVALID = "def broken(:\n"


def test_e29_each_snapshot_kind_reads_its_own_exclusions(repo):
    """black keeps deliberately invalid ``.py`` files under its test data;
    ``diffcone.toml``'s ``[index] exclude`` reads them as other files, by the
    blob id git gives them, in a commit, the index and the working tree."""
    path = "tests/data/case.py"
    repo.commit({"pkg/__init__.py": "", path: INVALID, "diffcone.toml": EXCLUDE})
    snapshots = [read_snapshot(repo.path, rev, ["."]) for rev in ("HEAD", INDEX, WORKTREE)]
    blob = repo.git("rev-parse", f"HEAD:{path}")
    for snap in snapshots:
        assert path not in snap.files and "pkg/__init__.py" in snap.files
        assert snap.other_files[path] == blob
        assert snap.info.excluded == ("tests/data/**",)
        assert not snap.errors
    # The working tree's own file decides for the working tree.
    (repo.path / "diffcone.toml").write_text("")
    worktree = read_snapshot(repo.path, WORKTREE, ["."])
    assert path in worktree.files and worktree.info.excluded == ()
    assert read_snapshot(repo.path, "HEAD", ["."]).info.excluded == ("tests/data/**",)

"""INDEX and WORKTREE snapshots: uncommitted state as the head of a plan."""

from __future__ import annotations

import json

from diffcone.cli import main
from diffcone.report import to_dict, to_text
from diffcone.snapshot import read_snapshot
from diffcone.testing import py_target, reason, selected, unselected

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
    (repo.path / "pkg/ops.py").write_text(OPS.replace("a + b", "b + a"))
    # staged modification (then a further unstaged edit on top)
    (repo.path / "pkg/staged.py").write_text("def staged():\n    return 2\n")
    repo.git("add", "pkg/staged.py")
    (repo.path / "pkg/staged.py").write_text("def staged():\n    return 3\n")
    # untracked test file, ignored file, deleted tracked file, changed config
    (repo.path / "tests/test_new.py").write_text(
        "from pkg.ops import mul\n\n\ndef test_new():\n    assert mul(1, 1) == 1\n"
    )
    (repo.path / "pkg/ignored_scratch.py").write_text("def scratch():\n    pass\n")
    (repo.path / "pkg/gone.py").unlink()
    (repo.path / "pytest.ini").write_text("[pytest]\npython_files = test_*.py\n")
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
    }
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
    (repo.path / "pytest.ini").write_text("[pytest]\n")
    plan = repo.plan(base, "WORKTREE", targets)
    assert plan.uncommitted_analyzed
    assert {c.id: c.changes for c in plan.changes} == {
        "pkg.ops.add": ("body_changed",),
        "pkg.staged.staged": ("body_changed",),
        "pkg.gone": ("deleted",),
        "pkg.gone.gone": ("deleted",),
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
    (repo.path / "pkg/ops.py").write_text(OPS.replace("a + b", "b + a"))

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

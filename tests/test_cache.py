"""The per-commit index cache must be invisible: same plan, fewer git calls."""

from __future__ import annotations

import json

from diffcone import cache as cache_mod
from diffcone import planner as planner_mod
from diffcone.cache import IndexCache, index_from_dict, index_to_dict
from diffcone.cli import main
from diffcone.report import to_dict
from diffcone.testing import py_target

OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_OPS = (
    "from pkg.ops import add, mul\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)


def _plans_equal(a, b) -> bool:
    da, db = to_dict(a), to_dict(b)
    da["analysis"].pop("repo"), db["analysis"].pop("repo")
    return json.dumps(da, sort_keys=True) == json.dumps(db, sort_keys=True)


def test_index_round_trips_through_json(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "pkg/bad.py": "def (:\n",
            "tests/test_ops.py": TEST_OPS,
        }
    )
    plan = repo.plan(base, base, [])
    index = plan.head_index
    again = index_from_dict(json.loads(json.dumps(index_to_dict(index))))
    assert index_to_dict(again) == index_to_dict(index)
    assert again.symbols == index.symbols and again.edges == index.edges
    assert again.unresolved == index.unresolved and again.errors == index.errors
    assert again.failed_modules == index.failed_modules


def test_cached_plan_is_identical_and_skips_git(repo, tmp_path, monkeypatch):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    targets = [
        py_target("t::test_add", "tests.test_ops.test_add"),
        py_target("t::test_mul", "tests.test_ops.test_mul"),
    ]
    cache = IndexCache(tmp_path / "cache")
    uncached = repo.plan(base, head, targets)
    first = repo.plan(base, head, targets, cache=cache)
    assert cache.misses == 2 and cache.hits == 0
    assert _plans_equal(first, uncached)

    reads: list[str] = []
    real = planner_mod.read_snapshot

    def counting(repo_path, revision, roots, **kw):
        reads.append(revision)
        return real(repo_path, revision, roots, **kw)

    monkeypatch.setattr(planner_mod, "read_snapshot", counting)
    second = repo.plan(base, head, targets, cache=cache)
    assert cache.hits == 2 and reads == []  # both commits served from cache
    assert _plans_equal(second, uncached)
    # A symbolic revision resolves to the cached commit and keeps its label.
    third = repo.plan("HEAD~1", "HEAD", targets, cache=cache)
    assert third.base.revision == "HEAD~1" and third.base.commit == base
    assert _plans_equal(third, uncached) or third.head.commit == head


def test_worktree_and_discovery_heads_bypass_the_cache(repo, tmp_path, monkeypatch):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    (repo.path / "pkg/ops.py").write_text(OPS.replace("a + b", "b + a"))
    cache = IndexCache(tmp_path / "cache")
    repo.plan(base, "WORKTREE", [], discover_runners=["pytest"], cache=cache)
    assert (cache.hits, cache.misses) == (0, 1)  # base missed and stored; WORKTREE never cached
    stored = list((tmp_path / "cache" / "index").glob("*.json"))
    assert len(stored) == 1
    repo.plan(base, "WORKTREE", [], discover_runners=["pytest"], cache=cache)
    assert cache.hits == 1
    # With discovery the head commit needs its files, so it is read, not cached.
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    repo.plan(base, head, [], discover_runners=["pytest"], cache=cache)
    assert len(list((tmp_path / "cache" / "index").glob("*.json"))) == 2  # head stored anyway
    plan_cached = repo.plan(base, head, [], discover_runners=["pytest"], cache=cache)
    plan_plain = repo.plan(base, head, [], discover_runners=["pytest"])
    assert _plans_equal(plan_cached, plan_plain)


def test_cli_cache_flags(repo, capsys, tmp_path):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    manifest = repo.write_manifest(
        [
            {
                "runner": "pytest",
                "runner_id": "t::test_add",
                "entry_symbol": "tests.test_ops.test_add",
            }
        ]
    )
    args = [
        "plan",
        "--repo",
        str(repo.path),
        "--base",
        base,
        "--head",
        head,
        "--targets",
        str(manifest),
    ]
    assert main([*args, "--cache-dir", str(tmp_path / "c")]) == 0
    out1 = capsys.readouterr().out
    assert (tmp_path / "c" / "index").exists()
    assert main([*args, "--cache-dir", str(tmp_path / "c")]) == 0
    assert capsys.readouterr().out == out1
    assert main([*args, "--no-cache"]) == 0
    assert capsys.readouterr().out == out1
    assert not (repo.path / ".diffcone").exists()  # default dir untouched by --no-cache
    assert main(args) == 0
    assert (repo.path / ".diffcone" / "cache" / "index").exists()
    assert cache_mod.INDEX_FORMAT == 2

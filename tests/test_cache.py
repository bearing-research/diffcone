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
    assert cache_mod.INDEX_FORMAT == 15
    assert len(cache_mod.INDEXER_FINGERPRINT) == 16


def test_module_cache_reindexes_only_what_changed(repo, tmp_path):
    from diffcone.cache import ModuleCache
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    ops = OPS + "\n\nX = 1\n\n\nclass K:\n    def m(self):\n        return X\n"
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": ops, "tests/test_ops.py": TEST_OPS})
    snap = read_snapshot(repo.path, base, ["."])
    plain = build_index(snap)
    mc = ModuleCache(tmp_path / "c")
    first = build_index(snap, module_cache=mc)
    assert index_to_dict(first) == index_to_dict(plain)
    assert (mc.facts_hits, mc.facts_misses) == (0, 3)
    assert (mc.resolved_hits, mc.resolved_misses) == (0, 3)
    second = build_index(snap, module_cache=mc)
    assert index_to_dict(second) == index_to_dict(plain)
    assert (mc.facts_hits, mc.resolved_hits) == (3, 3)

    # A body edit re-parses and re-resolves exactly the edited module.
    (repo.path / "pkg/ops.py").write_text(ops.replace("a + b", "b + a"))
    wt = read_snapshot(repo.path, "WORKTREE", ["."])
    mc = ModuleCache(tmp_path / "c")
    assert index_to_dict(build_index(wt, module_cache=mc)) == index_to_dict(build_index(wt))
    assert (mc.facts_hits, mc.facts_misses) == (2, 1)
    assert (mc.resolved_hits, mc.resolved_misses) == (2, 1)

    # Removing a symbol changes what other modules may see: their facts are
    # served, but every module is resolved again against the new environment.
    (repo.path / "pkg/ops.py").write_text(OPS + "\n\nX = 2\n")
    wt = read_snapshot(repo.path, "WORKTREE", ["."])
    mc = ModuleCache(tmp_path / "c")
    assert index_to_dict(build_index(wt, module_cache=mc)) == index_to_dict(build_index(wt))
    assert (mc.facts_hits, mc.facts_misses) == (2, 1)
    assert (mc.resolved_hits, mc.resolved_misses) == (0, 3)
    mc = ModuleCache(tmp_path / "c")
    build_index(wt, module_cache=mc)
    assert (mc.facts_hits, mc.resolved_hits) == (3, 3)


def test_module_cache_keeps_symbol_collision_errors(repo, tmp_path):
    """``retry`` in ``pkg/__init__.py`` shadows ``pkg/retry.py`` and so is
    ``pkg.__init__.retry``, which a module ``pkg/__init__/retry.py`` also
    names; the error is reported the same whether facts came from the cache
    (the colliding module is indexed afresh) or not."""
    from diffcone.cache import ModuleCache
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    mc = ModuleCache(tmp_path / "c")
    clean = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/retry.py": "",
            "pkg/__init__/__init__.py": "",
            "pkg/__init__/retry.py": "def f():\n    return 1\n",
        }
    )
    build_index(read_snapshot(repo.path, clean, ["."]), module_cache=mc)  # caches every module
    base = repo.commit({"pkg/__init__.py": "def retry():\n    pass\n"})
    snap = read_snapshot(repo.path, base, ["."])
    plain = build_index(snap)
    assert any("collides" in e.message for e in plain.errors)
    for _ in range(2):
        mc = ModuleCache(tmp_path / "c")
        assert index_to_dict(build_index(snap, module_cache=mc)) == index_to_dict(plain)
    # pkg.__init__.retry's cached facts hit but collide, so it is indexed
    # afresh (and its colliding record is never stored); the rest are served.
    assert (mc.facts_hits, mc.facts_misses) == (4, 0)


def test_module_cache_renames_a_binding_when_a_shadowed_submodule_appears(repo, tmp_path):
    """A package's cached facts name its binding ``pkg.a.b``; once
    ``pkg/a/b.py`` exists the binding is ``pkg.a.__init__.b``, so the stale
    record must not be applied although the file did not change."""
    from diffcone.cache import ModuleCache
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    mc = ModuleCache(tmp_path / "c")
    before = repo.commit(
        {"pkg/__init__.py": "", "pkg/a/__init__.py": "class b:\n    def m(self):\n        pass\n"}
    )
    first = build_index(read_snapshot(repo.path, before, ["."]), module_cache=mc)
    assert "pkg.a.b.m" in first.symbols
    after = repo.commit({"pkg/a/b.py": "X = 1\n"})
    snap = read_snapshot(repo.path, after, ["."])
    plain = build_index(snap)
    assert plain.errors == []
    assert {"pkg.a.__init__.b", "pkg.a.__init__.b.m", "pkg.a.b", "pkg.a.b.X"} <= set(plain.symbols)
    cached = build_index(snap, module_cache=ModuleCache(tmp_path / "c"))
    assert index_to_dict(cached) == index_to_dict(plain)


def test_worktree_plan_reresolves_one_module_after_a_body_edit(repo, tmp_path):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "pkg/more.py": "from pkg.ops import add\n\n\ndef twice(a):\n    return add(a, a)\n",
            "tests/test_ops.py": TEST_OPS,
        }
    )
    targets = [py_target("t::test_add", "tests.test_ops.test_add")]
    for _ in range(2):
        cache = IndexCache(tmp_path / "cache")
        repo.plan(base, "WORKTREE", targets, cache=cache)
    assert (cache.modules.facts_misses, cache.modules.resolved_misses) == (0, 0)
    (repo.path / "pkg/ops.py").write_text(OPS.replace("a + b", "b + a"))
    cache = IndexCache(tmp_path / "cache")
    result = repo.plan(base, "WORKTREE", targets, cache=cache)
    assert (cache.modules.facts_misses, cache.modules.resolved_misses) == (1, 1)
    assert cache.modules.facts_hits == 3 and cache.modules.resolved_hits == 3
    uncached = repo.plan(base, "WORKTREE", targets)
    assert _plans_equal(result, uncached)


def test_module_cache_treats_malformed_records_as_misses(repo, tmp_path):
    import sqlite3

    from diffcone.cache import ModuleCache
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    snap = read_snapshot(repo.path, base, ["."])
    plain = build_index(snap)
    mc = ModuleCache(tmp_path / "c")
    build_index(snap, module_cache=mc)
    with sqlite3.connect(mc.path) as conn:
        # One facts record missing keys, one resolution record naming an
        # unknown module, one row that is not JSON at all.
        key = ModuleCache.key("pkg.ops", "pkg/ops.py", OPS.encode())
        conn.execute("UPDATE records SET data = '{}' WHERE key = ? AND fingerprint = ''", [key])
        conn.execute(
            "UPDATE records SET data = ? WHERE key != ? AND fingerprint != ''",
            [json.dumps({"edges": [], "param_dynamics": [[0, 0, 0, 0, {"module": "?"}, 0]]}), key],
        )
        conn.execute("UPDATE records SET data = 'nope' WHERE key = ? AND fingerprint != ''", [key])
    mc = ModuleCache(tmp_path / "c")
    assert index_to_dict(build_index(snap, module_cache=mc)) == index_to_dict(plain)
    with sqlite3.connect(mc.path) as conn:  # every bad row was replaced by a good one
        rows = conn.execute("SELECT data FROM records").fetchall()
    assert len(rows) == 6
    assert all(set(json.loads(d)) >= {"edges"} for (d,) in rows)


def test_module_cache_keeps_one_resolution_per_file_and_serves_read_only(repo, tmp_path):
    import os
    import sqlite3

    from diffcone.cache import ModuleCache
    from diffcone.indexer import build_index
    from diffcone.snapshot import read_snapshot

    repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    mc = ModuleCache(tmp_path / "c")
    build_index(read_snapshot(repo.path, "WORKTREE", ["."]), module_cache=mc)
    # Three environment-changing edits of one file: every module is resolved
    # again each time, but only the latest fingerprint's rows survive.
    for n in range(3):
        (repo.path / "pkg/ops.py").write_text(OPS + f"\n\nX{n} = {n}\n")
        build_index(read_snapshot(repo.path, "WORKTREE", ["."]), module_cache=mc)
    with sqlite3.connect(mc.path) as conn:
        facts = conn.execute("SELECT COUNT(*) FROM records WHERE fingerprint = ''").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM records WHERE fingerprint != ''").fetchone()
        key = ModuleCache.key("pkg", "pkg/__init__.py", b"")
        per_file = conn.execute("SELECT COUNT(*) FROM records WHERE key = ?", [key]).fetchone()
    assert facts == 3 + 3  # unchanged modules once, ops.py per content
    assert resolved[0] <= facts and per_file[0] == 2  # facts + the latest resolution only
    if os.geteuid() == 0:  # pragma: no cover - root ignores permission bits
        return
    wt = read_snapshot(repo.path, "WORKTREE", ["."])
    plain = index_to_dict(build_index(wt))
    for p in (mc.path, mc.path.parent):
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    try:
        mc = ModuleCache(tmp_path / "c")
        assert index_to_dict(build_index(wt, module_cache=mc)) == plain
        assert (mc.facts_hits, mc.resolved_hits) == (3, 3)
        (repo.path / "pkg/ops.py").write_text(OPS + "\n\nY = 9\n")  # content never seen
        wt = read_snapshot(repo.path, "WORKTREE", ["."])
        mc = ModuleCache(tmp_path / "c")
        assert index_to_dict(build_index(wt, module_cache=mc)) == index_to_dict(build_index(wt))
        assert mc.facts_misses == 1  # computed, and the failed store is silent
    finally:
        for p in (mc.path.parent, mc.path):
            os.chmod(p, 0o755 if p.is_dir() else 0o644)


def test_index_cache_removes_the_legacy_hash_directory(tmp_path):
    (tmp_path / "c" / "hashes").mkdir(parents=True)
    (tmp_path / "c" / "hashes" / "x.json").write_text("{}")
    IndexCache(tmp_path / "c")
    assert not (tmp_path / "c" / "hashes").exists()

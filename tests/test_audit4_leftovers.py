"""Regression scenarios for round 4 of the pre-release audit: the leftovers
the batch agents found outside their files (L1 to L3).

Each test names the finding it guards, and each failed before its fix.
"""

from __future__ import annotations

import json

import pytest

from diffcone.cache import IndexCache
from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import build_index
from diffcone.model import IMPORTS, SnapshotInfo
from diffcone.snapshot import Snapshot, read_snapshot
from diffcone.testing import asv_target, selected

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'
BENCH = {
    "asv.conf.json": ASV_CONF,
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}
BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
TESTS = {"tests/__init__.py": "", "tests/test_ok.py": "def test_ok():\n    pass\n"}

# ---------------------------------------------------------------------- L1
# A module too deeply nested for discovery's recursive walks (ast.unparse
# of a test name bound to a long attribute chain, ast.parse itself on a
# longer one) raised RecursionError out of discovery: no plan at all. It is
# now left out as a file that does not parse (``unparsed_file``, so
# discovery is incomplete), and such a result is not cached.

CHAIN = "import a\n\n{0} = a" + ".b" * 1000 + "\n"
LONG_CHAIN = "import a\n\nX = a" + ".b" * 100_000 + "\n"


def _discover(repo, rev, runner, **options):
    snap = read_snapshot(repo.path, rev, ["."], with_config=True)
    return discover(runner, snap, build_index(snap), DiscoveryOptions(**options))


def _unparsed(result) -> set[str]:
    return {n.detail for n in result.notes if n.kind == "unparsed_file"}


@pytest.mark.parametrize(
    "module",
    [
        CHAIN.format("test_x"),
        "import a\n\n\nclass TestX:\n" + CHAIN.format("    test_y").split("\n\n", 1)[1],
        LONG_CHAIN,
    ],
    ids=["module_binding", "class_binding", "parse"],
)
def test_l1_a_too_deep_test_module_leaves_discovery_incomplete(repo, tmp_path, module):
    base = repo.commit({**BENCH, **TESTS, "tests/test_x.py": module})
    head = repo.commit({"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert {(n.kind, n.path) for n in plan.incomplete_discovery} == {
        ("unparsed_file", "tests/test_x.py")
    }
    assert any("too deeply nested" in n.detail for n in plan.incomplete_discovery)
    assert {"tests/test_ok.py::test_ok", "bench.time_noop"} <= selected(plan)
    # Running out of stack depends on the caller: not cached.
    cache = IndexCache(tmp_path / "cache")
    repo.plan(base, head, [], discover_runners=["pytest", "asv"], cache=cache)
    stored = [json.loads(p.read_text()) for p in (tmp_path / "cache" / "discovery").glob("*")]
    assert sorted(d["runner"] for d in stored) == ["asv", "asv"]


def test_l1_a_too_deep_conftest_and_benchmark_module(repo):
    rev = repo.commit(
        {
            **BENCH,
            **TESTS,
            "tests/conftest.py": LONG_CHAIN,
            "benchmarks/deep.py": LONG_CHAIN + "\n\ndef time_x():\n    pass\n",
        }
    )
    found = _discover(repo, rev, "pytest")
    assert {n.path for n in found.notes if n.kind == "unparsed_file"} == {"tests/conftest.py"}
    assert {t.runner_id for t in found.targets} == {"tests/test_ok.py::test_ok"}
    found = _discover(repo, rev, "asv")
    assert _unparsed(found) == {"benchmarks/deep.py: too deeply nested to analyse"}
    assert {t.runner_id for t in found.targets} == {"bench.time_noop"}


def test_l1_a_too_deep_doctest_or_timeraw_snippet_is_unread(repo):
    """Code in a string (a doctest example, the code a ``timeraw_``
    benchmark returns) is parsed on its own: too deep, it is code that
    cannot be read, as a syntax error is."""
    snippet = "x = a" + ".b" * 100_000
    rev = repo.commit(
        {
            **BENCH,
            "pkg/__init__.py": "",
            "pkg/mod.py": f"def f():\n    '''\n    >>> {snippet}\n    '''\n",
            "benchmarks/raw.py": f"def timeraw_x():\n    return {snippet!r}\n",
        }
    )
    found = _discover(repo, rev, "pytest", runner_args=("--doctest-modules",))
    target = next(t for t in found.targets if t.runner_id.endswith("pkg.mod.f"))
    assert "doctest:unparsed" in target.lifecycle_dependencies
    found = _discover(repo, rev, "asv")
    raw = next(t for t in found.targets if t.runner_id == "raw.timeraw_x")
    assert any("timeraw" in d for d in raw.lifecycle_dependencies)


# ---------------------------------------------------------------------- L2
# The reference collector read a callee or a stored-to chain through one
# import binding only (the last of an import in ``try`` and another in
# ``except``, the primary of a function-local one) and never through a star
# import, so a write to ``os.environ`` through any other binding was no
# process write: a test that ran after it was not selected. Every name the
# chain may stand for is considered now; where recognising a name leaves
# something out (``with catch_warnings():`` puts the filters back), every
# name must be it.

TEST_B = "def test_b():\n    pass\n"
WRITE = "environ['MODE'] = '{0}'\n\n\ndef test_a():\n    pass\n"


@pytest.mark.parametrize(
    "module",
    [
        "from os import *\n\n" + WRITE,
        "try:\n    from os import environ\nexcept ImportError:\n    from compat import environ\n\n"
        + WRITE,
        "def _set(v):\n    try:\n        from os import environ\n    except ImportError:\n"
        "        from compat import environ\n    environ['MODE'] = v\n\n\n_set('{0}')\n\n\n"
        "def test_a():\n    pass\n",
    ],
    ids=["star", "alt_import", "local_alt_import"],
)
def test_l2_a_process_write_through_any_binding_reaches_every_test(repo, module):
    files = {
        "tests/__init__.py": "",
        "tests/test_a.py": module.format("x"),
        "tests/test_b.py": TEST_B,
    }
    base = repo.commit({**BENCH, **files})
    head = repo.commit({"tests/test_a.py": module.format("y")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_l2_a_with_that_may_not_be_catch_warnings_restores_nothing(repo):
    module = (
        "import warnings\n\ntry:\n    from compat import nothing as catch_warnings\n"
        "except ImportError:\n    from warnings import catch_warnings\n\n"
        "with catch_warnings():\n    warnings.filterwarnings('{0}')\n\n\n"
        "def test_a():\n    pass\n"
    )
    files = {
        "tests/__init__.py": "",
        "tests/test_a.py": module.format("ignore"),
        "tests/test_b.py": TEST_B,
    }
    base = repo.commit({**BENCH, **files})
    head = repo.commit({"tests/test_a.py": module.format("error")})
    plan = repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


# ---------------------------------------------------------------------- L3
# ``m.__file__`` in a command runs the file of whichever module ``m`` is; on
# a name bound several ways only the first binding's module was an import
# of the code running it. (Latent: the bindings' own edges reached the same
# modules in every scenario tried.)


@pytest.mark.parametrize(
    ("files", "expr"),
    [
        (
            {
                "pkg/__init__.py": "",
                "pkg/fast.py": "",
                "pkg/slow.py": "",
                "tests/test_a.py": (
                    "try:\n    import pkg.fast as tool\nexcept ImportError:\n"
                    "    import pkg.slow as tool\n"
                ),
            },
            "tool",
        ),
        (
            {
                "pkg/__init__.py": "from pkg.other import fast\n",
                "pkg/other.py": "def fast():\n    pass\n",
                "pkg/fast.py": "",
                "tests/test_a.py": "import pkg\n",
            },
            "pkg.fast",
        ),
    ],
    ids=["alt_import", "submodule_of_the_same_name"],
)
def test_l3_a_module_file_run_by_name_counts_every_binding(files, expr):
    files = {**files, "tests/__init__.py": ""}
    files["tests/test_a.py"] = (
        f"import subprocess\nimport sys\n\n{files['tests/test_a.py']}\n\ndef test_a():\n"
        f"    subprocess.run([sys.executable, {expr}.__file__])\n"
    )
    snapshot = Snapshot(
        info=SnapshotInfo(revision="x", commit="x"),
        source_roots=["."],
        files={k: v.encode() for k, v in files.items()},
    )
    edges = build_index(snapshot).edges
    imported = {e.target for e in edges if e.source == "tests.test_a.test_a" and e.kind == IMPORTS}
    assert "pkg.fast" in imported

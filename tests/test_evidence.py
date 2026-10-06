"""Execution evidence: the recorder, the store, and what collection refuses.

These run the fixture repository's suite for real under the recorder
(``diffcone collect``), so each asserts exactly what one test executed or
touched.
"""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from diffcone.cli import main
from diffcone.evidence import (
    FLAG_SUBPROCESS,
    FLAG_UNSTABLE,
    Evidence,
    EvidenceError,
    TestRecord,
    advance,
    list_stores,
    load_store,
    write_store,
)
from diffcone.snapshot import GitError
from diffcone.testing import executed, selected, touched

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

OPS = """\
import json


def add(a, b):
    return a + b


def mul(a, b):
    return a * b


def load():
    with open("pkg/data.json") as f:
        return json.load(f)
"""

CONFTEST = """\
import pytest


@pytest.fixture(scope="session")
def shared():
    from pkg.ops import mul

    return mul(2, 2)
"""

TESTS = """\
import os
import subprocess
import sys

import pytest

from pkg.ops import add, load


@pytest.mark.parametrize("x", [1, 2])
def test_add(x):
    assert add(x, 1) == x + 1


def test_shared(shared):
    assert shared == 4


def test_shared_again(shared):
    assert shared == 4


def test_shared_by_name(request):
    assert request.getfixturevalue("shared") == 4


def test_load():
    assert load() == [1]


def test_exists():
    assert not os.path.exists("pkg/extra.txt")


def test_listing():
    assert "data.json" in os.listdir("pkg")


def test_subprocess():
    subprocess.run([sys.executable, "-c", "pass"], check=True)
"""

FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "pkg/data.json": "[1]",
    "tests/__init__.py": "",
    "tests/conftest.py": CONFTEST,
    "tests/test_ops.py": TESTS,
}
T = "tests/test_ops.py::"


def test_each_test_records_what_it_executed_and_touched(repo):
    repo.commit(FILES)
    ev = repo.collect()
    assert executed(ev, T + "test_add") == {"pkg.ops.add", "tests.test_ops.test_add"}
    # A session fixture's setup is credited to every test that used it, the
    # ones after the first included, and to one that asked for it by name.
    for test in ("test_shared", "test_shared_again", "test_shared_by_name"):
        assert {"pkg.ops.mul", "tests.conftest.shared"} <= executed(ev, T + test)
    assert executed(ev, T + "test_load") == {"pkg.ops.load", "tests.test_ops.test_load"}
    assert "pkg/data.json" in touched(ev, T + "test_load")
    assert "pkg/data.json" not in touched(ev, T + "test_add")
    # A stat of a file that does not exist yet, and a directory listing.
    assert "pkg/extra.txt" in touched(ev, T + "test_exists")
    assert "pkg" in ev.listed(ev.tests[T + "test_listing"])
    flags = {t: r.flags for t, r in ev.tests.items()}
    assert {t for t, f in flags.items() if f & FLAG_SUBPROCESS} == {T + "test_subprocess"}
    assert not any(f & FLAG_UNSTABLE for f in flags.values())
    assert ev.environment["variables"]["PYTHONHASHSEED"] == "0"
    assert {"pkg.ops", "tests.conftest", "tests.test_ops"} <= ev.import_phase


def test_code_run_by_an_import_is_attributed_to_the_importing_module(repo):
    repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/registry.py": "def make():\n    return 1\n",
            "pkg/table.py": "from pkg.registry import make\n\nVALUE = make()\n",
            "tests/test_table.py": "from pkg.table import VALUE\n\n\n"
            "def test_value():\n    assert VALUE == 1\n",
        }
    )
    ev = repo.collect()
    assert ev.import_by["pkg.registry.make"] == {"pkg.table"}
    assert "pkg.registry.make" in ev.import_phase
    # It ran before the test, so it is not in the test's own record.
    assert executed(ev, "tests/test_table.py::test_value") == {"tests.test_table.test_value"}


def test_functools_caches_are_cleared_between_tests(repo):
    repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/calc.py": "import functools\n\n\n@functools.lru_cache\n"
            "def compute():\n    return 1\n",
            "tests/test_calc.py": "from pkg.calc import compute\n\n\n"
            "def test_one():\n    assert compute() == 1\n\n\n"
            "def test_two():\n    assert compute() == 1\n",
        }
    )
    ev = repo.collect()
    assert "pkg.calc.compute" in executed(ev, "tests/test_calc.py::test_one")
    assert "pkg.calc.compute" in executed(ev, "tests/test_calc.py::test_two")


def test_a_record_that_depends_on_test_order_is_unstable(repo):
    repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/memo.py": "_VALUE = None\n\n\ndef compute():\n    return 1\n\n\n"
            "def get():\n    global _VALUE\n    if _VALUE is None:\n"
            "        _VALUE = compute()\n    return _VALUE\n",
            "pkg/plain.py": "def f():\n    return 2\n",
            "tests/test_memo.py": "from pkg.memo import get\nfrom pkg.plain import f\n\n\n"
            "def test_one():\n    assert get() == 1\n\n\n"
            "def test_two():\n    assert get() == 1\n\n\n"
            "def test_plain():\n    assert f() == 2\n",
        }
    )
    ev = repo.collect(reverse_check=True)
    unstable = {t for t, r in ev.tests.items() if r.flags & FLAG_UNSTABLE}
    assert unstable == {"tests/test_memo.py::test_one", "tests/test_memo.py::test_two"}
    # The record is the union of both orders.
    assert "pkg.memo.compute" in executed(ev, "tests/test_memo.py::test_two")
    assert ev.reverse_checked


def test_the_store_round_trips(repo, tmp_path):
    repo.commit(FILES)
    ev = repo.collect()
    again = load_store(write_store(ev, tmp_path / "stores"))
    assert again.commit == ev.commit
    assert again.environment == ev.environment
    assert again.environment_hash == ev.environment_hash
    assert {t: (again.executed(r), again.touched(r), r.flags) for t, r in again.tests.items()} == {
        t: (ev.executed(r), ev.touched(r), r.flags) for t, r in ev.tests.items()
    }
    assert again.import_by == ev.import_by
    assert again.import_phase == ev.import_phase
    assert again.import_paths == ev.import_paths
    assert [s.commit for s in list_stores(repo.path)] == [ev.commit]


def test_collection_at_an_older_commit_uses_a_temporary_worktree(repo):
    first = repo.commit(FILES)
    repo.commit({"pkg/ops.py": OPS + "\n\ndef extra():\n    return 3\n"})
    (repo.path / "pkg" / "ops.py").write_text("broken(", "utf-8")  # a dirty tree is fine here
    ev = repo.collect(first)
    assert ev.commit == first
    assert executed(ev, T + "test_add") == {"pkg.ops.add", "tests.test_ops.test_add"}


def test_the_record_is_finished_before_the_session_reports_done(repo):
    # pytest-xdist reports a worker done after its session finishes and then
    # kills workers that take more than ten seconds to exit, so the record
    # must not wait for unconfigure. This conftest's unconfigure runs before
    # the recorder's (pluggy calls later registrations first).
    conftest = (
        CONFTEST
        + """

def pytest_unconfigure(config):
    import os

    out = os.environ["DIFFCONE_COLLECT_OUT"]
    assert os.path.exists(os.path.join(out, f"process-{os.getpid()}.json"))
"""
    )
    repo.commit({**FILES, "tests/conftest.py": conftest})
    ev = repo.collect()
    assert executed(ev, T + "test_add") == {"pkg.ops.add", "tests.test_ops.test_add"}


def test_a_dirty_tree_is_refused(repo):
    repo.commit(FILES)
    (repo.path / "pkg" / "ops.py").write_text(OPS + "\n# edited\n", "utf-8")
    with pytest.raises(GitError, match="evidence describes a commit"):
        repo.collect()


def test_a_suite_that_runs_an_installed_copy_is_refused(repo, tmp_path):
    installed = tmp_path / "site"
    (installed / "pkg").mkdir(parents=True)
    (installed / "pkg" / "__init__.py").write_text("", "utf-8")
    (installed / "pkg" / "ops.py").write_text(OPS, "utf-8")
    repo.commit(
        {
            **FILES,
            # The copy is imported first, as an installed package would be.
            "tests/conftest.py": f"import sys\n\nsys.path.insert(0, {str(installed)!r})\n"
            "import pkg.ops  # noqa: E402\n\n" + CONFTEST,
        }
    )
    with pytest.raises(EvidenceError, match="installed copy"):
        repo.collect()


def test_collect_and_evidence_commands(repo, capsys):
    repo.commit(FILES)
    command = f"{sys.executable} -m pytest"
    assert main(["collect", "--repo", str(repo.path), "--command", command, "--no-cache"]) == 0
    out, err = capsys.readouterr()
    assert out.strip().endswith(".sqlite")
    assert "recorded 8 tests" in err
    assert "1 started a subprocess" in err
    assert main(["evidence", "--repo", str(repo.path)]) == 0
    assert "8 tests" in capsys.readouterr().out


def _edited_add(repo):
    repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    return ev, head


def test_run_with_evidence_runs_only_the_tests_that_executed_the_change(repo, capfd):
    ev, head = _edited_add(repo)
    command = f"{sys.executable} -m pytest"
    args = ["run", "--repo", str(repo.path), "--base", ev.commit, "--head", head]
    args += ["--discover", "pytest", "--command", command, "--evidence", "auto", "--no-cache"]
    assert main(args + ["--", "-q"]) == 0
    out, err = capfd.readouterr()
    assert "2 of 8 pytest target(s) selected" in err  # test_add, and test_subprocess
    assert "3 passed" in out  # test_add's two cases and test_subprocess
    assert "environment differs" not in err


def test_run_falls_back_to_static_planning_in_another_environment(repo, capsys, monkeypatch):
    from diffcone import execution
    from diffcone.testing import selected

    ev, head = _edited_add(repo)
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {T + "test_add", T + "test_subprocess"}
    plan.evidence["environment_hash"] = "0" * 16  # recorded somewhere else
    static = repo.plan(ev.commit, head, [], discover_runners=["pytest"])
    run = execution.run_with_evidence(
        plan, lambda: static, cwd=repo.path, command=f"{sys.executable} -m pytest", extra=["-q"]
    )
    assert run.mismatch is not None and run.mismatch["python"] == ev.environment["python"]
    assert run.result.returncode != 0  # stopped before any test ran
    assert run.static is not None and run.static.returncode == 0
    assert {t.runner_id for t in run.static.selected} == selected(static)


def test_validate_with_evidence(repo):
    from diffcone.execution import validate_pytest

    ev, head = _edited_add(repo)
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    v = validate_pytest(plan, repo=repo.path, command=f"{sys.executable} -m pytest", coverage=True)
    assert v.ok
    assert v.coverage is not None and not v.coverage.missed


# --------------------------------------------------------------------------- advancing


def _run_collect(repo, base, head, *extra):
    args = ["run", "--repo", str(repo.path), "--base", base, "--head", head]
    args += ["--discover", "pytest", "--command", f"{sys.executable} -m pytest"]
    return main(args + ["--evidence", "auto", "--collect", "--no-cache", *extra])


def test_run_collect_advances_the_store_to_head(repo, capfd):
    repo.commit(FILES)
    ev = repo.collect()
    helper = OPS.replace(
        "def add(a, b):\n    return a + b",
        "def _plus(a, b):\n    return a + b\n\n\ndef add(a, b):\n    return _plus(a, b)",
    )
    head = repo.commit({"pkg/ops.py": helper})
    assert _run_collect(repo, ev.commit, head) == 0
    err = capfd.readouterr().err
    assert "2 of 8 pytest target(s) selected" in err  # test_add, and test_subprocess
    assert "2 test(s) recorded afresh" in err
    stores = {s.commit: s for s in list_stores(repo.path)}
    head_commit = repo.git("rev-parse", head).strip()
    assert set(stores) == {ev.commit, head_commit}
    assert (stores[head_commit].advanced_from, stores[head_commit].full_commit) == (
        ev.commit,
        ev.commit,
    )
    advanced = load_store(stores[head_commit].path)
    assert set(advanced.tests) == set(ev.tests)
    # The selected test's record is new; the others carry over unchanged.
    assert executed(advanced, T + "test_add") == {
        "pkg.ops._plus",
        "pkg.ops.add",
        "tests.test_ops.test_add",
    }
    for test in set(ev.tests) - {T + "test_add", T + "test_subprocess"}:
        assert executed(advanced, test) == executed(ev, test)
        assert touched(advanced, test) == touched(ev, test)
    assert advanced.tests[T + "test_subprocess"].flags & FLAG_SUBPROCESS

    # The next plan starts from the advanced store, so it sees only its own change.
    later = repo.commit(
        {"pkg/ops.py": helper.replace("return json.load(f)", "return list(json.load(f))")}
    )
    plan = repo.plan(head, later, [], discover_runners=["pytest"], evidence=advanced)
    assert plan.evidence["commit"] == head_commit
    assert selected(plan) == {T + "test_load", T + "test_subprocess"}
    assert main(["evidence", "--repo", str(repo.path)]) == 0
    assert f"advanced from {ev.commit[:12]}" in capfd.readouterr().out


def test_run_collect_with_nothing_selected_relabels_the_store(repo, capfd):
    # A test that starts a subprocess is always selected: leave it out.
    tests = TESTS[: TESTS.index("\n\ndef test_subprocess")] + "\n"
    repo.commit({**FILES, "tests/test_ops.py": tests})
    ev = repo.collect()
    repo.git("commit", "--allow-empty", "-q", "-m", "empty")
    head = repo.git("rev-parse", "HEAD").strip()
    assert _run_collect(repo, ev.commit, head) == 0
    assert "nothing selected" in capfd.readouterr().err
    head_commit = repo.git("rev-parse", head).strip()
    (advanced,) = [s for s in list_stores(repo.path) if s.commit == head_commit]
    relabelled = load_store(advanced.path)
    assert {t: executed(relabelled, t) for t in relabelled.tests} == {
        t: executed(ev, t) for t in ev.tests
    }


def test_run_collect_refusals(repo, capsys):
    repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    # Other pytest arguments could deselect cases the old record covered.
    assert _run_collect(repo, ev.commit, head, "--", "-q") == 2
    assert "differ from the ones the store was collected with" in capsys.readouterr().err
    (repo.path / "pkg" / "ops.py").write_text(OPS + "\n# edited\n", "utf-8")
    assert _run_collect(repo, ev.commit, "WORKTREE") == 2
    assert "a store describes a commit" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["run", "--repo", str(repo.path), "--base", ev.commit, "--head", head])
    with pytest.raises(SystemExit):
        main(["run", "--repo", str(repo.path), "--base", "HEAD", "--head", "HEAD", "--collect"])


def test_run_collect_in_another_environment_does_not_advance(repo):
    from diffcone import execution

    repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    plan.evidence["environment_hash"] = "0" * 16  # recorded somewhere else
    static = repo.plan(ev.commit, head, [], discover_runners=["pytest"])
    run = execution.run_with_evidence(
        plan,
        lambda: static,
        cwd=repo.path,
        command=f"{sys.executable} -m pytest",
        advance_from=ev,
    )
    assert run.mismatch is not None and run.advanced is None
    assert [s.commit for s in list_stores(repo.path)] == [ev.commit]


def _evidence(commit, tests, **process):
    symbols = sorted({s for symbols, _ in tests.values() for s in symbols})
    paths = sorted({p for _, paths in tests.values() for p in paths})
    return Evidence(
        commit=commit,
        source_roots=["."],
        environment={},
        environment_hash="e",
        command="pytest",
        created=0.0,
        symbols=symbols,
        paths=paths,
        tests={
            name: TestRecord(
                frozenset(symbols.index(s) for s in syms),
                frozenset(paths.index(p) for p in ps),
                frozenset(),
                flags,
            )
            for name, ((syms, ps), flags) in (
                (n, (v, process.get("flags", {}).get(n, 0))) for n, v in tests.items()
            )
        },
        import_phase=frozenset(process.get("import_phase", ())),
        import_by=process.get("import_by", {}),
    )


def test_advance_merges_records_by_name():
    previous = _evidence(
        "c",
        {
            "t::kept": ({"m.a", "m.b"}, {"data.json"}),
            "t::rerun": ({"m.a"}, set()),
            "t::gone": ({"m.c"}, set()),
        },
        flags={"t::rerun": FLAG_UNSTABLE},
        import_phase={"m"},
        import_by={"m.a": frozenset({"m"})},
    )
    fresh = _evidence(
        "h",
        {"t::rerun": ({"m.z"}, {"other.txt"}), "t::new": ({"m.b"}, set())},
        import_phase={"n"},
        import_by={"m.a": frozenset({"n"})},
    )
    out = advance(previous, fresh, {"t::rerun", "t::gone", "t::new"}, "h")
    # A carried record of a test that is no longer a target is dropped.
    pruned = advance(previous, fresh, {"t::rerun"}, "h", alive={"t::rerun", "t::new"})
    assert set(pruned.tests) == {"t::rerun", "t::new"}
    # Unselected: carried. Rerun: the new record, still unstable. Selected but
    # not recorded: dropped, so it has no evidence from here on.
    assert set(out.tests) == {"t::kept", "t::rerun", "t::new"}
    assert out.executed(out.tests["t::kept"]) == {"m.a", "m.b"}
    assert out.touched(out.tests["t::kept"]) == {"data.json"}
    assert out.executed(out.tests["t::rerun"]) == {"m.z"}
    assert out.touched(out.tests["t::rerun"]) == {"other.txt"}
    assert out.tests["t::rerun"].flags & FLAG_UNSTABLE
    assert out.import_phase == {"m", "n"}
    assert out.import_by == {"m.a": frozenset({"m", "n"})}
    assert (out.commit, out.advanced_from, out.full_commit) == ("h", "c", "c")
    with pytest.raises(EvidenceError, match="environment"):
        advance(previous, replace(fresh, environment_hash="x"), set(), "h")


def test_run_collect_drops_the_records_of_deleted_tests(repo, capfd):
    repo.commit(FILES)
    ev = repo.collect()
    assert T + "test_listing" in ev.tests
    listing = '\n\ndef test_listing():\n    assert "data.json" in os.listdir("pkg")\n'
    head = repo.commit({"tests/test_ops.py": TESTS.replace(listing, "")})
    assert _run_collect(repo, ev.commit, head) == 0
    capfd.readouterr()
    head_commit = repo.git("rev-parse", head).strip()
    (store,) = [s for s in list_stores(repo.path) if s.commit == head_commit]
    advanced = load_store(store.path)
    assert set(advanced.tests) == set(ev.tests) - {T + "test_listing"}

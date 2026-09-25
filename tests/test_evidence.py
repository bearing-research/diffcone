"""Execution evidence: the recorder, the store, and what collection refuses.

These run the fixture repository's suite for real under the recorder
(``diffcone collect``), so each asserts exactly what one test executed or
touched.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.cli import main
from diffcone.evidence import (
    FLAG_SUBPROCESS,
    FLAG_UNSTABLE,
    EvidenceError,
    list_stores,
    load_store,
    write_store,
)
from diffcone.snapshot import GitError
from diffcone.testing import executed, touched

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
    assert "pkg" in touched(ev, T + "test_listing")
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

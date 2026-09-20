"""run / validate: executing the selected set and checking it."""

from __future__ import annotations

import json
import sys

from diffcone.cli import main
from diffcone.execution import build_command, parse_pytest_verbose, run_selected, validate_pytest
from diffcone.testing import asv_target, py_target

PYTEST = f"{sys.executable} -m pytest"

OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_OPS = (
    "from pkg.ops import add, mul\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)
TARGETS = [
    py_target("tests/test_ops.py::test_add", "tests.test_ops.test_add"),
    py_target("tests/test_ops.py::test_mul", "tests.test_ops.test_mul"),
    asv_target("bench.time_add", "tests.test_ops.test_add"),
]


def test_build_command():
    assert build_command("pytest", TARGETS[:2], None, ["-x"]) == [
        "python",
        "-m",
        "pytest",
        "-x",
        "tests/test_ops.py::test_add",
        "tests/test_ops.py::test_mul",
    ]
    assert build_command("asv", [TARGETS[2]], "asv run --quick", []) == [
        "asv",
        "run",
        "--quick",
        "--bench",
        "^(bench\\.time_add)$",
    ]


def test_parse_pytest_verbose_folds_parameter_cases():
    out = (
        "tests/test_a.py::test_x[1] PASSED [ 25%]\n"
        "tests/test_a.py::test_x[2] FAILED [ 50%]\n"
        "tests/test_a.py::TestK::test_m PASSED [ 75%]\n"
        "tests/test_a.py::test_e ERROR [100%]\n"
        "some other line\n"
    )
    assert parse_pytest_verbose(out) == {
        "tests/test_a.py::test_x": "FAILED",
        "tests/test_a.py::TestK::test_m": "PASSED",
        "tests/test_a.py::test_e": "ERROR",
    }


def test_run_executes_only_selected_targets(repo, capsys):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "a + b + 1")})  # breaks test_add
    plan = repo.plan(base, head, TARGETS)

    dry = run_selected(plan, "pytest", cwd=repo.path, command=PYTEST, dry_run=True)
    assert [t.runner_id for t in dry.selected] == ["tests/test_ops.py::test_add"]
    assert dry.total == 2 and dry.returncode is None
    assert dry.command[-1] == "tests/test_ops.py::test_add"

    real = run_selected(plan, "pytest", cwd=repo.path, command=PYTEST, extra=["-q"])
    assert real.returncode == 1  # test_add fails at head, and only test_add ran

    manifest = repo.write_manifest(
        [
            {"runner": t.runner, "runner_id": t.runner_id, "entry_symbol": t.entry_symbol}
            for t in TARGETS
        ]
    )
    code = main(
        [
            "run",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--targets",
            str(manifest),
            "--command",
            PYTEST,
            "--dry-run",
        ]
    )
    out, err = capsys.readouterr()
    assert code == 0
    assert out.strip().endswith("tests/test_ops.py::test_add")
    assert "1 of 2 pytest target(s) selected" in err

    code = main(
        [
            "run",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--targets",
            str(manifest),
            "--command",
            PYTEST,
            "--",
            "-q",
        ]
    )
    assert code == 1

    # Nothing selected: nothing runs, exit 0.
    code = main(
        [
            "run",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            base,
            "--targets",
            str(manifest),
            "--command",
            PYTEST,
        ]
    )
    assert code == 0
    assert "nothing selected" in capsys.readouterr().err

    code = main(
        [
            "run",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--targets",
            str(manifest),
            "--runner",
            "asv",
            "--dry-run",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "asv run --bench '^(bench\\.time_add)$'"


def test_validate_catches_and_misses(repo, capsys):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "data.txt": "3\n",
            "tests/test_ops.py": TEST_OPS,
            "tests/test_data.py": (
                "from pathlib import Path\n\n\n"
                "def test_data():\n"
                "    data = Path(__file__).parent.parent / 'data.txt'\n"
                "    assert data.read_text() == '3\\n'\n"
            ),
        }
    )
    # test_add breaks (visible to the planner); test_data breaks via a data
    # file (invisible to static analysis) -> a genuine miss.
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "a + b + 1"), "data.txt": "4\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    v = validate_pytest(plan, repo=repo.path, command=PYTEST)
    assert not v.ok
    assert [(o.runner_id, o.base, o.head) for o in v.caught] == [
        ("tests/test_ops.py::test_add", "PASSED", "FAILED")
    ]
    assert [(o.runner_id, o.base, o.head) for o in v.missed] == [
        ("tests/test_data.py::test_data", "PASSED", "FAILED")
    ]
    assert v.selected_count == 1 and len(v.outcomes) == 3
    # The temporary worktrees are gone.
    assert "diffcone-validate" not in repo.git("worktree", "list")

    code = main(
        [
            "validate",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--discover",
            "pytest",
            "--command",
            PYTEST,
            "--format",
            "json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    assert data["ok"] is False
    assert data["counts"] == {
        "targets": 3,
        "selected": 1,
        "outcome_changed": 2,
        "caught": 1,
        "missed": 1,
    }

    # A clean case: only the visible change, and WORKTREE as head.
    (repo.path / "data.txt").write_text("3\n")
    code = main(
        [
            "validate",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            "WORKTREE",
            "--discover",
            "pytest",
            "--command",
            PYTEST,
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "validation (pytest): OK" in out
    assert "caught tests/test_ops.py::test_add: PASSED -> FAILED" in out

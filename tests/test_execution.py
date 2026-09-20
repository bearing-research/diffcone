"""run / validate: executing the selected set and checking it."""

from __future__ import annotations

import json
import sys

import pytest

from diffcone import execution
from diffcone.cli import main
from diffcone.execution import build_command, parse_pytest_verbose, run_selected, validate_pytest
from diffcone.snapshot import GitError
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


def test_validate_with_coverage_measures_recall(repo, capsys):
    base = repo.commit(
        {
            "pytest.ini": "[pytest]\npythonpath = src\n",
            "src/pkg/__init__.py": "",
            "src/pkg/ops.py": OPS,
            "src/pkg/util.py": "def helper():\n    return 1\n",
            # Outside the source roots: statically invisible, dynamically executed.
            "ext/__init__.py": "",
            "ext/bridge.py": (
                "from pkg.util import helper\n\n\ndef via_bridge():\n    return helper()\n"
            ),
            "tests/test_ops.py": TEST_OPS,
            "tests/test_bridge.py": (
                "from ext.bridge import via_bridge\n\n\n"
                "def test_bridge():\n    assert via_bridge() >= 1\n"
            ),
        }
    )
    head = repo.commit(
        {
            "src/pkg/ops.py": OPS.replace("a * b", "b * a"),  # same outcome, different body
            "src/pkg/util.py": "def helper():\n    return 2\n",  # same outcome via >= 1
        }
    )
    roots = ["src", "tests"]
    plan = repo.plan(base, head, [], source_roots=roots, discover_runners=["pytest"])
    # Static view: test_mul reaches mul; test_bridge cannot reach helper.
    assert {d.target.runner_id for d in plan.decisions if d.selected} == {
        "tests/test_ops.py::test_mul"
    }

    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert v.missed == []  # no outcome changed at all...
    assert v.coverage is not None
    assert v.coverage.changed_symbols == ("pkg.ops.mul", "pkg.util.helper")
    assert [(h.runner_id, h.selected, h.executed_changed) for h in v.coverage.affected] == [
        ("tests/test_bridge.py::test_bridge", False, ("pkg.util.helper",)),
        ("tests/test_ops.py::test_mul", True, ("pkg.ops.mul",)),
    ]
    assert [h.runner_id for h in v.coverage.missed] == ["tests/test_bridge.py::test_bridge"]
    assert v.coverage.recall == 0.5 and v.coverage.precision == 1.0
    assert not v.ok  # ...but coverage shows a miss

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
            "--source-root",
            "src",
            "--source-root",
            "tests",
            "--command",
            PYTEST,
            "--coverage",
            "--format",
            "json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    assert data["coverage"]["counts"] == {
        "tests": 3,
        "executed_a_changed_symbol": 2,
        "caught": 1,
        "missed": 1,
    }
    assert data["coverage"]["missed"] == [
        {"runner_id": "tests/test_bridge.py::test_bridge", "executed": ["pkg.util.helper"]}
    ]

    # Text output names the miss and the ratios.
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
            "--source-root",
            "src",
            "--source-root",
            "tests",
            "--command",
            PYTEST,
            "--coverage",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "recall 50% (caught 1, missed 1), precision 100%" in out
    assert "MISSED tests/test_bridge.py::test_bridge: executed pkg.util.helper" in out


def test_symbols_carry_line_ranges(repo):
    base = repo.commit(
        {"m.py": "def f():\n    return 1\n\n\nclass C:\n    def m(self):\n        pass\n"}
    )
    plan = repo.plan(base, base, [])
    symbols = plan.head_index.symbols
    assert symbols["m.f"].line_ranges == ((1, 2),)
    assert symbols["m.C"].line_ranges == ((5, 7),)
    assert symbols["m.C.m"].line_ranges == ((6, 7),)
    assert symbols["m"].line_ranges == ((1, 7),)
    assert symbols["m.C.m"].covers_line(7) and not symbols["m.C.m"].covers_line(2)


# --- regression tests added after code review ------------------------------

MOD = "X = 1\n\n\ndef add(a, b):\n    return a + b\n\n\ndef unrelated():\n    return X\n"
TEST_MOD = (
    "from pkg.ops import add, unrelated\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_unrelated():\n    assert unrelated() >= 1\n"
)


def _covplan(repo, base, head, extra_ini: str = ""):
    return repo.plan(base, head, [], discover_runners=["pytest"])


def test_coverage_attributes_lines_to_the_innermost_symbol(repo):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": MOD, "tests/test_ops.py": TEST_MOD})
    # A module *body* change: only functions referencing module state are
    # affected statically; coverage must not blame every function in the file.
    head = repo.commit({"pkg/ops.py": MOD.replace("X = 1", "X = 2")})
    plan = _covplan(repo, base, head)
    assert {d.target.runner_id for d in plan.decisions if d.selected} == {
        "tests/test_ops.py::test_unrelated"
    }
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert v.coverage is not None and v.coverage.changed_symbols == ("pkg.ops",)
    assert v.coverage.missed == []  # test_add ran add(), not the module's own lines
    assert v.ok

    # A structural class change (method added) counts every method's lines.
    base2 = repo.commit(
        {
            "pkg/ops.py": "class K:\n    def a(self):\n        return 1\n",
            "tests/test_ops.py": (
                "from pkg.ops import K\n\n\ndef test_a():\n    assert K().a() == 1\n"
            ),
        }
    )
    head2 = repo.commit(
        {
            "pkg/ops.py": (
                "class K:\n    def a(self):\n        return 1\n\n"
                "    def b(self):\n        return 2\n"
            )
        }
    )
    plan2 = _covplan(repo, base2, head2)
    v2 = validate_pytest(plan2, repo=repo.path, command=PYTEST, coverage=True)
    hit = next(h for h in v2.coverage.hits if h.runner_id == "tests/test_ops.py::test_a")
    assert "pkg.ops.K" in hit.executed_changed
    assert hit.selected and v2.ok


@pytest.mark.parametrize(
    "coveragerc",
    [
        "[run]\nbranch = True\n",
        "[run]\nrelative_files = True\n",
        "[run]\nbranch = True\nrelative_files = True\n",
    ],
)
def test_coverage_honours_branch_and_relative_files_config(repo, coveragerc):
    base = repo.commit(
        {
            ".coveragerc": coveragerc,
            "pkg/__init__.py": "",
            "pkg/ops.py": MOD,
            "ext/__init__.py": "",
            "ext/bridge.py": "from pkg.ops import add\n\n\ndef via():\n    return add(1, 1)\n",
            "tests/test_bridge.py": (
                "from ext.bridge import via\n\n\ndef test_via():\n    assert via() >= 2\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": MOD.replace("a + b", "a + b + 0")})
    plan = repo.plan(base, head, [], source_roots=["pkg", "tests"], discover_runners=["pytest"])
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert [h.runner_id for h in v.coverage.missed] == ["tests/test_bridge.py::test_via"]
    assert not v.ok


def test_coverage_with_no_contexts_is_an_error_not_ok(repo):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": MOD, "tests/test_ops.py": TEST_MOD})
    head = repo.commit({"pkg/ops.py": MOD.replace("a + b", "b + a")})
    plan = _covplan(repo, base, head)
    with pytest.raises(GitError, match="no per-test contexts"):
        validate_pytest(plan, repo=repo.path, command=f"{PYTEST} -k no_such_test", coverage=True)


def test_coverage_run_keeps_addopts_and_runs_head_once(repo, monkeypatch):
    base = repo.commit(
        {
            "pytest.ini": "[pytest]\naddopts = --ignore=tests/excluded\n",
            "pkg/__init__.py": "",
            "pkg/ops.py": MOD,
            "tests/test_ops.py": TEST_MOD,
            "tests/excluded/test_x.py": (
                "from pkg.ops import add\n\n\ndef test_excluded():\n    assert add(1, 1)\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": MOD.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    calls: list[list[str]] = []
    real_run = execution.subprocess.run

    def counting_run(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(execution.subprocess, "run", counting_run)
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    # The excluded test never runs, so it is neither an outcome nor a coverage miss.
    assert v.ok
    assert all("excluded" not in h.runner_id for h in v.coverage.hits)
    excluded = next(o for o in v.outcomes if "excluded" in o.runner_id)
    assert (excluded.base, excluded.head, excluded.changed) == (None, None, False)
    # base run + head run (with coverage): exactly two pytest invocations.
    pytest_calls = [c for c in calls if "-m" in c and "pytest" in c]
    assert len(pytest_calls) == 2
    assert any("--cov-context=test" in c for c in pytest_calls)
    assert all("addopts=" not in " ".join(c) for c in pytest_calls)


def test_validation_status_label_names_the_failing_check():
    from diffcone.execution import CoverageHit, CoverageValidation, TargetOutcome, Validation

    v = Validation("pytest", ["pytest"])
    assert "validation (pytest): OK" in execution.validation_to_text(v)
    v.coverage = CoverageValidation([CoverageHit("t::a", False, ("pkg.f",))])
    assert "validation (pytest): MISSED (coverage)" in execution.validation_to_text(v)
    v.outcomes = [TargetOutcome("t::b", "PASSED", "FAILED", False)]
    assert "validation (pytest): MISSED (outcome, coverage)" in execution.validation_to_text(v)
    v.coverage = None
    assert "validation (pytest): MISSED (outcome)" in execution.validation_to_text(v)


def test_coverage_credits_every_test_that_runs_a_line(repo):
    """Two tests execute the same changed function; both must be attributed.

    With coverage.py's default sys.monitoring core a line is disabled after
    its first hit, so only the first test would be credited.
    """
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": MOD,
            "ext/__init__.py": "",
            "ext/bridge.py": "from pkg.ops import add\n\n\ndef via():\n    return add(1, 1)\n",
            "tests/test_first.py": (
                "from ext.bridge import via\n\n\ndef test_first():\n    assert via()\n"
            ),
            "tests/test_second.py": (
                "from ext.bridge import via\n\n\ndef test_second():\n    assert via()\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": MOD.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [], source_roots=["pkg", "tests"], discover_runners=["pytest"])
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert [h.runner_id for h in v.coverage.missed] == [
        "tests/test_first.py::test_first",
        "tests/test_second.py::test_second",
    ]

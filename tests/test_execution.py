"""run / validate: executing the selected set and checking it."""

from __future__ import annotations

import json
import os
import sys

import pytest

from diffcone import execution
from diffcone.cli import main
from diffcone.execution import build_command, parse_pytest_verbose, run_selected, validate_pytest
from diffcone.snapshot import GitError
from diffcone.testing import asv_target, changes, py_target

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
    # A module-level constant bound to a literal runs no code at import, so
    # only its readers are selected; coverage must not blame every function
    # in the file either.
    head = repo.commit({"pkg/ops.py": MOD.replace("X = 1", "X = 2")})
    plan = _covplan(repo, base, head)
    assert {d.target.runner_id for d in plan.decisions if d.selected} == {
        "tests/test_ops.py::test_unrelated"
    }
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert v.coverage is not None and v.coverage.changed_symbols == ("pkg.ops.X",)
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


def test_coverage_ignores_project_coverage_config(repo):
    """A project's own coverage config (source/omit excluding tests, branch,
    parallel) must not blind attribution."""
    base = repo.commit(
        {
            ".coveragerc": "[run]\nsource = pkg\nomit = tests/*\nbranch = True\nparallel = True\n",
            "pkg/__init__.py": "",
            "pkg/ops.py": MOD,
            "tests/test_ops.py": TEST_MOD,
        }
    )
    head = repo.commit({"tests/test_ops.py": TEST_MOD.replace("== 3", "== 2 + 1")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert v.coverage.changed_symbols == ("tests.test_ops.test_add",)
    assert [(h.runner_id, h.selected) for h in v.coverage.affected] == [
        ("tests/test_ops.py::test_add", True)
    ]
    assert v.ok


def test_read_coverage_contexts_handles_arcs_and_relative_paths(tmp_path):
    """Branch coverage stores arcs instead of lines, and relative_files
    stores checkout-relative paths; both must be readable."""
    import importlib.util

    import coverage

    from diffcone.execution import read_coverage_contexts

    root = tmp_path / "checkout"
    root.mkdir()
    (root / "mod.py").write_text("def f(x):\n    if x:\n        return 1\n    return 2\n")
    db = tmp_path / ".cov"
    cov = coverage.Coverage(data_file=str(db), branch=True, config_file=False)
    cov.set_option("run:relative_files", True)
    cov.set_option("run:core", "ctrace")
    import os

    old = os.getcwd()
    os.chdir(root)
    try:
        cov.start()
        cov.switch_context("tests/test_m.py::test_f[1]|run")
        spec = importlib.util.spec_from_file_location("mod", root / "mod.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.f(True)
        cov.stop()
        cov.save()
    finally:
        os.chdir(old)
    outside: set[str] = set()
    contexts = read_coverage_contexts(db, root, outside)
    assert outside == set()
    assert set(contexts) == {"tests/test_m.py::test_f"}
    assert {1, 2, 3} <= contexts["tests/test_m.py::test_f"]["mod.py"]


def test_a_coverage_run_with_nothing_in_it_is_an_error_not_ok(repo):
    """Whether the suite selected no test at all or ran without per-test
    contexts, there is nothing to validate against and saying "ok" would be
    a validation that passed by finding nothing."""
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": MOD, "tests/test_ops.py": TEST_MOD})
    head = repo.commit({"pkg/ops.py": MOD.replace("a + b", "b + a")})
    plan = _covplan(repo, base, head)
    with pytest.raises(GitError, match="the suite did not run"):
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


def test_corpus_replays_history_and_aggregates(repo, capsys):
    c1 = repo.commit(
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
    c2 = repo.commit({"pkg/ops.py": OPS.replace("a * b", "b * a")})  # visible, same outcome
    c3 = repo.commit({"README.md": "docs only\n"})  # skipped
    c4 = repo.commit({"data.txt": "4\n", "pkg/ops.py": OPS.replace("b * a", "b * a + 0")})  # miss
    calls: list[list[str]] = []
    real_run = execution.subprocess.run

    def counting_run(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(execution.subprocess, "run", counting_run)
    try:
        report = execution.corpus_validation(
            repo.path,
            f"{c1}..{c4}",
            lambda b, h: repo.plan(b, h, [], discover_runners=["pytest"]),
            command=PYTEST,
            coverage=True,
        )
    finally:
        mp.undo()
    assert [(e.commit, e.skipped is not None, e.error) for e in report.entries] == [
        (c2, False, None),
        (c3, True, None),
        (c4, False, None),
    ]
    e2, _, e4 = report.entries
    assert e2.validation.ok and e2.selected == 1 and e2.targets == 3
    assert e2.validation.coverage.recall == 1.0
    assert not e4.validation.ok
    assert [o.runner_id for o in e4.validation.missed] == ["tests/test_data.py::test_data"]
    assert report.outcome_missed == 1 and report.recall == 1.0 and not report.ok
    assert report.mean_savings == pytest.approx(2 / 3)
    # Each commit's suite ran once: c1, c2 (head, coverage), c3 (base of the
    # next pair: skipped as a commit, but its non-.py change could still alter
    # outcomes, so it is not assumed equal to c2) and c4 (head, coverage).
    suite_runs = [c for c in calls if "-m" in c and "pytest" in c and "-v" in c]
    assert len(suite_runs) == 4
    # Symmetric instrumentation: every run is under coverage when requested.
    assert sum(1 for c in suite_runs if "--cov-context=test" in c) == 4

    code = main(
        [
            "corpus",
            "--repo",
            str(repo.path),
            "--range",
            f"{c1}..{c4}",
            "--discover",
            "pytest",
            "--command",
            PYTEST,
            "--format",
            "json",
        ]
    )
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert code == 1
    assert data["totals"]["validated"] == 2 and data["totals"]["skipped"] == 1
    assert data["totals"]["outcome_missed"] == 1 and data["totals"]["coverage_affected"] == 0
    assert "validating" in err

    code = main(
        [
            "corpus",
            "--repo",
            str(repo.path),
            "--range",
            f"{c1}..{c2}",
            "--discover",
            "pytest",
            "--command",
            PYTEST,
            "--coverage",
            "--max",
            "1",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "corpus" in out and "1 validated" in out and "OK" in out
    assert "recall 100%" in out


def test_removed_tests_are_not_misses_and_additive_changes_are_not_ground_truth(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "tests/test_ops.py": TEST_OPS + "\n\ndef test_gone():\n    assert True\n",
        }
    )
    head = repo.commit(
        {
            "tests/test_ops.py": (
                "import os\n" + TEST_OPS + "\n\n@pytest.mark.parametrize('n', [1])\n"
                "def test_new(n):\n    assert os.sep\n"
            ).replace("import os\nfrom", "import os\nimport pytest\nfrom"),
        }
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    # The module only gained imports: additive, no impact; test_gone was removed.
    assert changes(plan)["tests.test_ops"] == ("imports_added",)  # external imports: no edges
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert [o.runner_id for o in v.removed] == ["tests/test_ops.py::test_gone"]
    assert v.missed == []
    assert [(h.runner_id, h.selected) for h in v.coverage.affected] == [
        ("tests/test_ops.py::test_new", True)
    ]
    assert v.ok
    d = validation_to_dict_local(v)
    assert d["removed"] == ["tests/test_ops.py::test_gone"]
    assert "removed at head (not misses): 1" in execution.validation_to_text(v)


def validation_to_dict_local(v):
    from diffcone.execution import validation_to_dict

    return validation_to_dict(v)


def test_decorators_belong_to_the_decorated_definition(repo):
    base = repo.commit(
        {
            "m.py": (
                "import functools\n\n\n"
                "@functools.lru_cache\n"
                "@functools.wraps(len)\n"
                "def f():\n    return 1\n\n\n"
                "@functools.total_ordering\n"
                "class C:\n    def __lt__(self, o):\n        return True\n"
            )
        }
    )
    symbols = repo.plan(base, base, []).head_index.symbols
    assert symbols["m.f"].line_ranges == ((4, 7),)
    assert symbols["m.C"].line_ranges == ((10, 13),)


def test_validation_runs_with_checkout_source_roots_on_pythonpath(repo, monkeypatch, tmp_path):
    """A src layout: an installed copy elsewhere must not shadow the checkout."""
    base = repo.commit(
        {
            "pytest.ini": "[pytest]\npythonpath = src\n",
            "src/pkg/__init__.py": "",
            "src/pkg/ops.py": OPS,
            "tests/test_ops.py": TEST_OPS,
        }
    )
    head = repo.commit({"src/pkg/ops.py": OPS.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [], source_roots=["src", "tests"], discover_runners=["pytest"])
    seen_env: list[dict] = []
    real_run = execution.subprocess.run

    def capturing_run(argv, **kwargs):
        if "pytest" in argv:
            seen_env.append(dict(kwargs.get("env") or {}))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(execution.subprocess, "run", capturing_run)
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert v.ok and v.coverage.recall == 1.0
    assert len(seen_env) == 2
    for env in seen_env:
        first, second = env["PYTHONPATH"].split(os.pathsep)[:2]
        assert first.endswith("/src") and second.endswith("/tests")


def test_shadowed_files_detects_installed_copies(repo):
    base = repo.commit(
        {"src/pkg/__init__.py": "", "src/pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS}
    )
    plan = repo.plan(base, base, [], source_roots=["src", "tests"])
    outside = {"/site-packages/pkg/ops.py", "/elsewhere/src/pkg/__init__.py", "/unrelated/x.py"}
    assert execution.shadowed_files(plan, outside) == [
        ("/elsewhere/src/pkg/__init__.py", "src/pkg/__init__.py"),
        ("/site-packages/pkg/ops.py", "src/pkg/ops.py"),
    ]


def test_parsers_cope_with_spaces_pipes_and_brackets_in_parameter_ids():
    from diffcone.execution import fold_nodeid

    out = (
        "tests/test_o.py::test_choice[choices4-[TEXT: a|b]] PASSED [ 12%]\n"
        "tests/test_o.py::test_choice[c-x] FAILED [ 50%]\n"
        "tests/test_o.py::test_plain PASSED\n"
    )
    assert parse_pytest_verbose(out) == {
        "tests/test_o.py::test_choice": "FAILED",
        "tests/test_o.py::test_plain": "PASSED",
    }
    context = "tests/test_o.py::test_choice[choices4-[TEXT: a|b]]|run"
    assert fold_nodeid(context.rsplit("|", 1)[0]) == "tests/test_o.py::test_choice"


def test_coverage_validation_instruments_both_snapshots(repo, monkeypatch):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    argvs: list[list[str]] = []
    real_run = execution.subprocess.run

    def capturing(argv, **kwargs):
        if "pytest" in argv:
            argvs.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(execution.subprocess, "run", capturing)
    cache = execution.OutcomeCache()
    validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True, outcome_cache=cache)
    assert [("--cov-context=test" in a) for a in argvs] == [True, True]
    assert set(cache) == {(base, True), (head, True)}
    # A plain validation does not reuse coverage-mode outcomes.
    validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=False, outcome_cache=cache)
    assert (base, False) in cache and len(argvs) == 4


def test_setup_command_runs_in_each_checkout(repo):
    base = repo.commit(
        {
            ".gitignore": "pkg/_version.py\n",
            "pkg/__init__.py": "from pkg._version import version\n",
            "pkg/ops.py": OPS,
            "tests/test_ops.py": TEST_OPS,
        }
    )
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    # Without the generated file the suite cannot import the package at all,
    # and a suite that never ran has no outcomes to compare: that is an error,
    # not a validation that passed because it found no missed outcome change.
    with pytest.raises(GitError, match="the suite did not run"):
        validate_pytest(plan, repo=repo.path, command=PYTEST)
    setup = "printf 'version = \"0.0\"\\n' > pkg/_version.py"
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, setup_command=setup)
    assert {o.head for o in v.outcomes} == {"PASSED"} and v.ok  # the suite ran at both snapshots
    with pytest.raises(GitError, match="setup command failed"):
        validate_pytest(plan, repo=repo.path, command=PYTEST, setup_command="exit 3")


def test_corpus_jobs_matches_serial_and_overlaps_suites(repo, monkeypatch):
    c1 = repo.commit(
        {"pkg/__init__.py": "", "pkg/ops.py": OPS, "data.txt": "3\n", "tests/test_ops.py": TEST_OPS}
    )
    for body in ("b * a", "b + a", "a + b + 0", "a * b * 1"):
        repo.commit(
            {
                "pkg/ops.py": OPS.replace("a + b", body)
                if "+" in body
                else OPS.replace("a * b", body)
            }
        )
    c5 = repo.git("rev-parse", "HEAD")
    make = lambda b, h: repo.plan(b, h, [], discover_runners=["pytest"])  # noqa: E731
    spans: list[tuple[float, float]] = []
    real_run = execution.subprocess.run
    lock = execution.threading.Lock()

    def timed(argv, **kwargs):
        if "pytest" not in argv:
            return real_run(argv, **kwargs)
        import time

        start = time.perf_counter()
        # Pad each suite so overlaps are measurable on a fast fixture.
        result = real_run(argv, **kwargs)
        time.sleep(0.3)
        with lock:
            spans.append((start, time.perf_counter()))
        return result

    monkeypatch.setattr(execution.subprocess, "run", timed)
    serial = execution.corpus_validation(
        repo.path, f"{c1}..{c5}", make, command=PYTEST, coverage=True
    )
    serial_runs = len(spans)
    spans.clear()
    parallel = execution.corpus_validation(
        repo.path, f"{c1}..{c5}", make, command=PYTEST, coverage=True, jobs=2
    )
    got, want = execution.corpus_to_dict(parallel), execution.corpus_to_dict(serial)
    # Report a transient failure (a suite that could not run) as itself, and
    # a real difference per commit: a whole-report diff is unreadable.
    assert [
        (e["commit"], e["error"]) for r in (want, got) for e in r["entries"] if e["error"]
    ] == []
    for expected, actual in zip(want["entries"], got["entries"], strict=True):
        assert actual == expected, f"{expected['commit']}: parallel differs from serial"
    assert got == want
    # Serial: five distinct snapshots, each once. Two jobs over four pairs:
    # workers take second pairs, so at most one extra run per pair...
    assert serial_runs == 5 and 5 <= len(spans) <= 9
    # ...and suites genuinely overlap instead of serialising on the cache.
    ordered = sorted(spans)
    overlaps = sum(1 for a, b in zip(ordered, ordered[1:], strict=False) if b[0] < a[1])
    assert overlaps >= 1


def test_corpus_jobs_aborts_queued_pairs_on_unexpected_errors(repo):
    c1 = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    c2 = repo.commit({"pkg/ops.py": OPS.replace("a * b", "b * a")})
    c3 = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    c4 = repo.commit({"pkg/ops.py": OPS.replace("a + b", "a + b + 0")})
    planned: list[str] = []

    def make(b, h):
        planned.append(h)
        if h == c2:
            raise RuntimeError("boom")
        return repo.plan(b, h, [], discover_runners=["pytest"])

    with pytest.raises(RuntimeError, match="boom"):
        execution.corpus_validation(repo.path, f"{c1}..{c4}", make, command=PYTEST, jobs=1)
    assert planned == [c2] and c3 not in planned  # serial: stops at the first error
    planned.clear()
    with pytest.raises(RuntimeError, match="boom"):
        execution.corpus_validation(repo.path, f"{c1}..{c4}", make, command=PYTEST, jobs=2)
    assert c4 not in planned or len(planned) <= 3  # queued pairs were cancelled


def test_corpus_progress_reports_skipped_commits(repo):
    c1 = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    c2 = repo.commit({"README.md": "docs\n"})
    c3 = repo.commit({"pkg/ops.py": OPS.replace("a * b", "b * a")})
    assert c3 != c2
    seen: list[tuple[str, bool]] = []
    execution.corpus_validation(
        repo.path,
        f"{c1}..{c3}",
        lambda b, h: repo.plan(b, h, [], discover_runners=["pytest"]),
        command=PYTEST,
        progress=lambda e: seen.append((e.commit, e.skipped is not None)),
    )
    assert seen == [(c2, True), (c3, False)]


def test_validate_resolves_a_relative_command_against_the_repo(repo, monkeypatch):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    runner = repo.path / "run-pytest.sh"
    runner.write_text(f'#!/bin/sh\nexec {sys.executable} -m pytest "$@"\n')
    runner.chmod(0o755)
    plan = repo.plan(
        base, head, [py_target("tests/test_ops.py::test_add", "tests.test_ops.test_add")]
    )
    monkeypatch.chdir(repo.path.parent)  # not the repository: the path is resolved through it
    v = validate_pytest(plan, repo=repo.path, command="./run-pytest.sh")
    assert v.missed == []
    with pytest.raises(GitError, match="cannot run './missing.sh'"):
        validate_pytest(plan, repo=repo.path, command="./missing.sh")


def test_relative_command_keeps_a_symlinked_interpreter(tmp_path, monkeypatch):
    """``.venv/bin/python`` is a symlink to the base interpreter; resolving
    it ran the suite outside the venv (no pytest-cov, no dependencies)."""
    from diffcone.execution import resolve_command

    real = tmp_path / "base" / "python3"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    venv = tmp_path / "repo" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(real)
    monkeypatch.chdir(tmp_path / "repo")
    resolved = resolve_command(".venv/bin/python -m pytest", tmp_path / "repo")
    assert resolved == f"{venv / 'python'} -m pytest"


def test_coverage_attributes_symbols_deleted_in_head_from_the_base_run(repo):
    """A test that executed a function deleted in head has no head lines to
    attribute; the base run's coverage supplies them. A test deleted with it
    is removed, not missed."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS + "\n\ndef old(a):\n    return a\n",
            "tests/test_ops.py": (
                "from pkg import ops\n\n\n"
                "def test_old():\n    assert ops.old(1) == 1\n\n\n"
                "def test_gone():\n    assert ops.old(2) == 2\n\n\n"
                "def test_add():\n    assert ops.add(1, 2) == 3\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/ops.py": OPS,
            "tests/test_ops.py": (
                "from pkg import ops\n\n\n"
                "def test_old():\n    assert ops.mul(1, 1) == 1\n\n\n"
                "def test_add():\n    assert ops.add(1, 2) == 3\n"
            ),
        }
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    v = validate_pytest(plan, repo=repo.path, command=PYTEST, coverage=True)
    assert v.ok
    assert v.coverage is not None
    assert "pkg.ops.old" in v.coverage.changed_symbols
    affected = {h.runner_id: h for h in v.coverage.affected}
    assert affected["tests/test_ops.py::test_old"].executed_changed == (
        "pkg.ops.old",
        "tests.test_ops.test_old",
    )
    assert affected["tests/test_ops.py::test_old"].selected
    assert "tests/test_ops.py::test_gone" not in affected  # removed at head
    assert [o.runner_id for o in v.removed] == ["tests/test_ops.py::test_gone"]
    assert "tests/test_ops.py::test_add" not in affected


def test_validate_with_prefixed_roots_in_importlib_mode(repo):
    same_test = "from {pkg} import core\n\n\ndef test_run():\n    assert core.run() == {v}\n"
    base = repo.commit(
        {
            "a/src/pa/__init__.py": "",
            "a/src/pa/core.py": "def run():\n    return 1\n",
            "a/tests/test_core.py": same_test.format(pkg="pa", v=1),
            "b/src/pb/__init__.py": "",
            "b/src/pb/core.py": "def run():\n    return 2\n",
            "b/tests/test_core.py": same_test.format(pkg="pb", v=2),
        }
    )
    head = repo.commit({"b/src/pb/core.py": "def run():\n    return 3\n"})  # outcome flips
    roots = ["a/src", "b/src", "a/tests=a_tests", "b/tests=b_tests"]
    plan = repo.plan(base, head, [], source_roots=roots, discover_runners=["pytest"])
    # The package roots (directory parts of the specs) go on PYTHONPATH; the
    # session collects both same-named test modules in importlib mode.
    v = validate_pytest(plan, repo=repo.path, command=PYTEST + " --import-mode=importlib")
    assert v.ok
    assert [(o.runner_id, o.base, o.head, o.selected) for o in v.outcomes] == [
        ("a/tests/test_core.py::test_run", "PASSED", "PASSED", False),
        ("b/tests/test_core.py::test_run", "PASSED", "FAILED", True),
    ]

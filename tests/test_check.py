"""diffcone check (roadmap item 9): a plan against the JUnit XML of real
pytest runs, so the name mapping is pytest's own."""

from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from diffcone import check as checking
from diffcone.cli import main
from diffcone.report import to_dict

OPS = """\
def add(a, b):
    return a + b


def neg(a):
    return -a
"""

TESTS = """\
import pytest

from pkg.ops import add, neg


def test_add():
    assert add(1, 2) == 3


class TestNeg:
    @pytest.mark.parametrize("value", [1, 2])
    def test_neg(self, value):
        assert neg(value) == -value

    class TestInner:
        def test_inner(self):
            assert neg(0) == 0


def test_always_broken():
    assert False
"""

T = "tests/test_ops.py::"
ADD, NEG, INNER, BROKEN = (
    T + "test_add",
    T + "TestNeg::test_neg",
    T + "TestNeg::TestInner::test_inner",
    T + "test_always_broken",
)
IMPORTS = "tests/test_imports.py::test_imports"


def _junit(repo, name, *args):
    out = repo.path / f"{name}.xml"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={out}"]
        + ["--continue-on-collection-errors", *args],
        cwd=repo.path,
        capture_output=True,
    )
    return checking.read_junit(out)


@pytest.fixture
def runs(repo):
    """A plan base -> head, and JUnit from full runs at both: head breaks
    add (test_add), one parameter of neg, and test_imports' import (a
    collection error); test_always_broken fails on both sides."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "tests/__init__.py": "",
            "tests/test_ops.py": TESTS,
            "tests/test_imports.py": "def test_imports():\n    pass\n",
        }
    )
    baseline = _junit(repo, "base")
    head = repo.commit(
        {
            "pkg/ops.py": OPS.replace("a + b", "a - b").replace(
                "return -a", "return -a if a < 2 else a"
            ),
            "tests/test_imports.py": "import not_a_module\n\n\ndef test_imports():\n    pass\n",
        }
    )
    full = _junit(repo, "head")
    plan = to_dict(repo.plan(base, head, [], discover_runners=["pytest"]))
    return plan, full, baseline


def _drop(plan, *tests):
    """The plan with ``tests`` moved from selected to unselected."""
    plan = copy.deepcopy(plan)
    keep = [t for t in plan["selected_targets"] if t["runner_id"] not in tests]
    plan["unselected_targets"] += [t for t in plan["selected_targets"] if t["runner_id"] in tests]
    plan["selected_targets"] = keep
    return plan


def test_junit_cases_map_back_to_the_plans_targets(runs):
    plan, full, baseline = runs
    selected = {t["runner_id"] for t in plan["selected_targets"]}
    assert {ADD, NEG, IMPORTS} <= selected
    report = checking.check(plan, full, baseline=baseline)
    # Parameters fold into their target, nested classes keep their path, and
    # the collection error stands for its file.
    got = {(f.test, f.outcome, f.kind, f.already) for f in report.failures}
    assert got == {
        (ADD, "failed", "test", False),
        (NEG, "failed", "test", False),
        ("tests.test_imports", "error", "collection", False),
        (BROKEN, "failed", "test", True),
    }
    assert report.unmatched == 0 and report.ok
    assert report.targets == 5


def test_a_failure_the_plan_did_not_select_is_a_miss(runs):
    plan, full, baseline = runs
    report = checking.check(_drop(plan, ADD, IMPORTS, BROKEN), full, baseline=baseline)
    assert [(f.test, f.kind) for f in report.misses] == [
        ("tests.test_imports", "collection"),
        (ADD, "test"),
    ]
    # A failure the baseline had too is not one.
    assert not report.ok
    # Without a baseline, every failure counts.
    assert BROKEN in {f.test for f in checking.check(_drop(plan, BROKEN), full).misses}
    # A failing test the plan does not know at all is a miss too.
    unknown = copy.deepcopy(plan)
    unknown["selected_targets"] = [t for t in plan["selected_targets"] if t["runner_id"] != ADD]
    report = checking.check(unknown, full, baseline=baseline)
    assert [(f.test, f.kind) for f in report.misses] == [("tests.test_ops::test_add", "unknown")]
    assert report.unmatched == 1


def test_selective_runs_are_compared_on_the_same_failures(repo, runs):
    plan, full, baseline = runs
    # A selector that ran only TestNeg (as another tool might have chosen).
    partial = _junit(repo, "partial", "tests/test_ops.py::TestNeg")
    report = checking.check(plan, full, baseline=baseline, runs={"other": partial})
    (run,) = report.runs
    assert run.ran == 2  # TestNeg::test_neg and TestNeg::TestInner::test_inner
    assert run.misses == ["tests.test_imports", ADD]
    assert run.disagreements == []
    markdown = checking.to_markdown(report)
    assert "### diffcone check: no miss" in markdown and "| other run |" in markdown


def test_the_cli_exits_1_on_a_miss_and_2_on_unreadable_input(repo, runs, tmp_path, capsys):
    plan, _full, _baseline = runs
    ok, missed = tmp_path / "plan.json", tmp_path / "missed.json"
    import json

    ok.write_text(json.dumps(plan))
    missed.write_text(json.dumps(_drop(plan, ADD)))
    head, base = str(repo.path / "head.xml"), str(repo.path / "base.xml")
    assert main(["check", "--plan", str(ok), "--full", head, "--baseline", base]) == 0
    assert "OK: every new failure was selected" in capsys.readouterr().out
    assert main(["check", "--plan", str(missed), "--full", head, "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["misses"] == [ADD]
    assert main(["check", "--plan", str(ok), "--full", str(tmp_path / "none.xml")]) == 2
    assert main(["check", "--plan", head, "--full", head]) == 2
    assert main(["check", "--plan", str(ok), "--full", head, "--run", "nameless"]) == 2

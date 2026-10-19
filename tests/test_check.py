"""diffcone check (roadmap item 9): a plan against the JUnit XML of real
pytest runs, so the name mapping is pytest's own."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from diffcone import check as checking
from diffcone.cli import main
from diffcone.report import to_dict
from diffcone.testing import FixtureRepo

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


def test_a_baseline_failure_of_one_parameter_does_not_excuse_another(runs):
    """Head breaks ``test_neg[2]``; a baseline where only ``test_neg[1]``
    failed must not make that an already-failing test (audit CI-1)."""
    plan, full, _baseline = runs

    def other_case_failed(case):
        if case.name.startswith("test_neg["):
            failed = case.name == "test_neg[1]"
            return checking.Case(case.classname, case.name, "failed" if failed else "passed")
        return checking.Case(case.classname, case.name, "passed")

    baseline = [other_case_failed(c) for c in full]
    assert {c.name for c in full if c.outcome == "failed" and "neg" in c.name} == {"test_neg[2]"}
    report = checking.check(_drop(plan, NEG), full, baseline=baseline)
    assert [f.test for f in report.misses] == [NEG]
    # The same case failing at the baseline is already failing.
    same = [
        checking.Case(c.classname, c.name, c.outcome if c.name == "test_neg[2]" else "passed")
        for c in full
    ]
    report = checking.check(_drop(plan, NEG), full, baseline=same)
    assert NEG not in {f.test for f in report.misses}


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


@pytest.mark.parametrize("command", ["check", "report"])
def test_a_crash_exits_2_not_the_miss_code(command, tmp_path, monkeypatch, capsys):
    """An unexpected exception in check or report must not exit 1, which
    CI reads as a miss or a failed check and parses a verdict for (audit
    CI-4)."""
    from diffcone import ci_report

    def crash(*_args, **_kwargs):
        raise LookupError("unknown encoding: bogus")

    monkeypatch.setattr(checking, "read_junit", crash)
    monkeypatch.setattr(ci_report, "load", crash)
    plan = tmp_path / "plan.json"
    plan.write_text('{"selected_targets": []}')
    argv = {
        "check": ["check", "--plan", str(plan), "--full", str(plan)],
        "report": ["report", "--dir", str(tmp_path)],
    }[command]
    assert main(argv) == 2
    assert "internal error" in capsys.readouterr().err


# --------------------------------------------------------------------------- runs and summary


def test_r6_a_failure_diffcones_run_did_not_run_fails_the_check(tmp_path):
    from diffcone.check import check, load_plan, read_junit

    plan = {
        "status": "complete",
        "discovery_incomplete": False,
        "selected_targets": [{"runner": "pytest", "runner_id": "tests/test_a.py::test_x"}],
        "unselected_targets": [],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "full.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a" name="test_x">'
        '<failure message="boom"/></testcase></testsuite></testsuites>'
    )
    (tmp_path / "dc.xml").write_text("<testsuites><testsuite/></testsuites>")
    report = check(
        load_plan(tmp_path / "plan.json"),
        read_junit(tmp_path / "full.xml"),
        runs={"diffcone": read_junit(tmp_path / "dc.xml")},
    )
    assert report.not_run == ["tests/test_a.py::test_x"]
    assert not report.ok


def test_f8_a_miss_is_counted_once_in_the_summary(tmp_path):
    from diffcone.check import check, load_plan, read_junit, to_markdown

    plan = {
        "status": "complete",
        "discovery_incomplete": False,
        "selected_targets": [],
        "unselected_targets": [{"runner": "pytest", "runner_id": "tests/test_a.py::test_x"}],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "full.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a" name="test_x">'
        '<failure message="boom"/></testcase></testsuite></testsuites>'
    )
    (tmp_path / "dc.xml").write_text("<testsuites><testsuite/></testsuites>")
    report = check(
        load_plan(tmp_path / "plan.json"),
        read_junit(tmp_path / "full.xml"),
        runs={"diffcone": read_junit(tmp_path / "dc.xml")},
    )
    assert "**1 missed**" in to_markdown(report)


def test_t16_without_diffcones_run_the_verdict_says_only_the_plan_was_checked(tmp_path):
    """The check action passes ``--run diffcone=...`` only when the run's
    JUnit is there; without it the check compares the plan alone, yet said
    every new failure was "selected and run"."""
    from diffcone.check import check, read_junit, to_dict, to_markdown, to_text

    plan = {
        "status": "complete",
        "selected_targets": [{"runner": "pytest", "runner_id": "tests/test_a.py::test_x"}],
        "unselected_targets": [],
    }
    (tmp_path / "full.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a" name="test_x">'
        '<failure message="boom"/></testcase></testsuite></testsuites>'
    )
    full = read_junit(tmp_path / "full.xml")
    alone = check(plan, full)
    assert alone.ok and not alone.own_run_checked
    text = to_text(alone)
    assert "selected and run" not in text
    assert "whether diffcone's run ran them was not checked" in text
    assert "### diffcone check: no miss in the plan (diffcone's run not checked)" in (
        to_markdown(alone)
    )
    assert to_dict(alone)["own_run_checked"] is False
    ran = check(plan, full, runs={"diffcone": full})
    assert ran.ok and ran.own_run_checked
    assert "OK: every new failure was selected and run" in to_text(ran)
    assert "### diffcone check: no miss\n" in to_markdown(ran)


# --------------------------------------------------------------------------- rootdir and prefix


PY = sys.executable
DIFFCONE = shutil.which("diffcone", path=str(Path(sys.executable).parent)) or str(
    Path(sys.executable).parent / "diffcone"
)
PYTEST = f"{PY} -m pytest"


ADD_OPS = "def add(a, b):\n    return a + b\n"
ADD_OPS_BROKEN = "def add(a, b):\n    return a + b + 1\n"


def _cli(repo: FixtureRepo, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DIFFCONE, *args], cwd=cwd or repo.path, capture_output=True, text=True, check=False
    )


def _pytest(repo: FixtureRepo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )


def _check(repo: FixtureRepo, plan: Path, full: Path, *extra: str) -> tuple[int, dict]:
    proc = _cli(repo, "check", "--plan", str(plan), "--full", str(full), "--format", "json", *extra)
    return proc.returncode, json.loads(proc.stdout)


MONOREPO = {
    "README.md": "monorepo\n",
    "python/pytest.ini": "[pytest]\ntestpaths = tests\n",
    "python/pkg/__init__.py": "",
    "python/pkg/ops.py": ADD_OPS,
    "python/tests/__init__.py": "",
    "python/tests/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n\n\n"
        "def test_other():\n    assert True\n"
    ),
}
MONOREPO_ADD, MONOREPO_OTHER = (
    "python/tests/test_ops.py::test_add",
    "python/tests/test_ops.py::test_other",
)


@pytest.mark.parametrize(
    "command",
    [PYTEST, f"{PYTEST} -c python/pytest.ini --rootdir python"],
    ids=["found", "given"],
)
def test_r4_rootdir_below_the_repository_root(repo, tmp_path, command):
    """pytest's rootdir is ``python/`` (its ini file is there), so JUnit
    class names and node ids are relative to it while the plan names tests
    relative to the repository. ``check`` maps through the rootdir the
    plan's discovery determined, and ``validate`` compares
    repository-relative ids: no false miss, no false verdict."""
    base = repo.commit(MONOREPO)
    head = repo.commit({"python/pkg/ops.py": ADD_OPS_BROKEN})
    plan = tmp_path / "plan.json"
    proc = _cli(
        repo,
        "plan",
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
        "--source-root",
        "python",
        "--command",
        command,
        "-o",
        str(plan),
        "--",
        "python",
    )
    assert proc.returncode == 3, proc.stderr  # the nested ini is reported, not read
    data = json.loads(plan.read_text("utf-8"))
    assert [t["runner_id"] for t in data["selected_targets"]] == [MONOREPO_ADD]
    config = data["discovery"][0]["config"]
    assert config["rootdir"] == "python" and config["configfile"] == "python/pytest.ini"
    notes = {n["kind"] for n in data["discovery"][0]["notes"]}
    assert "unmodelled_runner_option" in notes

    full = tmp_path / "full.xml"
    _pytest(repo, *command.split()[3:], "python", f"--junitxml={full}")
    assert 'classname="tests.test_ops"' in full.read_text("utf-8")
    code, verdict = _check(repo, plan, full)
    assert code == 0, verdict
    assert verdict["full_run"]["unmatched_cases"] == 0
    assert verdict["mapping"] == {"rootdir": "python", "junit_prefix": "", "note": None}
    assert [(f["test"], f["kind"], f["selected"]) for f in verdict["failures"]] == [
        (MONOREPO_ADD, "test", True)
    ]
    # Mapped with the wrong rootdir, nothing matches and the report says so.
    code, verdict = _check(repo, plan, full, "--rootdir", ".")
    assert code == 1 and verdict["full_run"]["unmatched_cases"] == 2
    assert "--rootdir" in verdict["mapping"]["note"]

    proc = _cli(
        repo,
        "validate",
        "--base",
        base,
        "--head",
        head,
        "--discover",
        "pytest",
        "--source-root",
        "python",
        "--command",
        f"{command} python",
        "--format",
        "json",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert {o["runner_id"] for o in verdict["outcomes"]} == {MONOREPO_ADD, MONOREPO_OTHER}
    assert [(o["runner_id"], o["head"]) for o in verdict["caught"]] == [(MONOREPO_ADD, "FAILED")]
    assert verdict["missed"] == []


COLLECTION = {
    "pkg/__init__.py": "",
    "pkg/ops.py": ADD_OPS,
    "tests/__init__.py": "",
    "tests/test_top.py": "def test_top():\n    assert True\n",
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef one():\n    return 1\n",
    "tests/sub/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add(one):\n    assert add(one, 1) == 2\n"
    ),
}
SUB_ADD, TOP = "tests/sub/test_ops.py::test_add", "tests/test_top.py::test_top"
# The fixture's body changes (which selects its tests) and the conftest no
# longer imports (an added import alone is additive by the documented rule).
BROKEN_CONFTEST = {
    "tests/sub/conftest.py": (
        "import pytest\nimport nosuchmodule\n\n\n@pytest.fixture\ndef one():\n    return 2\n"
    )
}


def _plan_file(repo: FixtureRepo, base: str, head: str, out: Path, *extra: str) -> dict:
    proc = _cli(
        repo, "plan", "--base", base, "--head", head, "--discover", "pytest", "-o", str(out), *extra
    )
    assert proc.returncode in (0, 3), proc.stderr
    return json.loads(out.read_text("utf-8"))


def _unselect(plan_path: Path, *tests: str) -> None:
    data = json.loads(plan_path.read_text("utf-8"))
    moved = [t for t in data["selected_targets"] if t["runner_id"] in tests]
    data["selected_targets"] = [t for t in data["selected_targets"] if t not in moved]
    data["unselected_targets"] += moved
    plan_path.write_text(json.dumps(data), "utf-8")


def test_r5_a_directory_that_fails_to_collect_is_a_collection_failure_of_its_tests(repo, tmp_path):
    """A ``conftest.py`` that no longer imports makes its directory a
    collector error (``classname="" name="tests.sub"``): a collection
    failure of every target under it, caught when any is selected, a miss
    of them all otherwise."""
    base = repo.commit(COLLECTION)
    head = repo.commit(BROKEN_CONFTEST)
    plan = tmp_path / "plan.json"
    data = _plan_file(repo, base, head, plan)
    assert [t["runner_id"] for t in data["selected_targets"]] == [SUB_ADD]
    assert [t["runner_id"] for t in data["unselected_targets"]] == [TOP]
    full = tmp_path / "full.xml"
    _pytest(repo, "--continue-on-collection-errors", f"--junitxml={full}")
    assert 'classname="" name="tests.sub"' in full.read_text("utf-8")
    code, verdict = _check(repo, plan, full)
    assert code == 0, verdict
    assert [(f["test"], f["kind"], f["selected"]) for f in verdict["failures"]] == [
        ("tests.sub", "collection", True)
    ]
    _unselect(plan, SUB_ADD)
    code, verdict = _check(repo, plan, full)
    assert code == 1 and verdict["misses"] == ["tests.sub"]
    assert verdict["failures"][0]["kind"] == "collection"


def test_r5_junit_prefix_from_the_plan_or_the_option(repo, tmp_path):
    """``--junit-prefix`` puts a prefix before every class name (alone on
    a collector error): taken off when the plan's pytest arguments carry
    it, or when ``check --junit-prefix`` says so."""
    base = repo.commit(COLLECTION)
    head = repo.commit(BROKEN_CONFTEST)
    full = tmp_path / "full.xml"
    _pytest(repo, "--continue-on-collection-errors", "--junit-prefix=ci", f"--junitxml={full}")
    xml = full.read_text("utf-8")
    assert 'classname="ci" name="tests.sub"' in xml and 'classname="ci.tests.test_top"' in xml

    carried = tmp_path / "carried.json"
    data = _plan_file(repo, base, head, carried, "--", "--junit-prefix=ci")
    assert data["discovery"][0]["config"]["junit_prefix"] == "ci"
    code, verdict = _check(repo, carried, full)
    assert code == 0 and verdict["full_run"]["unmatched_cases"] == 0
    assert verdict["mapping"]["junit_prefix"] == "ci"
    assert [(f["test"], f["kind"]) for f in verdict["failures"]] == [("tests.sub", "collection")]

    plain = tmp_path / "plain.json"
    _plan_file(repo, base, head, plain)
    code, verdict = _check(repo, plain, full, "--junit-prefix", "ci")
    assert code == 0 and verdict["full_run"]["unmatched_cases"] == 0
    # Without either, nothing matches and the report points at the option.
    code, verdict = _check(repo, plain, full)
    assert code == 1 and verdict["full_run"]["unmatched_cases"] == 2
    assert "--junit-prefix" in verdict["mapping"]["note"]

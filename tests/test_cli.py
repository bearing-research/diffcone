from __future__ import annotations

import json

from diffcone.cli import main
from diffcone.report import to_dict, to_text

OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_OPS = (
    "from pkg.ops import add, mul\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)
TARGETS = [
    {
        "runner": "pytest",
        "runner_id": "tests/test_ops.py::test_add",
        "entry_symbol": "tests.test_ops.test_add",
    },
    {
        "runner": "asv",
        "runner_id": "bench.time_mul",
        "entry_symbol": "tests.test_ops.test_mul",
        "lifecycle_dependencies": [],
    },
]


def test_plan_json_output(repo, capsys):
    base = repo.commit({"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a + b", "b + a")})
    manifest = repo.write_manifest(TARGETS)

    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--targets",
            str(manifest),
            "--format",
            "json",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    report = json.loads(out)
    assert report["status"] == "complete"
    assert report["analysis"]["working_tree_analyzed"] is False
    assert report["analysis"]["base"]["commit"] == base
    assert report["analysis"]["head"]["commit"] == head
    assert [c["id"] for c in report["changed_symbols"]] == ["pkg.ops.add"]
    assert [t["runner_id"] for t in report["selected_targets"]] == ["tests/test_ops.py::test_add"]
    assert [t["runner_id"] for t in report["unselected_targets"]] == ["bench.time_mul"]
    explanation = report["dependency_explanations"][0]["reasons"][0]
    assert explanation["rule"] == "dependency"
    assert explanation["changed_symbol"] == "pkg.ops.add"
    assert [s["kind"] for s in explanation["path"]] == ["entry", "references"]
    for key in ("unresolved_relationships", "fallback_decisions", "analysis_errors"):
        assert key in report

    # Deterministic: a second run yields byte-identical output.
    main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--targets",
            str(manifest),
            "--format",
            "json",
        ]
    )
    assert capsys.readouterr().out == out


def test_plan_text_output_and_degraded_exit_code(repo, capsys):
    base = repo.commit({"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/broken.py": "def (:\n"})
    manifest = repo.write_manifest(TARGETS)
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--targets",
            str(manifest),
            "--format",
            "text",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "status: DEGRADED" in out
    assert "analysis_error (all_targets)" in out
    assert "selected targets (2)" in out


def test_bad_revision_and_bad_manifest(repo, capsys):
    base = repo.commit({"pkg/ops.py": OPS})
    manifest = repo.write_manifest(TARGETS)
    assert (
        main(
            [
                "plan",
                "--repo",
                str(repo.path),
                "--base",
                base,
                "--head",
                "nope",
                "--targets",
                str(manifest),
            ]
        )
        == 2
    )
    assert "error" in capsys.readouterr().err
    bad = repo.path.parent / "bad.json"
    bad.write_text('{"targets": [{"runner": "pytest"}]}')
    assert (
        main(
            [
                "plan",
                "--repo",
                str(repo.path),
                "--base",
                base,
                "--head",
                base,
                "--targets",
                str(bad),
            ]
        )
        == 2
    )
    assert "runner_id" in capsys.readouterr().err


def test_report_helpers_round_trip(repo):
    base = repo.commit({"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    head = repo.commit({"pkg/ops.py": OPS.replace("a * b", "b * a")})
    plan = repo.plan(base, head, TARGETS)
    d = to_dict(plan)
    assert d["analysis"]["counts"]["selected"] == 1
    text = to_text(plan)
    assert "pkg.ops.mul [function] body_changed" in text
    assert "asv: bench.time_mul" in text


def test_unwritable_output_and_non_utf8_manifest(repo, capsys, tmp_path):
    base = repo.commit({"pkg/ops.py": OPS, "tests/test_ops.py": TEST_OPS})
    manifest = repo.write_manifest(TARGETS)
    missing_dir = tmp_path / "no" / "such" / "dir" / "out.json"
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            base,
            "--targets",
            str(manifest),
            "--output",
            str(missing_dir),
        ]
    )
    assert code == 2
    assert "cannot write" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"targets": []}\xff')
    code = main(
        ["plan", "--repo", str(repo.path), "--base", base, "--head", base, "--targets", str(bad)]
    )
    assert code == 2
    assert "manifest" in capsys.readouterr().err


INCOMPLETE_TREE = {
    "pkg/__init__.py": "",
    "pkg/ops.py": "def add(a, b):\n    return a + b\n",
    "tests/test_ops.py": (
        "from pkg.ops import add\n\n\n"
        "class TestAdd:\n    def test_two(self):\n        assert add(1, 1) == 2\n\n\n"
        "class AddRoundTripTest:\n"
        "    def test_round(self):\n        assert add(2, 2) == 4\n"
    ),
}


def test_incomplete_discovery_exits_three_and_run_refuses(repo, capsys):
    """A class a runner plugin may collect is not a target, so the plan is not
    a judgement on it: ``plan`` exits 3 and ``run`` refuses to skip it."""
    base = repo.commit(INCOMPLETE_TREE)
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    args = ["--repo", str(repo.path), "--base", base, "--head", head, "--discover", "pytest"]

    code = main(["plan", *args, "--format", "text"])
    out = capsys.readouterr().out
    assert code == 3
    assert "status: complete" in out
    assert "discovery INCOMPLETE: 1 place(s)" in out
    assert "uncollected_test_class" in out

    code = main(["plan", *args, "--format", "json"])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["discovery_incomplete"] is True

    code = main(["run", *args, "--dry-run", "--command", "pytest"])
    err = capsys.readouterr().err
    assert code == 3
    assert "may collect tests that are not targets" in err
    assert "--allow-incomplete-discovery" in err

    code = main(["run", *args, "--dry-run", "--allow-incomplete-discovery", "--command", "pytest"])
    captured = capsys.readouterr()
    assert code == 0
    assert "tests/test_ops.py::TestAdd::test_two" in captured.out


def test_a_complete_plan_still_exits_zero(repo, capsys):
    tree = dict(INCOMPLETE_TREE)
    tree["tests/test_ops.py"] = (
        "from pkg.ops import add\n\n\nclass TestAdd:\n"
        "    def test_two(self):\n        assert add(1, 1) == 2\n"
    )
    base = repo.commit(tree)
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    args = ["--repo", str(repo.path), "--base", base, "--head", head, "--discover", "pytest"]
    assert main(["plan", *args, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["discovery_incomplete"] is False


def test_run_refuses_a_working_tree_that_is_not_what_was_planned(repo, capsys):
    """``run`` executes the checkout, so a plan made from a commit only
    describes what will run while the checkout is that code."""
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": "def add(a, b):\n    return b + a\n"})
    args = ["--repo", str(repo.path), "--base", base, "--head", head, "--discover", "pytest"]
    # The checkout is the analysed commit: nothing to complain about.
    assert main(["run", *args, "--dry-run", "--command", "pytest"]) == 0

    (repo.path / "pkg" / "ops.py").write_text("def add(a, b):\n    return b + a + 1\n", "utf-8")
    code = main(["run", *args, "--dry-run", "--command", "pytest"])
    err = capsys.readouterr().err
    assert code == 2
    assert "differs in 1 file(s) the plan would select on" in err
    assert "pkg/ops.py" in err and "--allow-mismatched-worktree" in err

    code = main(["run", *args, "--dry-run", "--allow-mismatched-worktree", "--command", "pytest"])
    assert code == 0

    # Any file under the roots is one the plan would select on (a committed
    # change to it selects everything), not only Python; bytecode is not.
    (repo.path / "pkg" / "ops.py").write_text("def add(a, b):\n    return b + a\n", "utf-8")
    (repo.path / "pkg" / "__pycache__").mkdir()
    (repo.path / "pkg" / "__pycache__" / "ops.cpython-313.pyc").write_bytes(b"")
    assert main(["run", *args, "--dry-run", "--command", "pytest"]) == 0
    (repo.path / "notes.md").write_text("hello\n", "utf-8")
    code = main(["run", *args, "--dry-run", "--command", "pytest"])
    assert code == 2 and "notes.md" in capsys.readouterr().err


# --------------------------------------------------------------------------- exit codes


def test_x1_a_bad_argument_exits_2_not_1(repo, capsys):
    repo.commit({"pkg/__init__.py": ""})
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            "HEAD",
            "--head",
            "HEAD",
            "--source-root",
            "src=",
            "--discover",
            "pytest",
        ]
    )
    assert code == 2


def test_x1_a_missing_runner_command_exits_2(repo, capsys):
    base = repo.commit({"tests/__init__.py": "", "tests/test_a.py": "def test_a():\n    pass\n"})
    head = repo.commit({"tests/test_a.py": "def test_a():\n    assert True\n"})
    code = main(
        [
            "run",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--discover",
            "pytest",
            "--allow-mismatched-worktree",
            # A command naming no pytest program makes discovery incomplete
            # (its pytest arguments cannot be found): allowed, to reach it.
            "--allow-incomplete-discovery",
            "--command",
            "no-such-pytest-command",
        ]
    )
    assert code == 2


def test_x2_discover_reports_an_incomplete_target_list_and_the_plan_keeps_it(
    repo, tmp_path, capsys
):
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/test_ok.py": "def test_ok():\n    pass\n",
            "tests/test_bad.py": "def test_bad(:\n    pass\n",
        }
    )
    out = tmp_path / "targets.json"
    code = main(
        [
            "discover",
            "--repo",
            str(repo.path),
            "--rev",
            "HEAD",
            "--discover",
            "pytest",
            "-o",
            str(out),
        ]
    )
    assert code == 3
    data = json.loads(out.read_text())
    assert any(n["kind"] == "unparsed_file" for n in data["discovery"]["runners"][0]["notes"])
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            base,
            "--targets",
            str(out),
            "-o",
            str(tmp_path / "plan.json"),
        ]
    )
    assert code == 3


def test_x3_a_source_root_with_no_python_file_degrades_the_plan(repo, tmp_path, capsys):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": "def f():\n    return 1\n"})
    head = repo.commit({"pkg/ops.py": "def f():\n    return 2\n"})
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--source-root",
            "nonexistent",
            "--discover",
            "pytest",
            "-o",
            str(tmp_path / "plan.json"),
        ]
    )
    assert code == 1
    report = json.loads((tmp_path / "plan.json").read_text())
    assert report["status"] == "degraded"
    assert any("nonexistent" in e["message"] for e in report["analysis_errors"])


def test_r3_r7_run_exit_codes():
    from diffcone.cli import _run_exit_code

    assert _run_exit_code(0, missing=True) == 3  # a selected test never ran
    assert _run_exit_code(5, missing=True) == 3
    assert _run_exit_code(1, missing=True) == 1  # the runner's failure wins
    assert _run_exit_code(5, missing=False) == 0  # every selected test skipped at import
    assert _run_exit_code(1, missing=False) == 1


def test_r4_a_repo_subdirectory_is_refused(repo, capsys):
    repo.commit({"proj/calc/__init__.py": "", "proj/calc/ops.py": "X = 1\n"})
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path / "proj"),
            "--base",
            "HEAD",
            "--head",
            "HEAD",
            "--discover",
            "pytest",
        ]
    )
    assert code == 2
    assert "--source-root proj" in capsys.readouterr().err


# --------------------------------------------------------------------------- plan and discover


def test_f7_plan_reads_the_runs_pytest_arguments(repo, tmp_path, capsys):
    rev = repo.commit(
        {"calc/__init__.py": "", "calc/ops.py": "def f():\n    '''\n    >>> 1\n    1\n    '''\n"}
    )
    out = tmp_path / "plan.json"
    main(["plan", "--repo", str(repo.path), "--base", rev, "--head", rev, "--discover",
          "pytest", "-o", str(out), "--", "--doctest-modules"])  # fmt: skip
    ids = {t["runner_id"] for t in json.loads(out.read_text())["unselected_targets"]}
    assert "calc/ops.py::calc.ops.f" in ids


ADD_OPS = "def add(a, b):\n    return a + b\n"
FILES = {
    "pkg/__init__.py": "",
    "pkg/ops.py": ADD_OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
    ),
    "benchmarks/asv.conf.json": '{"benchmark_dir": "benchmarks"}\n',
    "benchmarks/benchmarks/__init__.py": "",
    "benchmarks/benchmarks/bench_ops.py": (
        "from pkg.ops import add\n\n\n"
        "class Ops:\n    def time_add(self):\n        return add(1, 1)\n"
    ),
}


def test_discover_uses_the_caches_and_they_change_nothing(repo, tmp_path, capsys):
    repo.commit(FILES)
    cache = tmp_path / "cache"
    common = ["discover", "--repo", str(repo.path), "--discover", "pytest", "--discover", "asv"]

    def run(*extra: str) -> dict:
        assert main([*common, *extra]) == 0
        return json.loads(capsys.readouterr().out)

    uncached = run("--no-cache")
    assert not cache.exists()
    cold = run("--cache-dir", str(cache))
    # The index, the module records and the discovery results were stored.
    assert any((cache / "index").iterdir()) and any((cache / "discovery").iterdir())
    assert (cache / "modules.sqlite").exists()
    warm = run("--cache-dir", str(cache))
    assert uncached == cold == warm
    assert {t["runner"] for t in warm["targets"]} == {"pytest", "asv"}

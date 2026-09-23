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

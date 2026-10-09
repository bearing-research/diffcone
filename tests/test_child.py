"""The child recorder's own rules (child.py, roadmap item 15), on records
written by hand: what a spawn resolves to, and what counts as the
interpreter's entry. The scenarios in test_evidence_scenarios.py run real
children."""

from __future__ import annotations

import json
import sys

import pytest

from diffcone import child


def _write(out, spawner, pid, start, argv, *lines):
    directory = out / child.CHILDREN / str(spawner)
    directory.mkdir(parents=True, exist_ok=True)
    header = {"pid": pid, "ppid": spawner, "argv": argv, "start": start, "entry": ["code"]}
    text = "".join(json.dumps(v) + "\n" for v in (header, *lines))
    (directory / f"{pid}-{start}.jsonl").write_text(text)


PY = "/venv/bin/python3"


def test_a_python_spawn_resolves_to_its_own_record(tmp_path):
    _write(tmp_path, 10, 11, 5, [PY, "-m", "pkg.tool"], ["c", "pkg/tool.py", 1, "<module>"])
    tree = child.resolve(tmp_path, 10, 11, 5, [PY, "-m", "pkg.tool"])
    assert [r.codes for r in tree.records] == [[("pkg/tool.py", 1, "<module>")]]
    assert tree.problems == []


def test_a_record_older_than_the_spawn_is_another_process(tmp_path):
    """The pid was reused: the record began before this spawn."""
    _write(tmp_path, 10, 11, 5, [PY, "-m", "pkg.tool"])
    tree = child.resolve(tmp_path, 10, 11, 9, [PY, "-m", "pkg.tool"])
    assert tree.records == []
    assert tree.problems


def test_a_process_that_became_python_another_way_is_a_problem(tmp_path):
    _write(tmp_path, 10, 11, 5, [PY, "-m", "pkg.tool"])
    tree = child.resolve(tmp_path, 10, 11, 5, ["/bin/sh", "-c", "cp a b; exec python -m pkg.tool"])
    assert any("ran other code before Python" in p for p in tree.problems)


def test_a_console_script_is_its_interpreter(tmp_path):
    _write(tmp_path, 10, 11, 5, [PY, "/venv/bin/tool", "-x"])
    assert child.resolve(tmp_path, 10, 11, 5, ["tool", "-x"]).problems == []


def test_a_launcher_must_have_run_its_command(tmp_path):
    uv = ["/bin/uv", "run", "--directory", "nb", "python", "harness.py", "m"]
    # Another Python under it (a build backend), not the command.
    _write(tmp_path, 20, 21, 5, [PY, "-c", "build"])
    assert child.resolve(tmp_path, 10, 20, 5, uv).problems
    _write(tmp_path, 20, 22, 6, [PY, "harness.py", "m"])
    tree = child.resolve(tmp_path, 10, 20, 5, uv)
    assert tree.problems == []
    assert {r.pid for r in tree.records} == {21, 22}


def test_another_program_is_a_problem(tmp_path):
    _write(tmp_path, 20, 21, 5, [PY, "x.py"])
    assert child.resolve(tmp_path, 10, 20, 5, ["/bin/sh", "-c", "python x.py"]).problems


def test_children_and_forks_are_followed(tmp_path):
    _write(
        tmp_path,
        10,
        11,
        5,
        [PY, "outer.py"],
        ["spawn", 11, 12, 6, [PY, "inner.py"]],
        ["fork", 13],
    )
    _write(tmp_path, 11, 12, 6, [PY, "inner.py"], ["c", "inner.py", 1, "<module>"])
    # Started without a spawn line (killed before writing it), and by a fork.
    _write(tmp_path, 11, 14, 7, [PY, "lost.py"])
    _write(tmp_path, 13, 15, 8, [PY, "forked.py"], ["flag", "started a process another way"])
    tree = child.resolve(tmp_path, 10, 11, 5, [PY, "outer.py"])
    assert {r.pid for r in tree.records} == {11, 12, 14, 15}
    assert {11, 12, 13, 14, 15} <= tree.pids
    assert tree.problems == ["started a process another way"]


def test_a_corrupt_line_is_a_problem(tmp_path):
    _write(tmp_path, 10, 11, 5, [PY, "x.py"])
    path = tmp_path / child.CHILDREN / "10" / "11-5.jsonl"
    path.write_text(path.read_text() + '["c", "x.py"\n')
    assert child.resolve(tmp_path, 10, 11, 5, [PY, "x.py"]).problems


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["python", "-c", "pass"], ["code"]),
        (["python", "-cpass"], ["code"]),
        (["python", "-"], ["code"]),
        (["python"], ["code"]),
        (["python", "-X", "dev", "-W", "error", "-m", "pkg.tool", "-v"], ["module", "pkg.tool"]),
        (["python", "-mpkg.tool"], ["module", "pkg.tool"]),
        (["python", "-u", "/elsewhere/tool.py"], ["script", None, False]),
    ],
)
def test_the_entry_is_read_from_the_command_line(monkeypatch, argv, expected):
    monkeypatch.setattr(sys, "orig_argv", argv, raising=False)
    monkeypatch.setattr(child, "_roots", ("/checkout/",))
    assert child.entry() == expected


def test_a_script_under_the_interpreters_prefix_is_installed(monkeypatch):
    monkeypatch.setattr(sys, "orig_argv", ["python", f"{sys.prefix}/bin/tool"], raising=False)
    assert child.entry()[2] is True


def test_an_installed_copy_is_the_package_under_site_packages(monkeypatch):
    """The checkout's own directory may share the package's name."""
    monkeypatch.setattr(child, "_packages", frozenset({"strata"}))
    site = "/w/strata/.venv/lib/python3.13/site-packages/".replace("/", child.os.sep)
    assert child._installed_copy(site + "strata/__init__.py".replace("/", child.os.sep))
    assert child._installed_copy(site + "strata.py")
    assert not child._installed_copy(site + "pyarrow/__init__.py".replace("/", child.os.sep))


@pytest.mark.parametrize(
    "argv, clean",
    [
        (["/bin/uv", "sync", "--python", "3.13"], True),
        (["uv", "--quiet", "pip", "install", "x"], True),
        (["uv", "build"], False),
        (["uv", "tool", "run", "x"], False),
        (["git", "status"], False),
    ],
)
def test_a_package_managers_command_runs_no_project_code(tmp_path, argv, clean):
    # A build backend it started, which recorded itself.
    _write(tmp_path, 20, 21, 5, [PY, "-c", "build"])
    tree = child.resolve(tmp_path, 10, 20, 5, argv)
    assert (tree.problems == []) is clean
    if clean:
        assert [r.pid for r in tree.records] == [21]

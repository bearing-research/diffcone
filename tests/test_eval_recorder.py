"""Regression scenarios for the recorder findings of the corpus evaluation:
E6 (a recording disabled pytest's cache plugin) and E8 (each test's record
held its own file).

Evidence is recorded for real in a fixture repository; assertions check
exact target sets.
"""

from __future__ import annotations

import json
import sys

import pytest

from diffcone.evidence import EvidenceError
from diffcone.testing import selected

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_OPS = (
    "from pkg.ops import add, mul\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)
FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n.pytest_cache/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TEST_OPS,
}
BOTH = {"tests/test_ops.py::test_add", "tests/test_ops.py::test_mul"}


# --------------------------------------------------------------------------- E6


@needs_monitoring
def test_e6_a_recording_runs_a_suite_whose_addopts_need_the_cache_plugin(repo):
    """``--lf --ff`` are options of pytest's cache plugin: a recording
    disabled it, and pytest refused the run as a usage error (coverage.py's
    ``--failed-first``). The plugin stays, on an empty cache: the recorded
    failure neither narrows the recording (``--lf`` would run test_mul
    alone) nor is the repository's cache written."""
    repo.commit({**FILES, "pytest.ini": "[pytest]\naddopts = --lf --ff\n"})
    cache = repo.path / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    recorded = json.dumps({"tests/test_ops.py::test_mul": True})
    (cache / "lastfailed").write_text(recorded, encoding="utf-8")
    ev = repo.collect()
    assert set(ev.tests) == BOTH
    assert (cache / "lastfailed").read_text(encoding="utf-8") == recorded
    assert sorted(p.name for p in cache.iterdir()) == ["lastfailed"]


@needs_monitoring
def test_e6_a_recording_whose_cache_could_not_be_moved_is_refused(repo):
    """Moving the cache uses pytest internals; should that stop taking
    effect (here a conftest puts the cache back), the recording is refused
    rather than left for ``--lf`` to narrow."""
    conftest = (
        "import pytest\nfrom _pytest.cacheprovider import Cache\n\n\n"
        "@pytest.hookimpl(trylast=True)\ndef pytest_configure(config):\n"
        "    config.cache = Cache(config.rootpath / '.elsewhere', config, _ispytest=True)\n"
    )
    repo.commit({**FILES, "tests/conftest.py": conftest})
    with pytest.raises(EvidenceError, match="could not point pytest's cache"):
        repo.collect()


@needs_monitoring
def test_e6_a_command_that_disables_the_cache_plugin_still_records(repo):
    repo.commit({**FILES, "pytest.ini": "[pytest]\naddopts = --strict-config\n"})
    ev = repo.collect(command=f"{sys.executable} -m pytest -p no:cacheprovider")
    assert set(ev.tests) == BOTH


# --------------------------------------------------------------------------- E8


@needs_monitoring
def test_e8_a_body_change_of_a_test_selects_that_test_alone(repo):
    """pytest works out each test's location as it logs its start, which
    stats the test's file inside the test's window: every test's record
    held its own file, and an edit to one test selected every test of the
    file."""
    base = repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"tests/test_ops.py": TEST_OPS.replace("add(1, 2) == 3", "3 == add(1, 2)")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {"tests/test_ops.py::test_add"}


@needs_monitoring
def test_e8_a_test_that_reads_its_own_file_still_counts(repo):
    """Quiet only for pytest's own lookup: a test that reads its file is
    selected by an edit to it."""
    reads = TEST_OPS + (
        "\n\ndef test_reads_itself():\n"
        "    with open(__file__, encoding='utf-8') as f:\n        assert 'def' in f.read()\n"
    )
    base = repo.commit({**FILES, "tests/test_ops.py": reads})
    ev = repo.collect()
    head = repo.commit({"tests/test_ops.py": reads.replace("add(1, 2) == 3", "3 == add(1, 2)")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {"tests/test_ops.py::test_add", "tests/test_ops.py::test_reads_itself"}

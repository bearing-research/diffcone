"""Regression scenarios for Q19, Q20 and Q21 (audit round 4): the
indexer's pass-2 walkers and the use scan.

* Q19: every patcher form names its target the same way (one
  ``patch_target_form`` in indexer/patches.py); ``mock.patch.multiple``
  with a dotted string depends on the module and the names it patches.
* Q20: a patcher or MonkeyPatch store into process-global state that
  nothing undoes after the test is a process write.
* Q21: ``-c`` code that changes the import system by assignment can import
  anything.

Assertions check exact target sets, with an ASV target beside the pytest
ones that no change reaches.
"""

from __future__ import annotations

import pytest

from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
from diffcone.testing import asv_target, py_target, selected

NOOP = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")

# A sub-conftest's ``pytest_generate_tests`` runs ``tests/envsetup.py``'s
# ``apply`` before any test; a test in another directory reads the
# environment (as in tests/test_audit3_w11.py's W11 scenarios).
MODES = "def mode():\n    return 'a'\n"
MODES_B = MODES.replace("'a'", "'b'")
HOOK_FILES = {
    "pkg/__init__.py": "",
    "pkg/modes.py": MODES,
    "tests/__init__.py": "",
    "tests/sub/__init__.py": "",
    "tests/sub/test_s.py": "def test_s():\n    pass\n",
    "tests/sub/conftest.py": (
        "from tests import envsetup\n\n\ndef pytest_generate_tests(metafunc):\n"
        "    envsetup.apply()\n"
    ),
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": (
        "import os\n\n\ndef test_env():\n    assert os.environ.get('MODE', 'a') in 'ab'\n"
    ),
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
ENV, SUB = "tests/other/test_env.py::test_env", "tests/sub/test_s.py::test_s"

# ------------------------------------------------------------------ Q19

Q19_BASE = {
    "pkg/__init__.py": "",
    "pkg/config.py": "TIMEOUT = 5\nOTHER = 1\n",
    "pkg/api.py": "def fetch():\n    return 1\n",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
Q19_TEST = (
    "import contextlib\nfrom unittest import mock\n\nimport pytest\n\n"
    "from pkg.api import fetch\n\n\n"
    "def test_fetch():\n    with {call}:\n        assert fetch() == 1\n"
)
FETCH = py_target("tests/test_x.py::test_fetch", "tests.test_x.test_fetch")


@pytest.mark.parametrize(
    "call",
    [
        'mock.patch("pkg.config.TIMEOUT", 0)',
        'mock.patch.multiple("pkg.config", TIMEOUT=0)',
        'mock.patch.multiple("pkg.config", TIMEOUT=0, create=False)',
        'contextlib.nullcontext(pytest.MonkeyPatch().setattr("pkg.config.TIMEOUT", 0))',
    ],
    ids=["patch", "multiple", "multiple-options", "monkeypatch-value"],
)
def test_q19_patch_multiple_by_dotted_string_depends_on_its_names(repo, call):
    """``mock.patch.multiple("pkg.config", TIMEOUT=0)`` fails once
    ``TIMEOUT`` is gone, as ``mock.patch("pkg.config.TIMEOUT")`` does: the
    test depends on the names the call patches."""
    base = repo.commit({**Q19_BASE, "tests/test_x.py": Q19_TEST.format(call=call)})
    head = repo.commit({"pkg/config.py": "OTHER = 1\n"})
    plan = repo.plan(base, head, [FETCH, NOOP], source_roots=["."])
    assert selected(plan) == {FETCH.runner_id}


@pytest.mark.parametrize(
    "call",
    [
        'mock.patch("pkg.config.TIMEOUT", 0)',
        'mock.patch.multiple("pkg.config", TIMEOUT=0)',
        'mock.patch.dict("pkg.config.__dict__", TIMEOUT=0)',
    ],
    ids=["patch", "multiple", "dict"],
)
def test_q19_patching_by_dotted_string_imports_the_module(repo, call):
    """A patcher resolves a dotted string by importing its module, so the
    test runs that module's import-time code: a module-level statement
    added there reaches the test, which imports nothing of it itself."""
    base = repo.commit({**Q19_BASE, "tests/test_x.py": Q19_TEST.format(call=call)})
    head = repo.commit(
        {"pkg/config.py": "TIMEOUT = 5\nOTHER = 1\nif OTHER:\n    raise ImportError(OTHER)\n"}
    )
    plan = repo.plan(base, head, [FETCH, NOOP], source_roots=["."])
    assert selected(plan) == {FETCH.runner_id}


def test_q19_patch_multiple_of_a_third_party_module_writes_it(repo):
    """``mock.patch.multiple("json", dumps=...)`` started and kept stores on
    ``json``: a lookup by a name nothing bounds on it is a dynamic
    reference, both through the use scan and the walker."""
    files = {
        **Q19_BASE,
        "pkg/api.py": "import json\n\n\ndef fetch(name):\n    return getattr(json, name)\n",
        "tests/helper.py": (
            "from unittest import mock\n\n\ndef install():\n"
            "    mock.patch.multiple('json', dumps=len).start()\n"
        ),
    }
    snapshot = read_snapshot(repo.path, repo.commit(files), source_roots=["."])
    index = build_index(snapshot)
    assert any(r.symbol == "pkg.api.fetch" and "getattr" in r.detail for r in index.unresolved), (
        index.unresolved
    )


@pytest.mark.parametrize(
    "call",
    [
        "mock.patch.multiple(api, fetch=lambda: 1)",
        "mock.patch.multiple('pkg.api', fetch=lambda: 1)",
    ],
    ids=["object", "string"],
)
def test_q19_patch_multiple_rebinds_the_attributes_it_names(repo, call):
    """A started ``patch.multiple`` rebinds ``pkg.api.fetch`` for later
    code: a module-level ``getattr`` on ``pkg.api`` by a name a literal
    table bounds is no longer bounded by the table (a SCOPED or STORE record
    for each keyword, not an escape of the whole module)."""
    files = {
        **Q19_BASE,
        "pkg/api.py": "def fetch():\n    return 1\n",
        "tests/helper.py": (
            f"from unittest import mock\n\nfrom pkg import api\n\n\ndef install():\n"
            f"    {call}.start()\n"
        ),
    }
    snapshot = read_snapshot(repo.path, repo.commit(files), source_roots=["."])
    index = build_index(snapshot)
    writers = index.module_writers.get("pkg.api", ())
    assert "tests.helper.install" in writers, index.module_writers


# ------------------------------------------------------------------ Q20

PATCHERS = {
    "patch.dict start": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch.dict(os.environ, {'MODE': mode()}).start()\n"
    ),
    "patch.dict string start": (
        "from unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch.dict('os.environ', {'MODE': mode()}).start()\n"
    ),
    "MonkeyPatch().setenv": (
        "import pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().setenv('MODE', mode())\n"
    ),
    "MonkeyPatch().setitem": (
        "import os\nimport pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().setitem(os.environ, 'MODE', mode())\n"
    ),
    "MonkeyPatch().syspath_prepend": (
        "import pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().syspath_prepend(mode())\n"
    ),
    "patch.object start": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch.object(os, 'environ', {'MODE': mode()}).start()\n"
    ),
    "MonkeyPatch().setattr of a module": (
        "import sys\nimport pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    pytest.MonkeyPatch().setattr(sys, 'argv', [mode()])\n"
    ),
    "patch string start": (
        "from unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    mock.patch('os.sep', mode()).start()\n"
    ),
    # One notion of undone (patches.py): a ``with`` at module level is
    # import-time code, no test's, and counts as not undone.
    "with patch.dict at module level": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n"
        "with mock.patch.dict(os.environ):\n    os.environ['MODE'] = mode()\n\n\n"
        "def apply():\n    pass\n"
    ),
    "with patch.dict around a yield": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def _patched():\n    with mock.patch.dict(os.environ):\n"
        "        os.environ['MODE'] = mode()\n        yield\n\n\n"
        "def apply():\n    for _ in _patched():\n        break\n"
    ),
}


@pytest.mark.parametrize("source", PATCHERS.values(), ids=PATCHERS.keys())
def test_q20_patcher_write_of_process_state_reaches_every_test(repo, source):
    """A collection hook stores into the environment (or the import path)
    through a patcher nothing undoes after the test: what it stores outlives
    the test, so every test depends on the hook's code, as for a direct
    ``os.environ[...] = ...``."""
    base = repo.commit({**HOOK_FILES, "tests/envsetup.py": source})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = repo.plan(base, head, [NOOP], discover_runners=["pytest"])
    assert selected(plan) == {ENV, SUB}


UNDONE = {
    "with patch.dict": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    with mock.patch.dict(os.environ, {'MODE': mode()}):\n        pass\n"
    ),
    "with patch.dict around a store": (
        "import os\nfrom unittest import mock\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    with mock.patch.dict(os.environ):\n"
        "        os.environ['MODE'] = mode()\n"
    ),
    "MonkeyPatch.context": (
        "import pytest\n\nfrom pkg.modes import mode\n\n\n"
        "def apply():\n    with pytest.MonkeyPatch.context() as mp:\n"
        "        mp.setenv('MODE', mode())\n"
    ),
}


@pytest.mark.parametrize("source", UNDONE.values(), ids=UNDONE.keys())
def test_q20_patcher_write_undone_stays_scoped(repo, source):
    """The same stores undone when the statement ends write nothing that
    outlives it: the test in another directory is not selected."""
    base = repo.commit({**HOOK_FILES, "tests/envsetup.py": source})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = repo.plan(base, head, [NOOP], discover_runners=["pytest"])
    assert selected(plan) == {SUB}


# ------------------------------------------------------------------ Q21

Q21_BASE = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def value():\n    return 1\n",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
Q21_TEST = (
    "import subprocess\nimport sys\n\n\ndef test_cli():\n"
    "    subprocess.run([sys.executable, '-c', {code!r}], check=True)\n"
)
CLI = py_target("tests/test_cli.py::test_cli", "tests.test_cli.test_cli")


@pytest.mark.parametrize(
    "change",
    [
        "sys.path[:0] = ['pkg']",
        "sys.path += ['pkg']",
        "sys.path = ['pkg'] + sys.path",
        "del sys.path[0]",
        "sys.path.remove('')",
        "sys.meta_path.append(None)",
        "sys.modules['core'] = None",
        "p = sys.path; p[:0] = ['pkg']",
        "p = sys.path; p += ['pkg']",
        "import os; os.chdir('pkg')",
        "setattr(sys, 'path', ['pkg'] + sys.path)",
    ],
)
def test_q21_dash_c_code_changing_the_import_system_runs_anything(repo, change):
    """``-c`` code that redirects its imports (here ``import core`` finds
    ``pkg/core.py`` once ``pkg`` is on the path) can import anything, as
    with ``sys.path.insert``: a change anywhere selects the test."""
    code = f"import sys; {change}; import core; core.value()"
    base = repo.commit({**Q21_BASE, "tests/test_cli.py": Q21_TEST.format(code=code)})
    head = repo.commit({"pkg/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [CLI, NOOP], source_roots=["."])
    assert selected(plan) == {CLI.runner_id}


def test_q21_dash_c_code_through_an_imported_path(repo):
    """``from sys import path`` then ``path[:0] = ...``: the same list."""
    code = "from sys import path; path[:0] = ['pkg']; import core; core.value()"
    base = repo.commit({**Q21_BASE, "tests/test_cli.py": Q21_TEST.format(code=code)})
    head = repo.commit({"pkg/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [CLI, NOOP], source_roots=["."])
    assert selected(plan) == {CLI.runner_id}


def test_q21_dash_c_code_leaving_the_path_alone_stays_bounded(repo):
    """``-c`` code that binds a local named ``path`` and imports nothing of
    the project stays bounded: the change does not select it."""
    code = "import os; path = os.getcwd(); path += '/x'; print(path)"
    base = repo.commit({**Q21_BASE, "tests/test_cli.py": Q21_TEST.format(code=code)})
    head = repo.commit({"pkg/core.py": "def value():\n    return 2\n"})
    plan = repo.plan(base, head, [CLI, NOOP], source_roots=["."])
    assert selected(plan) == set()

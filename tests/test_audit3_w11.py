"""Regression scenarios for W11, W12 and W14 (internal/audit.md, "Known
after round 3"): code a hook or a collected module's import runs that
writes process-global state, and write-only references to a variable.

Static scenarios plan two commits; evidence scenarios record at the base
first (Python 3.12+). Assertions check exact target sets.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.testing import asv_target, rules, selected, unselected

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
BENCH = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")
needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")


def _plan(repo, base, head, **kwargs):
    return repo.plan(base, head, [BENCH], discover_runners=["pytest"], **kwargs)


MODES = "def mode():\n    return 'a'\n"
MODES_B = MODES.replace("'a'", "'b'")
ENVSETUP = (
    "import os\n\nfrom pkg.modes import mode\n\n\ndef apply():\n    os.environ['MODE'] = mode()\n"
)
READS_ENV = "import os\n\n\ndef test_env():\n    assert os.environ.get('MODE', 'a') in 'ab'\n"
QUIET = "def test_quiet():\n    pass\n"
ENV, SUB = "tests/other/test_env.py::test_env", "tests/sub/test_s.py::test_s"
QUIET_ID = "tests/test_quiet.py::test_quiet"


# --------------------------------------------------------------- W11: hook code

PLUGIN_OBJECT = """\
from tests import envsetup


class _Plugin:
    def {hook}(self, {arg}):
        envsetup.apply()


def pytest_configure(config):
    config.pluginmanager.register(_Plugin())
"""
GENERATE = """\
from tests import envsetup


def pytest_generate_tests(metafunc):
    envsetup.apply()
"""
HOOK_FILES = {
    **BASE,
    "pkg/modes.py": MODES,
    "tests/envsetup.py": ENVSETUP,
    "tests/sub/__init__.py": "",
    "tests/sub/test_s.py": "def test_s():\n    pass\n",
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": READS_ENV,
}
HOOK_CASES = [
    {"tests/conftest.py": PLUGIN_OBJECT.format(hook="pytest_sessionstart", arg="session")},
    # A path-scoped hook name on a plugin object: only conftests are scoped.
    {"tests/conftest.py": PLUGIN_OBJECT.format(hook="pytest_runtest_setup", arg="item")},
    {"tests/sub/conftest.py": GENERATE},
    # A module registered as a plugin by name at run time.
    {
        "tests/envplug.py": (
            "from tests import envsetup\n\n\ndef pytest_sessionstart(session):\n"
            "    envsetup.apply()\n"
        ),
        "tests/conftest.py": (
            "def pytest_configure(config):\n"
            "    config.pluginmanager.import_plugin('tests.envplug')\n"
        ),
    },
]
HOOK_IDS = [
    "plugin object",
    "plugin object, runtest hook",
    "sub-conftest generate_tests",
    "module plugin",
]


@pytest.mark.parametrize("conftests", HOOK_CASES, ids=HOOK_IDS)
def test_w11_code_a_hook_runs_reaches_every_test(repo, conftests):
    """A hook sets an environment variable through a helper, and a test in
    another directory reads it through ``os.environ``: no edge leads from
    the changed function to that test. The hook is a registered plugin
    object's method (pytest calls those for every test), or a collection
    hook in a sub-conftest (it runs before any test, and what it writes to
    the process stays)."""
    base = repo.commit({**HOOK_FILES, **conftests})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {ENV, SUB}
    assert rules(plan, ENV) == {"dependency"}
    assert BENCH.runner_id in unselected(plan)


def test_w11_collection_hook_writing_nothing_stays_scoped(repo):
    """A sub-conftest's ``pytest_generate_tests`` that writes no process
    state reaches only the tests under it."""
    base = repo.commit(
        {
            **HOOK_FILES,
            "tests/sub/conftest.py": (
                "from pkg.modes import mode\n\n\ndef pytest_generate_tests(metafunc):\n"
                "    metafunc.config.mode = mode()\n"
            ),
        }
    )
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {SUB}


def test_w11_hook_writing_an_in_scope_variable(repo):
    """State kept in an in-scope variable needs no session rule: the reader
    reaches the variable its writer mutates."""
    base = repo.commit(
        {
            **HOOK_FILES,
            "pkg/state.py": (
                "from pkg.modes import mode\n\nSTATE = {}\n\n\ndef apply():\n"
                "    STATE['mode'] = mode()\n\n\ndef current():\n    return STATE.get('mode')\n"
            ),
            "tests/sub/conftest.py": (
                "from pkg import state\n\n\ndef pytest_generate_tests(metafunc):\n"
                "    state.apply()\n"
            ),
            "tests/other/test_env.py": (
                "from pkg.state import current\n\n\ndef test_env():\n"
                "    assert current() in (None, 'a', 'b')\n"
            ),
        }
    )
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {ENV, SUB}


@needs_monitoring
@pytest.mark.parametrize("conftests", HOOK_CASES, ids=HOOK_IDS)
def test_w11_hook_code_in_evidence_mode(repo, conftests):
    """The same under evidence: the helper ran in a hook at the recording,
    and the change escalates to static planning, which reaches every test
    through the hook."""
    base = repo.commit({**HOOK_FILES, **conftests})
    evidence = repo.collect()
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head, evidence=evidence)
    assert ENV in selected(plan)
    assert SUB in selected(plan)


# --------------------------------------------------------- W12: collected modules

SETS_AT_IMPORT = """\
import os

from pkg.modes import mode

os.environ['MODE'] = mode()


def test_sets():
    pass
"""
SETS = "tests/test_sets.py::test_sets"
IMPORT_FILES = {
    **BASE,
    "pkg/modes.py": MODES,
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": READS_ENV,
    "tests/test_quiet.py": QUIET,
}
IMPORT_CASES = [
    # A test module's own import-time code.
    ({"tests/test_sets.py": SETS_AT_IMPORT}, {SETS, ENV}),
    # A sub-conftest's.
    (
        {
            "tests/sub/__init__.py": "",
            "tests/sub/conftest.py": SETS_AT_IMPORT.split("\n\n\ndef")[0] + "\n",
            "tests/sub/test_s.py": "def test_s():\n    pass\n",
        },
        {SUB, ENV},
    ),
    # A helper module a test module imports, whose import-time code writes.
    (
        {
            "tests/envinit.py": SETS_AT_IMPORT.split("\n\n\ndef")[0] + "\n",
            "tests/test_sets.py": "import tests.envinit  # noqa: F401\n\n\ndef test_sets():\n"
            "    pass\n",
        },
        {SETS, ENV},
    ),
    # ``sys.path`` instead of the environment, through a called helper.
    (
        {
            "tests/paths.py": (
                "import sys\n\nfrom pkg.modes import mode\n\n\ndef extend():\n"
                "    sys.path.append(mode())\n"
            ),
            "tests/test_sets.py": "from tests.paths import extend\n\nextend()\n\n\n"
            "def test_sets():\n    pass\n",
        },
        {SETS, ENV},
    ),
]


@pytest.mark.parametrize(
    "files, picked",
    IMPORT_CASES,
    ids=["test module", "sub-conftest", "imported helper", "sys.path helper"],
)
def test_w12_import_time_write_of_process_state_reaches_every_test(repo, files, picked):
    """pytest imports every test module and conftest while collecting,
    before any test runs: what their import-time code writes into the
    process (an environment variable, ``sys.path``) is there for every
    test, including one that imports none of them."""
    base = repo.commit({**IMPORT_FILES, **files})
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == picked | {QUIET_ID}
    assert BENCH.runner_id in unselected(plan)


def test_w12_import_time_code_writing_nothing_stays_scoped(repo):
    """Import-time code that only computes a module constant reaches the
    tests of its own module."""
    base = repo.commit(
        {
            **IMPORT_FILES,
            "tests/test_sets.py": (
                "from pkg.modes import mode\n\nMODE = mode()\n\n\ndef test_sets():\n"
                "    assert MODE\n"
            ),
        }
    )
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head)
    assert selected(plan) == {SETS}


@needs_monitoring
def test_w12_import_time_write_in_evidence_mode(repo):
    """Under evidence the import ran ``mode`` outside every test, so the
    change escalates the test module statically, which now reaches every
    test."""
    base = repo.commit({**IMPORT_FILES, "tests/test_sets.py": SETS_AT_IMPORT})
    evidence = repo.collect()
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {SETS, ENV, QUIET_ID}


# ------------------------------------------------------- W14: write-only sites

REG = """\
from pkg.calc import compute

REG = {init}


def register(k):
    REG[k] = compute(k)


def lookup(k):
    return REG.get(k)
"""
CALC = "def compute(k):\n    return k * 2\n"
CALC_B = CALC.replace("* 2", "* 3")
RESET = """\
import pytest

from pkg import reg


@pytest.fixture(autouse=True)
def _reset():
    yield
    {body}
"""
TEST_REG = (
    "from pkg.reg import lookup, register\n\n\ndef test_register():\n"
    "    register(1)\n    assert lookup(1) in (2, 3)\n"
)
BENCH_LOOKUP = "from pkg.reg import lookup\n\n\ndef time_lookup():\n    lookup(1)\n"
REGISTER, OTHER = "tests/test_reg.py::test_register", "tests/test_other.py::test_other"
LOOKUP_BENCH = asv_target("bench_reg.time_lookup", "benchmarks.bench_reg.time_lookup")


def _reg_files(init="{}", body="reg.REG.clear()", extra=None):
    return {
        **BASE,
        "pkg/calc.py": CALC,
        "pkg/reg.py": REG.format(init=init),
        "tests/conftest.py": RESET.format(body=body),
        "tests/test_reg.py": TEST_REG,
        "tests/test_other.py": "def test_other():\n    pass\n",
        "benchmarks/bench_reg.py": BENCH_LOOKUP,
        **(extra or {}),
    }


def _reg_plan(repo, base, head, **kwargs):
    return repo.plan(base, head, [BENCH, LOOKUP_BENCH], discover_runners=["pytest"], **kwargs)


@pytest.mark.parametrize(
    "body",
    ["reg.REG.clear()", "reg.REG.update({})", "reg.REG['k'] = 0", "reg.REG.pop('k', None)"],
)
def test_w14_a_reset_fixture_is_not_a_reader(repo, body):
    """An autouse fixture that only empties the registry does the same
    whatever the registry holds: a change to what fills it (``compute``,
    through ``register``) reaches the code reading it, not every test
    through the fixture. ``lookup`` reads it, so the benchmark timing it is
    selected."""
    base = repo.commit(_reg_files(body=body))
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, LOOKUP_BENCH.runner_id}
    assert unselected(plan) == {OTHER, BENCH.runner_id}


@pytest.mark.parametrize(
    "case",
    [
        # Reads the registry: what it holds decides what it does.
        {"body": "for k in list(reg.REG):\n        del reg.REG[k]"},
        {"body": "reg.REG.pop('k')"},  # raises when the key is missing
        {"body": "del reg.REG['k']"},
        # Not a builtin container: its own ``clear`` may read what it holds.
        {
            "init": "Registry()",
            "extra": {"pkg/registry.py": "class Registry(dict):\n    pass\n"},
        },
        # Something binds it anew: it may hold any kind of object.
        {"extra": {"pkg/swap.py": "from pkg import reg\n\n\ndef swap(new):\n    reg.REG = new\n"}},
        # A list's item store raises on what it holds (its length).
        {"init": "[0, 0, 0]", "body": "reg.REG[0] = 0"},
        # Emptying it runs the finalizers of what it held.
        {"extra": {"pkg/res.py": "class Res:\n    def __del__(self):\n        pass\n"}},
    ],
    ids=["iterates", "pop", "del", "custom class", "rebound", "list item", "finalizer"],
)
def test_w14_a_site_that_reads_stays_a_reader(repo, case):
    """Sites whose outcome depends on what the variable holds, or on what
    kind of object it is, are still readers."""
    files = _reg_files(
        case.get("init", "{}"), case.get("body", "reg.REG.clear()"), case.get("extra")
    )
    if case.get("init") == "Registry()":
        files["pkg/reg.py"] = "from pkg.registry import Registry\n" + files["pkg/reg.py"]
    base = repo.commit(files)
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, OTHER, LOOKUP_BENCH.runner_id}
    assert rules(plan, OTHER) == {"dependency"}


def test_w14_a_change_to_the_variable_itself_reaches_write_sites(repo):
    """The registry's own initialiser changing may change what kind of
    object it is: the fixture emptying it is affected."""
    base = repo.commit(_reg_files())
    head = repo.commit({"pkg/reg.py": REG.format(init="{'seed': 0}")})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, OTHER, LOOKUP_BENCH.runner_id}


MODE_REG = REG + "\n\nMODE = None\n\n\ndef set_mode():\n    global MODE\n    MODE = compute(0)\n"


def test_w14_rebinding_another_modules_variable_reads_nothing(repo):
    """``reg.MODE = None`` in a reset fixture binds the variable anew and
    reads nothing of it, so a change to what sets it does not reach the
    fixture's tests. Deleting the variable does."""
    files = _reg_files(body="reg.MODE = None")
    files["pkg/reg.py"] = MODE_REG.format(init="{}")
    files["tests/test_mode.py"] = (
        "from pkg import reg\n\n\ndef test_mode():\n    reg.set_mode()\n    assert reg.MODE == 0\n"
    )
    base = repo.commit(files)
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert selected(plan) == {REGISTER, "tests/test_mode.py::test_mode", LOOKUP_BENCH.runner_id}
    deleted = repo.commit({"pkg/reg.py": REG.format(init="{}")})
    plan = _reg_plan(repo, head, deleted)
    assert OTHER in selected(plan)


def test_w14_augmented_assignment_through_a_module_reads(repo):
    """``reg.MODE += 1`` reads the old value: the fixture is a reader."""
    files = _reg_files(body="reg.MODE += 0")
    files["pkg/reg.py"] = MODE_REG.format(init="{}").replace("MODE = None", "MODE = 0")
    base = repo.commit(files)
    head = repo.commit({"pkg/calc.py": CALC_B})
    plan = _reg_plan(repo, base, head)
    assert OTHER in selected(plan)


@needs_monitoring
def test_w14_import_time_registration_in_evidence_mode(repo):
    """Evidence: import-time code fills the registry with other arguments,
    so the registry holds something else; ``lookup`` reads it (and the test
    running it), the fixture that only empties it does not."""
    files = _reg_files(
        extra={
            "pkg/boot.py": "from pkg.reg import register\n\nregister(5)\n",
            "tests/test_reg.py": "import pkg.boot  # noqa: F401\n" + TEST_REG,
        }
    )
    base = repo.commit(files)
    evidence = repo.collect()
    head = repo.commit({"pkg/boot.py": "from pkg.reg import register\n\nregister(6)\n"})
    plan = _reg_plan(repo, base, head, evidence=evidence)
    assert REGISTER in selected(plan)
    assert OTHER in unselected(plan)

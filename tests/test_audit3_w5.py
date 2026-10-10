"""Regression scenarios for W5 and W7 (internal/audit.md, "Known after
round 3"), planning from execution evidence.

Each records real evidence in a fixture repository and plans a later commit
with it; assertions check exact target sets.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.testing import asv_target, rules, selected, unselected

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
BENCH = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")


def _plan(repo, base, head, evidence, **kwargs):
    return repo.plan(base, head, [BENCH], discover_runners=["pytest"], evidence=evidence, **kwargs)


# ------------------------------------------------------------------ W5: lifecycle

FLAGS = (
    "FLAGS = {}\n\n\ndef turn_on():\n    FLAGS['on'] = True\n\n\n"
    "def flag():\n    return FLAGS.get('on', False)\n"
)
TURN_ON = (
    "import pytest\n\nfrom pkg.flags import turn_on\n\n\n"
    "@pytest.fixture{0}\ndef {1}():\n    turn_on()\n"
)
MODE_FIXTURE = "import pytest\n\n\n@pytest.fixture\ndef mode():\n    return 0\n"
TEST_A = "def test_a():\n    pass\n"
TEST_B = (
    "{0}from pkg.flags import flag\n\n\n{1}def test_b({2}):\n    assert flag() in (True, False)\n"
)
A, B = "tests/test_a.py::test_a", "tests/sub/test_b.py::test_b"
LIFECYCLE_FILES = {
    **BASE,
    "pkg/flags.py": FLAGS,
    "tests/test_a.py": TEST_A,
    "tests/sub/__init__.py": "",
    "tests/sub/test_b.py": TEST_B.format("", "", ""),
}


@pytest.mark.parametrize(
    "before, after, picked",
    [
        # A conftest closer to the test starts overriding the fixture it asks for.
        (
            {
                "tests/conftest.py": MODE_FIXTURE,
                "tests/sub/test_b.py": TEST_B.format("", "", "mode"),
            },
            {"tests/sub/conftest.py": TURN_ON.format("", "mode")},
            {B},
        ),
        # A mark stored in another module starts naming a fixture.
        (
            {
                "tests/conftest.py": TURN_ON.format("", "on"),
                "tests/marks.py": "import pytest\n\nwith_flag = pytest.mark.usefixtures()\n",
                "tests/sub/test_b.py": TEST_B.format(
                    "from tests.marks import with_flag\n", "@with_flag\n", ""
                ),
            },
            {"tests/marks.py": "import pytest\n\nwith_flag = pytest.mark.usefixtures('on')\n"},
            {B},
        ),
        # The ini option ``usefixtures``.
        (
            {"tests/conftest.py": TURN_ON.format("", "on"), "pytest.ini": "[pytest]\n"},
            {"pytest.ini": "[pytest]\nusefixtures = on\n"},
            {A, B, BENCH.runner_id},
        ),
        # A plugin loaded with ``-p`` in addopts.
        (
            {"tests/plugin.py": TURN_ON.format("(autouse=True)", "on"), "pytest.ini": "[pytest]\n"},
            {"pytest.ini": "[pytest]\naddopts = -p tests.plugin\n"},
            {A, B, BENCH.runner_id},
        ),
        # A plugin another test module registers with ``pytest_plugins``.
        (
            {"tests/plugin.py": TURN_ON.format("(autouse=True)", "on")},
            {"tests/test_a.py": "pytest_plugins = ['tests.plugin']\n\n\n" + TEST_A},
            {A, B},
        ),
    ],
    ids=[
        "conftest override",
        "usefixtures elsewhere",
        "ini usefixtures",
        "addopts -p",
        "pytest_plugins",
    ],
)
@pytest.mark.parametrize("since", ["head", "base"])
def test_w5_a_lifecycle_change_selects_the_test_in_evidence_mode(
    repo, before, after, picked, since
):
    """What runs around test_b changes though no code its record holds does:
    selected as static planning selects it (``lifecycle_changed``). With
    ``since == "base"`` the change lies between the recording and the
    base, and the plan's own change (to code only the new fixture runs)
    reaches the test only through it."""
    c = repo.commit({**LIFECYCLE_FILES, **before})
    evidence = repo.collect()
    if since == "head":
        base, head = c, repo.commit(after)
    else:
        base = repo.commit(after)
        head = repo.commit({"pkg/flags.py": FLAGS.replace("= True", "= 1")})
    plan = _plan(repo, base, head, evidence)
    if since == "base":  # the benchmark is planned statically, base -> head
        picked = picked - {BENCH.runner_id}
    assert selected(plan) == picked
    assert "lifecycle_changed" in rules(plan, B)
    assert "since the recording" in next(
        r.detail for r in reasons_of(plan, B) if r.rule == "lifecycle_changed"
    )
    if BENCH.runner_id not in picked:  # pytest's configuration changed: everything
        assert BENCH.runner_id in unselected(plan)


def reasons_of(plan, runner_id):
    (decision,) = [d for d in plan.decisions if d.target.runner_id == runner_id]
    return decision.reasons


# ------------------------------------------------------------- W5: hook code

MODES = "def mode():\n    return 'a'\n"
ENVSETUP = (
    "import os\n\nfrom pkg.modes import mode\n\n\ndef apply():\n    os.environ['MODE'] = mode()\n"
)
READS_ENV = "import os\n\n\ndef test_env():\n    assert os.environ['MODE'] in 'ab'\n"
PLUGIN_OBJECT = """\
from tests import envsetup


class _Plugin:
    def pytest_sessionstart(self, session):
        envsetup.apply()


def pytest_configure(config):
    config.pluginmanager.register(_Plugin())
"""
GENERATE = """\
from tests import envsetup


def pytest_generate_tests(metafunc):
    envsetup.apply()
"""
ENV, SUB = "tests/other/test_env.py::test_env", "tests/sub/test_s.py::test_s"


@pytest.mark.parametrize(
    "conftests",
    [{"tests/conftest.py": PLUGIN_OBJECT}, {"tests/sub/conftest.py": GENERATE}],
    ids=["plugin object", "path-scoped hook"],
)
def test_w5_code_a_hook_runs_reaches_every_test(repo, conftests):
    """A hook sets an environment variable through a helper, and a test
    reads it through ``os.environ``: no static path leads from the changed
    function to that test (the hook is a registered plugin object's method,
    or a hook pytest calls only for the tests under its conftest). The hook
    runs once per session, before the tests, so code it ran at C reaches
    every test."""
    base = repo.commit(
        {
            **BASE,
            "pkg/modes.py": MODES,
            "tests/envsetup.py": ENVSETUP,
            "tests/sub/__init__.py": "",
            "tests/sub/test_s.py": "def test_s():\n    pass\n",
            "tests/other/__init__.py": "",
            "tests/other/test_env.py": READS_ENV,
            **conftests,
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/modes.py": MODES.replace("'a'", "'b'")})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == {ENV, SUB}
    assert rules(plan, ENV) == {"pytest_hook_changed"}
    assert BENCH.runner_id in unselected(plan)


IDS_TEST = """\
import pytest


@pytest.mark.parametrize("n", [1, 2], ids=lambda n: f"n{n}")
def test_ids(n):
    assert n > 0
"""


def test_w5_a_tests_own_ids_callable_reaches_only_that_test(repo):
    """pytest calls ``ids=`` while collecting, outside every test, and the
    callable lands in the test function's symbol. That is code collecting
    the test itself: a change to the test reaches that test, not every test."""
    base = repo.commit(
        {
            **BASE,
            "tests/test_ids.py": IDS_TEST,
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    evidence = repo.collect()
    assert any(s.startswith("tests.test_ids.test_ids") for s in evidence.hook_phase)
    head = repo.commit({"tests/test_ids.py": IDS_TEST.replace("n > 0", "n >= 1")})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == {"tests/test_ids.py::test_ids"}


def test_w5_a_reflection_site_run_at_import_reaches_who_reads_what_it_built(repo):
    """``dir(pkg.reg)`` runs while ``pkg.user`` is imported, outside every
    test: the site joins E, but no test ran it. A site is a reader: one at
    module level is followed like the variable it initialises, through to
    the test reading that variable."""
    base = repo.commit(
        {
            **BASE,
            "pkg/reg.py": "def a():\n    return 1\n",
            "pkg/user.py": (
                "import pkg.reg\n\nNAMES = [n for n in dir(pkg.reg) if not n.startswith('_')]\n"
            ),
            "tests/test_u.py": (
                "from pkg.user import NAMES\n\n\ndef test_u():\n    assert 'a' in NAMES\n"
            ),
            "tests/test_v.py": "def test_v():\n    pass\n",
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/reg.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n"})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == {"tests/test_u.py::test_u"}


# ------------------------------------------------------------------------ W7

CACHE_LIB = """\
_CACHE = {}


def compute(k):
    return k * 2


def get(k):
    if k not in _CACHE:
        _CACHE[k] = compute(k)
    return _CACHE[k]
"""
# The writer reaches ``compute`` through a helper, and tests read the cache
# through another function than the writer.
CHAIN_LIB = """\
_CACHE = {}


def compute(k):
    return k * 2


def _fill(k):
    return compute(k)


def get(k):
    if k not in _CACHE:
        _CACHE[k] = _fill(k)
    return peek(k)


def peek(k):
    return _CACHE[k]
"""
# ``compute`` is a method called on an object of unknown type.
METHOD_LIB = """\
_CACHE = {}


class Backend:
    def compute(self, k):
        return k * 2


def get(backend, k):
    if k not in _CACHE:
        _CACHE[k] = backend.compute(k)
    return _CACHE[k]
"""
USES = "from pkg.lib import get\n\n\ndef test_{0}():\n    assert get(2) == 4\n"
USES_METHOD = (
    "from pkg.lib import Backend, get\n\n\ndef test_{0}():\n    assert get(Backend(), 2) == 4\n"
)
CACHE_TESTS = {
    "tests/test_a.py::test_a",
    "tests/test_m.py::test_m1",
    "tests/test_m.py::test_m2",
    "tests/test_z.py::test_z",
}


@pytest.mark.parametrize(
    "lib, uses, edit",
    [
        (CACHE_LIB, USES, ("k * 2", "k + k")),
        (CHAIN_LIB, USES, ("k * 2", "k + k")),
        (METHOD_LIB, USES_METHOD, ("k * 2", "k + k")),
    ],
    ids=["direct", "through a helper", "a method by name"],
)
def test_w7_a_module_cache_filled_by_an_earlier_test(repo, lib, uses, edit):
    """test_m1 and test_m2 run between test_a and test_z in both orders, so
    the cache is always filled when they run and their records never hold
    ``compute``: the reverse-order check cannot flag them. ``get`` can run
    ``compute`` and writes ``_CACHE``, so the tests that read ``_CACHE``
    are selected."""
    second = uses.format("m2").split("\n\n\n", 1)[1]
    base = repo.commit(
        {
            **BASE,
            "pkg/lib.py": lib,
            "tests/test_a.py": uses.format("a"),
            "tests/test_m.py": uses.format("m1") + "\n\n" + second,
            "tests/test_z.py": uses.format("z"),
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    evidence = repo.collect(reverse_check=True)
    middle = evidence.tests["tests/test_m.py::test_m1"]
    assert not any("compute" in s for s in evidence.executed(middle))
    head = repo.commit({"pkg/lib.py": lib.replace(*edit)})
    plan = _plan(repo, base, head, evidence)
    assert selected(plan) == CACHE_TESTS
    assert "stores into pkg.lib._CACHE" in next(
        r.detail for r in reasons_of(plan, "tests/test_m.py::test_m1")
    )

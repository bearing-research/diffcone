"""Evidence-mode scenarios (internal/evidence_design.md).

Each records real evidence in a fixture repository (``diffcone collect``
runs its suite under the recorder), commits a change and plans with that
evidence. pytest targets come from static discovery; ASV targets from a
manifest, which evidence does not cover, so they keep their static
selection. Assertions check exact target sets and the rules behind them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from diffcone.cli import main
from diffcone.evidence import EvidenceError, load_store
from diffcone.testing import (
    asv_target,
    reason,
    rules,
    selected,
    unselected,
)

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

OPS = """\
def add(a, b):
    return a + b


def mul(a, b):
    return a * b
"""

TEST_OPS = """\
from pkg.ops import add, mul


def test_add():
    assert add(1, 2) == 3


def test_mul():
    assert mul(2, 3) == 6
"""

BENCH = """\
from pkg.ops import add, mul


class TimeOps:
    def time_add(self):
        add(1, 2)

    def time_mul(self):
        mul(2, 3)
"""

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TEST_OPS,
    "benchmarks/__init__.py": "",
    "benchmarks/bench_ops.py": BENCH,
}
ASV = [
    asv_target("bench_ops.TimeOps.time_add", "benchmarks.bench_ops.TimeOps.time_add"),
    asv_target("bench_ops.TimeOps.time_mul", "benchmarks.bench_ops.TimeOps.time_mul"),
]
ADD, MUL = "tests/test_ops.py::test_add", "tests/test_ops.py::test_mul"
BENCHES = {"bench_ops.TimeOps.time_add", "bench_ops.TimeOps.time_mul"}


def _plan(repo, base, head, evidence, **kwargs):
    return repo.plan(base, head, ASV, discover_runners=["pytest"], evidence=evidence, **kwargs)


def _collected(repo, files):
    base = repo.commit(files)
    return base, repo.collect()


def test_a_body_change_selects_the_tests_that_executed_it(repo):
    base, ev = _collected(repo, BASE)
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {ADD, "bench_ops.TimeOps.time_add"}
    assert rules(plan, ADD) == {"executed_changed"}
    assert "executed pkg.ops.add" in reason(plan, ADD, "executed_changed").detail
    assert rules(plan, "bench_ops.TimeOps.time_add") == {"dependency"}  # static, for ASV
    assert plan.evidence["planned"] == [f"{base[:12]} -> head"]


DISPATCH = {
    **BASE,
    "pkg/api.py": """\
class Ops:
    def a(self):
        return 1

    def b(self):
        return 2


def call(obj, name):
    return getattr(obj, name)()


def has(obj, name):
    return hasattr(obj, name)
""",
    "tests/test_api.py": """\
from pkg.api import Ops, call, has


def test_call_a():
    assert call(Ops(), "a") == 1


def test_b():
    assert Ops().b() == 2


def test_has():
    assert not has(Ops(), "c")
""",
}
CALL_A, B, HAS = ("tests/test_api.py::" + n for n in ("test_call_a", "test_b", "test_has"))


def test_a_method_reached_only_by_a_dynamic_lookup_is_in_the_record(repo):
    base, ev = _collected(repo, DISPATCH)
    head = repo.commit(
        {"pkg/api.py": DISPATCH["pkg/api.py"].replace("return 1", "return 1 + 0", 1)}
    )
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {CALL_A}
    # Static planning binds the getattr to every member of the class handed
    # to it, so every test holding an Ops is selected.
    static = repo.plan(base, head, ASV, discover_runners=["pytest"])
    assert selected(static) == {CALL_A, B, HAS}


def test_an_added_method_selects_the_tests_that_ran_an_unbounded_lookup(repo):
    base, ev = _collected(repo, DISPATCH)
    api = DISPATCH["pkg/api.py"].replace(
        "    def b(self):", "    def c(self):\n        return 3\n\n    def b(self):"
    )
    head = repo.commit({"pkg/api.py": api})
    plan = _plan(repo, base, head, ev)
    # test_has would now find c. call() is bound: its callers name "a".
    assert selected(plan) == {HAS}
    assert rules(plan, HAS) == {"lookup_site"}


SETTINGS = {
    **BASE,
    "pkg/settings.py": "LIMIT = 1\nOTHER = 2\n",
    "pkg/derived.py": "from pkg.settings import LIMIT\n\nDOUBLE = LIMIT * 2\n",
    "tests/test_settings.py": """\
from pkg import derived, settings


def test_dynamic():
    name = "".join(["LIM", "IT"])
    assert getattr(settings, name) == 1


def test_vars():
    assert vars(settings)["LIMIT"] == 1


def test_derived():
    name = "".join(["DOU", "BLE"])
    assert getattr(derived, name) == 2


def test_other():
    assert settings.OTHER == 2
""",
}


def test_a_changed_value_reaches_the_lookups_that_can_read_it(repo):
    base, ev = _collected(repo, SETTINGS)
    head = repo.commit({"pkg/settings.py": "LIMIT = 3\nOTHER = 2\n"})
    plan = _plan(repo, base, head, ev)
    T = "tests/test_settings.py::"
    # Each of the first three now fails, though none names LIMIT or DOUBLE
    # (DOUBLE captured LIMIT's value); test_other reads nothing that changed.
    assert selected(plan) == {T + "test_dynamic", T + "test_vars", T + "test_derived"}
    assert rules(plan, T + "test_dynamic") == {"lookup_site"}
    assert rules(plan, T + "test_derived") == {"lookup_site"}


def test_a_changed_signature_reaches_callers_through_a_lookup(repo):
    base, ev = _collected(repo, DISPATCH)
    api = DISPATCH["pkg/api.py"].replace("    def b(self):", "    def b(self, extra=0):")
    head = repo.commit({"pkg/api.py": api})
    plan = _plan(repo, base, head, ev)
    # test_b executed Ops.b; test_has ran a lookup that could reach it.
    # call() is bound to the names its callers pass ("a").
    assert selected(plan) == {B, HAS}
    assert rules(plan, B) >= {"executed_changed"}


DATA = {
    **BASE,
    "pkg/data.json": "[1]",
    "pkg/files.py": """\
import json
import os


def load():
    with open("pkg/data.json") as f:
        return json.load(f)


def names():
    return sorted(os.listdir("pkg"))


def has_extra():
    return os.path.exists("pkg/extra.txt")
""",
    "tests/test_files.py": """\
from pkg.files import has_extra, load, names


def test_load():
    assert load() == [1]


def test_names():
    assert "data.json" in names()


def test_extra():
    assert not has_extra()
""",
}
LOAD, NAMES, EXTRA = (
    "tests/test_files.py::" + n for n in ("test_load", "test_names", "test_extra")
)


def test_a_changed_data_file_selects_the_tests_that_touched_it(repo):
    base, ev = _collected(repo, DATA)
    head = repo.commit({"pkg/data.json": "[2]"})
    plan = _plan(repo, base, head, ev)
    # test_names only listed the directory holding it, which an edit does not
    # change. ASV targets keep static planning, which reads no data file.
    assert selected(plan) == {LOAD, *BENCHES}
    assert rules(plan, LOAD) == {"touched_file"}
    assert plan.fallbacks == []


def test_an_added_file_selects_a_test_that_checked_it_was_absent(repo):
    base, ev = _collected(repo, DATA)
    head = repo.commit({"pkg/extra.txt": "x"})
    plan = _plan(repo, base, head, ev)
    # One test checked for it, one listed its directory.
    assert selected(plan) == {EXTRA, NAMES, *BENCHES}
    assert rules(plan, EXTRA) == {"touched_file"}


def test_compiled_source_and_configuration_select_everything(repo):
    base, ev = _collected(repo, {**DATA, "pkg/_speed.pyx": "def f(): pass\n"})
    head = repo.commit({"pkg/_speed.pyx": "def f(): return 1\n"})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) >= {LOAD, NAMES, EXTRA, ADD, MUL}
    assert rules(plan, ADD) == {"unobserved_file_changed"}
    head2 = repo.commit({"pytest.ini": "[pytest]\n"})
    plan2 = _plan(repo, base, head2, ev)
    assert rules(plan2, ADD) == {"unobserved_file_changed"}


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pylock.toml",
        "conda-lock.yml",
        "pytest.toml",
        "uv.toml",
        "vendor/tool-1.0.dist-info/entry_points.txt",
        "pkg.egg-info/PKG-INFO",
    ],
)
def test_a_changed_lock_file_selects_everything(repo, path):
    # What is installed or how pytest starts: read before any test runs, so
    # no record shows who depends on it. A distribution's metadata is found
    # by name on sys.path, which a recording does not keep (below).
    base, ev = _collected(repo, {**DATA, path: "a = 1\n"})
    head = repo.commit({path: "a = 2\n"})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) >= {LOAD, NAMES, EXTRA, ADD, MUL}
    assert rules(plan, ADD) == {"unobserved_file_changed"}


def test_always_run_targets_are_selected_under_evidence(repo):
    """``[[always_run]]`` holds in evidence mode, for a test the evidence
    would not select and for an ASV target planned from the code."""
    toml = (
        '[[always_run]]\ntargets = "tests/test_ops.py::test_mul"\n\n'
        '[[always_run]]\ntargets = "*time_mul"\nrunner = "asv"\n'
    )
    base, ev = _collected(repo, {**BASE, "diffcone.toml": toml})
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = _plan(repo, base, head, ev)
    assert plan.fallbacks == [] and not plan.degraded
    assert selected(plan) == {ADD, MUL, *BENCHES}
    assert rules(plan, MUL) == {"always_run"}
    assert rules(plan, "bench_ops.TimeOps.time_mul") == {"always_run"}


def _environment_in_checkout(repo):
    """A virtual environment inside the checkout (``.venv``, ignored), whose
    script runs pytest from this interpreter's packages, as ``uv run pytest``
    runs ``.venv/bin/pytest``."""
    import os
    import subprocess
    import sysconfig

    venv = repo.path / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    outer = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    (Path(purelib) / "outer.pth").write_text("".join(f"{p}\n" for p in sorted(outer)))
    script = scripts / "runtests"
    script.write_text("import sys\n\nimport pytest\n\nsys.exit(pytest.console_main())\n")
    return f'"{python}" "{script}"'


IGNORE_VENV = {".gitignore": "__pycache__/\n.diffcone/\n.venv/\n"}


def test_pytest_run_from_an_environment_in_the_checkout_is_not_project_code(repo):
    """The environment's own scripts (``.venv/bin/pytest``) are installed
    code: pytest stat'ing every file it collects, with that script at the
    bottom of the stack, is not the project reading them outside a test."""
    base = repo.commit({**BASE, **IGNORE_VENV})
    ev = repo.collect(command=_environment_in_checkout(repo))
    head = repo.commit({"tests/test_ops.py": TEST_OPS.replace("== 3", "== 1 + 2")})
    plan = _plan(repo, base, head, ev)
    assert plan.fallbacks == []
    # The edited test only: pytest's stat of a test's own file is not in its
    # record (E8), and no benchmark (everything was selected).
    assert selected(plan) == {ADD}


SCANS = {
    **BASE,
    **IGNORE_VENV,
    "conftest.py": """\
import importlib.metadata
import os


def pytest_sessionfinish(session):
    # The run wrote into the checkout (its mtime moved), so the scan lists
    # it again rather than using its cached listing.
    os.utime(session.config.rootpath)
    {d.metadata["Name"] for d in importlib.metadata.distributions()}
""",
}


def test_a_hook_scanning_installed_distributions_does_not_see_every_file(repo):
    """``importlib.metadata`` stats and lists each ``sys.path`` entry, the
    checkout included, looking for ``*.dist-info`` names: an added test file
    is not among what it can find (a distribution's metadata selects
    everything, above)."""
    base, ev = _collected(repo, SCANS)
    added = "tests/test_more.py"
    head = repo.commit({added: "def test_more():\n    pass\n"})
    plan = _plan(repo, base, head, ev)
    assert plan.fallbacks == []
    assert selected(plan) == {f"{added}::test_more"}


IMPORT_TIME = {
    **BASE,
    "pkg/registry.py": "def make():\n    return 1\n",
    "pkg/table.py": "from pkg.registry import make\n\nVALUE = make()\n",
    "tests/test_table.py": "from pkg.table import VALUE\n\n\n"
    "def test_value():\n    assert VALUE == 1\n",
}
VALUE = "tests/test_table.py::test_value"


def test_code_run_at_import_escalates_the_importing_module(repo):
    base, ev = _collected(repo, IMPORT_TIME)
    head = repo.commit({"pkg/registry.py": "def make():\n    return 2\n"})
    plan = _plan(repo, base, head, ev)
    # test_value never executed make: pkg.table's import did, and built VALUE.
    assert selected(plan) == {VALUE}
    assert rules(plan, VALUE) == {"escalated"}
    assert plan.evidence["escalated_modules"] == ["pkg.table"]


FIXTURES = {
    **BASE,
    "tests/conftest.py": """\
import pytest

from pkg.ops import mul


@pytest.fixture(scope="session")
def shared():
    return mul(2, 2)
""",
    "tests/test_shared.py": """\
def test_x(shared):
    assert shared == 4


def test_y(shared):
    assert shared == 4


def test_z():
    assert True
""",
}
X, Y, Z = ("tests/test_shared.py::" + n for n in ("test_x", "test_y", "test_z"))


def test_a_shared_fixture_is_credited_to_every_user(repo):
    base, ev = _collected(repo, FIXTURES)
    head = repo.commit({"pkg/ops.py": OPS.replace("return a * b", "return b * a")})
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {X, Y, MUL, "bench_ops.TimeOps.time_mul"}


def test_a_fixture_decorator_change_selects_every_test_in_its_scope(repo):
    base, ev = _collected(repo, FIXTURES)
    conftest = FIXTURES["tests/conftest.py"].replace('scope="session"', 'scope="module"')
    head = repo.commit({"tests/conftest.py": conftest})
    plan = _plan(repo, base, head, ev)
    # The decorator runs at import, so the conftest is planned statically
    # too, which reaches every test under it.
    assert selected(plan) == {X, Y, Z, ADD, MUL}
    assert "test_scope" in rules(plan, X)


def test_a_pytest_hook_change_selects_everything(repo):
    base, ev = _collected(
        repo, {**FIXTURES, "conftest.py": "def pytest_configure(config):\n    pass\n"}
    )
    head = repo.commit(
        {"conftest.py": "def pytest_configure(config):\n    config.option.verbose = 0\n"}
    )
    plan = _plan(repo, base, head, ev)
    assert selected(plan) >= {X, Y, Z, ADD, MUL}
    assert "pytest_hook_changed" in rules(plan, Z)


def test_tests_without_a_usable_record_are_always_selected(repo):
    files = {
        **BASE,
        "pkg/memo.py": "_V = None\n\n\ndef compute():\n    return 1\n\n\n"
        "def get():\n    global _V\n    if _V is None:\n        _V = compute()\n    return _V\n",
        "tests/test_misc.py": """\
import subprocess
import sys

from pkg.memo import get


def test_one():
    assert get() == 1


def test_two():
    assert get() == 1


def test_sub():
    subprocess.run([sys.executable, "-c", "import pkg.memo"], check=True)
""",
    }
    base = repo.commit(files)
    ev = repo.collect(reverse_check=True)
    head = repo.commit(
        {"tests/test_new.py": "def test_new():\n    assert True\n", "pkg/unused.py": "X = 1\n"}
    )
    plan = _plan(repo, base, head, ev)
    one, two, sub = ("tests/test_misc.py::" + n for n in ("test_one", "test_two", "test_sub"))
    assert selected(plan) == {one, two, sub, "tests/test_new.py::test_new"}
    assert rules(plan, one) == {"unstable"}
    assert rules(plan, sub) == {"subprocess"}
    assert {"no_evidence", "new_target"} <= rules(plan, "tests/test_new.py::test_new")


def test_evidence_older_than_the_base_plans_both_sides(repo):
    c, ev = _collected(repo, BASE)
    base = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    head = repo.commit({"pkg/ops.py": OPS})  # reverts to C's content
    plan = _plan(repo, base, head, ev)
    # C -> head is empty, but base -> head changes add: C -> base finds it.
    assert selected(plan) == {ADD, "bench_ops.TimeOps.time_add"}
    assert plan.evidence["planned"] == [f"{c[:12]} -> head", f"{c[:12]} -> base"]


VALUES = {
    **BASE,
    "pkg/cfg.py": "LIMIT = 3\n",
    "pkg/use.py": """\
from pkg.cfg import LIMIT

TABLE = {"x": LIMIT}


def limit():
    return LIMIT


def lookup():
    return TABLE["x"]


def other():
    return 0
""",
    "tests/test_use.py": """\
from pkg.use import limit, lookup, other


def test_limit():
    assert limit() == 3


def test_lookup():
    assert lookup() == 3


def test_other():
    assert other() == 0
""",
}


def test_a_variable_is_followed_through_the_values_that_captured_it(repo):
    base, ev = _collected(repo, VALUES)
    head = repo.commit({"pkg/cfg.py": "LIMIT = 4\n"})
    plan = _plan(repo, base, head, ev)
    t = "tests/test_use.py::"
    assert selected(plan) == {t + "test_limit", t + "test_lookup"}
    assert rules(plan, t + "test_lookup") == {"executed_reader"}


CLASSES = {
    **BASE,
    "pkg/shapes.py": """\
class Shape:
    sides = 0

    def area(self):
        return 0


DEFAULT = Shape()


def sides(obj):
    return obj.sides


def area(obj):
    return obj.area()
""",
    "tests/test_shapes.py": """\
from pkg.shapes import DEFAULT, area, sides


def test_sides():
    assert sides(DEFAULT) == 0


def test_area():
    assert area(DEFAULT) == 0
""",
}
SIDES, AREA = "tests/test_shapes.py::test_sides", "tests/test_shapes.py::test_area"


def test_a_class_attribute_change_reaches_its_readers_by_name(repo):
    base, ev = _collected(repo, CLASSES)
    head = repo.commit(
        {"pkg/shapes.py": CLASSES["pkg/shapes.py"].replace("sides = 0", "sides = 4")}
    )
    plan = _plan(repo, base, head, ev)
    # sides() reads .sides off an object from elsewhere. test_area holds the
    # same Shape but never reads the attribute.
    assert selected(plan) == {SIDES}
    assert rules(plan, SIDES) == {"executed_reader"}


def test_a_skipped_test_whose_mark_is_removed_is_selected(repo):
    files = {
        **BASE,
        "tests/test_skip.py": "import pytest\n\n\n@pytest.mark.skip\n"
        "def test_later():\n    assert True\n\n\ndef test_now():\n    assert True\n",
    }
    base, ev = _collected(repo, files)
    head = repo.commit(
        {
            "tests/test_skip.py": "def test_later():\n    assert True\n\n\n"
            "def test_now():\n    assert True\n"
        }
    )
    plan = _plan(repo, base, head, ev)
    assert "tests/test_skip.py::test_later" in selected(plan)
    assert "changed_target" in rules(plan, "tests/test_skip.py::test_later")


BASE_TESTS = {
    **BASE,
    "tests/base.py": """\
import pytest


class Base:
    @pytest.fixture
    def value(self):
        return 1

    def test_value(self, value):
        assert value == 1

    def check(self, name):
        return getattr(self, name)
""",
    "tests/test_sub.py": """\
from tests.base import Base


class TestSub(Base):
    def test_lookup(self):
        assert self.check("test_" + "value")
""",
    "tests/test_other.py": """\
def lookup(obj, name):
    return getattr(obj, name, None)


def test_other():
    assert lookup(object(), "x" + "y") is None
""",
}
SUB_VALUE, SUB_LOOKUP = (
    "tests/test_sub.py::TestSub::test_value",
    "tests/test_sub.py::TestSub::test_lookup",
)
OTHER = "tests/test_other.py::test_other"


def test_a_test_class_member_is_seen_only_by_lookups_in_its_hierarchy(repo):
    base, ev = _collected(repo, BASE_TESTS)
    changed = BASE_TESTS["tests/base.py"].replace(
        "def test_value(self, value):", "def test_value(self, value, extra=None):"
    )
    head = repo.commit({"tests/base.py": changed})
    plan = _plan(repo, base, head, ev)
    # Only pytest holds a TestSub; test_other's unbounded lookup cannot meet one.
    assert selected(plan) == {SUB_VALUE, SUB_LOOKUP}
    assert "changed_target" in rules(plan, SUB_VALUE)
    assert "lookup_site" in rules(plan, SUB_LOOKUP)


def test_a_fixture_on_a_base_class_reaches_the_tests_that_list_it(repo):
    base, ev = _collected(repo, BASE_TESTS)
    changed = BASE_TESTS["tests/base.py"].replace(
        "    @pytest.fixture\n", "    @pytest.fixture(params=[1])\n"
    )
    head = repo.commit({"tests/base.py": changed})
    plan = _plan(repo, base, head, ev)
    assert SUB_VALUE in selected(plan) and OTHER not in selected(plan)
    assert "test_scope" in rules(plan, SUB_VALUE)


GENERATED = "tests/test_gen.py::test_gen"


def test_an_unresolved_fixture_needs_no_fallback_when_no_fixture_changed(repo):
    files = {
        **BASE_TESTS,
        "tests/test_gen.py": """\
def pytest_generate_tests(metafunc):
    if "gen" in metafunc.fixturenames:
        metafunc.parametrize("gen", [1])


def test_gen(gen):
    assert gen == 1
""",
    }
    base, ev = _collected(repo, files)
    head = repo.commit({"pkg/ops.py": OPS.replace("return a + b", "return b + a")})
    plan = _plan(repo, base, head, ev)
    # Discovery cannot resolve `gen` (supplied by pytest_generate_tests), but
    # the record holds what the test ran, and no module that uses pytest
    # changed a definition.
    assert selected(plan) == {ADD, "bench_ops.TimeOps.time_add"}
    static = repo.plan(base, head, ASV, discover_runners=["pytest"])
    assert "lifecycle_dependency_unresolved" in rules(static, GENERATED)


COMBINED = {
    **BASE,
    "tests/base.py": """\
class BaseOne:
    def test_one(self):
        assert self.helper() == 1

    def helper(self):
        return 1


class BaseTwo:
    def test_two(self):
        assert True


class AllTests(BaseOne, BaseTwo):
    pass
""",
    "tests/test_all.py": """\
from tests.base import AllTests


class TestAll(AllTests):
    pass
""",
    "tests/test_other.py": BASE_TESTS["tests/test_other.py"],
}


def test_a_class_that_only_combines_base_tests_does_not_hold_them(repo):
    base, ev = _collected(repo, COMBINED)
    changed = COMBINED["tests/base.py"].replace(
        "    def helper(self):", "    def helper(self, extra=None):"
    )
    head = repo.commit({"tests/base.py": changed})
    plan = _plan(repo, base, head, ev)
    # Only pytest holds a TestAll, so test_other's lookup cannot meet one.
    # (test_two is in the changed class's scope: it could use a fixture there.)
    assert selected(plan) == {
        "tests/test_all.py::TestAll::test_one",
        "tests/test_all.py::TestAll::test_two",
    }


# A fake in test code (roadmap item 13): a name-matched call such as
# ``proc.stdout.read()`` in library code lands on its member only in a test
# that built one, so only those tests are selected through the name match.
FAKES = {
    **BASE,
    "pkg/stream.py": """\
def drain(proc):
    out = []
    if proc.stdout.closed:
        return b""
    while True:
        chunk = proc.stdout.read(2)
        if not chunk:
            return b"".join(out)
        out.append(chunk)
""",
    "tests/test_stream.py": """\
import pytest

from pkg.stream import drain


class _FakePipe:
    closed = False

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProcess:
    def __init__(self, chunks):
        self.stdout = _FakePipe(chunks)


@pytest.fixture
def proc():
    return _FakeProcess([b"x"])


def test_fake():
    assert drain(_FakeProcess([b"ab", b"c"])) == b"abc"


def test_fixture(proc):
    assert drain(proc) == b"x"

""",
    "tests/test_real.py": """\
import io
from types import SimpleNamespace

from pkg.stream import drain


def test_real():
    assert drain(SimpleNamespace(stdout=io.BytesIO(b"abc"))) == b"abc"
""",
    "tests/test_slow.py": """\
from types import SimpleNamespace

from pkg.stream import drain
from tests.test_stream import _FakePipe


class _Slow(_FakePipe):
    pass


def test_slow():
    assert drain(SimpleNamespace(stdout=_Slow([b"y"]))) == b"y"
""",
}
STREAM = "tests/test_stream.py::"
FAKE, FIXTURE = STREAM + "test_fake", STREAM + "test_fixture"
REAL = "tests/test_real.py::test_real"
SLOW = "tests/test_slow.py::test_slow"
READ = "    def read(self, n):"


def _fake_change(repo, files, old=READ, new="    def read(self, n=2):"):
    base, ev = _collected(repo, files)
    stream = files["tests/test_stream.py"]
    assert old in stream
    head = repo.commit({"tests/test_stream.py": stream.replace(old, new)})
    return _plan(repo, base, head, ev)


def test_a_fake_member_reaches_name_matched_readers_only_where_a_fake_was_built(repo):
    plan = _fake_change(repo, FAKES)
    # test_real ran drain, which calls .read() by name, but never built a pipe.
    assert selected(plan) == {FAKE, FIXTURE, SLOW}
    detail = reason(plan, FAKE, "executed_reader").detail
    assert "pkg.stream.drain" in detail and "tests.test_stream._FakeProcess.__init__" in detail
    assert "tests.test_slow.test_slow" in reason(plan, SLOW, "executed_reader").detail


def test_a_fake_attribute_reaches_name_matched_readers_only_where_a_fake_was_built(repo):
    plan = _fake_change(repo, FAKES, "    closed = False", "    closed = 0")
    assert selected(plan) == {FAKE, FIXTURE, SLOW}


def test_a_deleted_fake_member_reaches_the_tests_that_built_one(repo):
    plan = _fake_change(
        repo, FAKES, READ + '\n        return self._chunks.pop(0) if self._chunks else b""\n', ""
    )
    assert selected(plan) == {FAKE, FIXTURE, SLOW}


def test_a_test_class_handing_itself_on_reaches_the_reader(repo):
    files = {
        **FAKES,
        "tests/test_self.py": """\
from pkg.stream import drain


class TestSelf:
    def read(self, n):
        return b""

    def test_self(self):
        self.stdout = self
        assert drain(self) == b""
""",
    }
    base, ev = _collected(repo, files)
    old = files["tests/test_self.py"]
    head = repo.commit(
        {"tests/test_self.py": old.replace("def read(self, n):", "def read(self, n=2):")}
    )
    plan = _plan(repo, base, head, ev)
    assert selected(plan) == {"tests/test_self.py::TestSelf::test_self"}


# The rule's conditions matter where the test holding a fake runs none of
# its code: here a method added to it, which a library function looks up.
BARE = {
    **FAKES,
    "pkg/stream.py": FAKES["pkg/stream.py"]
    + """

def flushed(x):
    flush = getattr(x, "flush", None)
    return flush is None or flush() is None
""",
    "tests/test_bare.py": """\
class _Bare:
    closed = False


def test_bare():
    assert not _Bare.closed
""",
    "tests/test_open.py": """\
from types import SimpleNamespace

from pkg.stream import flushed


def test_open():
    assert flushed(SimpleNamespace())
""",
}
HELD, OPEN = "tests/test_held.py::test_held", "tests/test_open.py::test_open"
HELD_FILE = """\
import pytest

from pkg.stream import flushed
{imports}

{decorator}
def test_held({param}):
    assert flushed({value})
"""
LIBRARY = """

class Base:
    pass


REGISTRY = []


def register(cls):
    REGISTRY.append(cls)
    return cls


def all_flushed(classes):
    return all(flushed(c()) for c in classes)


def subclasses_flushed():
    return all_flushed(Base.__subclasses__())


def registered_flushed():
    return all_flushed(REGISTRY)
"""


def _bare_change(repo, held, bare=BARE["tests/test_bare.py"], **files):
    files = {**BARE, "tests/test_bare.py": bare, "tests/test_held.py": held, **files}
    files["pkg/stream.py"] += LIBRARY
    base, ev = _collected(repo, files)
    flush = "    closed = False\n\n    def flush(self):\n        return None\n"
    head = repo.commit({"tests/test_bare.py": bare.replace("    closed = False\n", flush)})
    return _plan(repo, base, head, ev)


@pytest.mark.parametrize(
    "where",
    ["in the test", "a lookup in test code", "parametrize"],
)
def test_a_bare_fake_reaches_the_tests_that_built_one(repo, where):
    held = {
        "in the test": dict(imports="from tests.test_bare import _Bare", value="_Bare()"),
        "a lookup in test code": dict(
            imports="import tests.test_bare as bare", value='getattr(bare, "_" + "Bare")()'
        ),
        "parametrize": dict(
            imports="from tests.test_bare import _Bare",
            decorator='@pytest.mark.parametrize("bare", [_Bare()])',
            param="bare",
            value="bare",
        ),
    }[where]
    plan = _bare_change(repo, HELD_FILE.format(**{"decorator": "", "param": "", **held}))
    assert HELD in selected(plan)
    if where != "parametrize":  # a module-level reference: the rule is off
        assert OPEN not in selected(plan)


@pytest.mark.parametrize(
    "where",
    [
        "module-level instance",
        "pytest_generate_tests",
        "library base",
        "decorated",
        "registered on creation",
    ],
)
def test_a_fake_that_can_be_held_without_building_it_keeps_every_reader(repo, where):
    bare, files = BARE["tests/test_bare.py"], {}
    held = {"imports": "", "decorator": "", "param": "", "value": ""}
    if where == "module-level instance":
        bare += "\n\nBARE = _Bare()\n"
        held.update(imports="from tests.test_bare import BARE", value="BARE")
    elif where == "pytest_generate_tests":
        # make() builds the fake during collection, outside every test.
        files["tests/conftest.py"] = """\
from tests.test_bare import _Bare


def make():
    return _Bare()


def pytest_generate_tests(metafunc):
    if "made" in metafunc.fixturenames:
        metafunc.parametrize("made", [make()])
"""
        held.update(param="made", value="made")
    elif where == "library base":
        # Only Base.__subclasses__() finds the fake.
        bare = "from pkg.stream import Base\n\n\n" + bare.replace("_Bare:", "_Bare(Base):")
        held.update(
            imports="import tests.test_bare\nfrom pkg.stream import subclasses_flushed",
            value="subclasses_flushed",
        )
    elif where == "decorated":
        # Only the registry finds the fake.
        bare = "from pkg.stream import register\n\n\n@register\n" + bare
        held.update(
            imports="import tests.test_bare\nfrom pkg.stream import registered_flushed",
            value="registered_flushed",
        )
    elif where == "registered on creation":
        # A base in test code registers every subclass when it is created.
        bare = (
            "from pkg.stream import REGISTRY\n\n\nclass _Registered:\n"
            "    def __init_subclass__(cls):\n        REGISTRY.append(cls)\n\n\n"
            + bare.replace("_Bare:", "_Bare(_Registered):")
        )
        held.update(
            imports="import tests.test_bare\nfrom pkg.stream import registered_flushed",
            value="registered_flushed",
        )
    if where in ("library base", "decorated", "registered on creation"):
        held_file = HELD_FILE.format(**{**held, "value": "x"}).replace(
            "assert flushed(x)", f"assert {held['value']}()"
        )
    else:
        held_file = HELD_FILE.format(**held)
    plan = _bare_change(repo, held_file, bare, **files)
    assert HELD in selected(plan)


# Lookups on an external module the project writes to (roadmap item 14):
# what they can find of the project's was stored by the code writing there,
# which is in the record of the tests that saw it.
EXTERNAL = {
    **BASE,
    "pkg/names.py": """\
import builtins


def is_builtin(name):
    return name in dir(builtins)


def lookup(name):
    return getattr(builtins, name, None)
""",
    "pkg/extra.py": """\
import builtins


def marker():
    return 1


def install():
    builtins.diffcone_installed = 1
""",
    "tests/test_names.py": """\
from pkg.names import is_builtin, lookup


def test_names():
    assert is_builtin("len")


def test_lookup():
    assert lookup("len") is len
""",
    "tests/test_patched.py": """\
import builtins

from pkg.extra import install, marker
from pkg.names import is_builtin


def test_patched(monkeypatch):
    monkeypatch.setattr(builtins, "diffcone_marker", marker, raising=False)
    assert is_builtin("diffcone_marker")


def test_installed():
    install()
    assert is_builtin("diffcone_installed")
    del builtins.diffcone_installed
""",
}
EXT_NAMES, EXT_LOOKUP = "tests/test_names.py::test_names", "tests/test_names.py::test_lookup"
PATCHED = "tests/test_patched.py::test_patched"
INSTALLED = "tests/test_patched.py::test_installed"
ADDED_NAME = OPS + "\n\ndef added():\n    return 2\n"


def test_a_lookup_on_a_module_only_tests_write_to_sees_no_unrelated_name(repo):
    base, ev = _collected(repo, EXTERNAL)
    head = repo.commit({"pkg/ops.py": ADDED_NAME})
    plan = _plan(repo, base, head, ev)
    assert not {EXT_NAMES, EXT_LOOKUP, PATCHED, INSTALLED} & selected(plan)


def test_a_value_stored_on_an_external_module_reaches_its_writer(repo):
    base, ev = _collected(repo, EXTERNAL)
    extra = EXTERNAL["pkg/extra.py"].replace("def marker():", "def marker(x=None):")
    head = repo.commit({"pkg/extra.py": extra})
    plan = _plan(repo, base, head, ev)
    assert PATCHED in selected(plan)
    assert not {EXT_NAMES, EXT_LOOKUP} & selected(plan)


def test_a_writer_newly_run_at_import_reaches_the_lookups(repo):
    base, ev = _collected(repo, EXTERNAL)
    head = repo.commit({"pkg/extra.py": EXTERNAL["pkg/extra.py"] + "\n\ninstall()\n"})
    plan = _plan(repo, base, head, ev)
    # pkg.extra's import now leaves a name on builtins for every later test.
    assert {EXT_NAMES, EXT_LOOKUP} <= selected(plan)


# Only tests write to builtins (test_patched and test_writer's own function).
TESTS_ONLY = {
    **EXTERNAL,
    "pkg/extra.py": "def marker():\n    return 1\n",
    "tests/test_patched.py": EXTERNAL["tests/test_patched.py"]
    .replace("from pkg.extra import install, marker", "from pkg.extra import marker")
    .split("\n\n\ndef test_installed")[0]
    + "\n",
    "tests/test_writer.py": """\
import builtins


def install_names():
    builtins.diffcone_named = 1


def test_writer():
    install_names()
    del builtins.diffcone_named
""",
}
PLUGIN = "import sys\n\n\ndef run(mod):\n    if mod is not None:\n        mod.install_names()\n"
REGISTRY = (
    "CALLBACKS = []\n\n\ndef run_all():\n    for callback in CALLBACKS:\n        callback()\n"
)
USES_PLUGIN = "import pkg.plugin  # noqa: F401\n\n\ndef test_plugin():\n    pass\n"
OPS_AT_IMPORT = OPS + "\n\nif OPS_READY := True:\n    pass\n"


OPS_RUNS_INSTALL = (
    OPS
    + "\n\nimport pkg.extra\n\n\ndef _setup():\n    pkg.extra.install()\n\n\n"
    + "if _setup():\n    pass\n"
)


@pytest.mark.parametrize("writer", ["tests only", "library", "library, run at import"])
def test_a_library_import_reaches_a_lookup_only_through_a_library_writer(repo, writer):
    files = dict(TESTS_ONLY)
    if writer != "tests only":
        files["pkg/extra.py"] = EXTERNAL["pkg/extra.py"]
    base, ev = _collected(repo, files)
    # Module-level code of pkg.ops changed: its import-time state may differ.
    ops = OPS_RUNS_INSTALL if writer == "library, run at import" else OPS_AT_IMPORT
    head = repo.commit({"pkg/ops.py": ops})
    plan = _plan(repo, base, head, ev)
    assert "pkg.ops" in plan.evidence["escalated_modules"]
    if writer == "library, run at import":
        # pkg.extra.install now runs at import, through a helper, for every
        # later test.
        assert {EXT_NAMES, EXT_LOOKUP} <= selected(plan)
        assert "lookup_site" in rules(plan, EXT_NAMES)
    else:
        # Audit round 3, W1: the new import-time code runs nothing that
        # writes to builtins (a library writer stays out of reach as a test's
        # does).
        assert not {EXT_NAMES, EXT_LOOKUP} & selected(plan)


@pytest.mark.parametrize(
    "how",
    [
        "a helper module imports it",
        "library code calls it by name",
        "a registry holds it",
        "a test module's variable calls it",
    ],
)
def test_a_test_writer_newly_run_at_import_reaches_the_lookups(repo, how):
    files = dict(TESTS_ONLY)
    changed: dict[str, str] = {}
    if how == "a helper module imports it":
        files["tests/helpers.py"] = "from tests.test_writer import install_names  # noqa: F401\n"
        files["tests/test_helped.py"] = (
            "import tests.helpers  # noqa: F401\n\n\ndef test_helped():\n    pass\n"
        )
        changed["tests/helpers.py"] = files["tests/helpers.py"] + "\ninstall_names()\n"
    elif how == "library code calls it by name":
        files["pkg/plugin.py"] = PLUGIN
        files["tests/test_plugin.py"] = USES_PLUGIN
        changed["pkg/plugin.py"] = PLUGIN + '\n\nrun(sys.modules.get("tests.test_writer"))\n'
    elif how == "a registry holds it":
        files["pkg/registry.py"] = REGISTRY
        files["pkg/plugin.py"] = ""
        files["tests/test_plugin.py"] = USES_PLUGIN
        files["tests/test_writer.py"] += (
            "\n\nfrom pkg.registry import CALLBACKS  # noqa: E402\n\n"
            "CALLBACKS.append(install_names)\n"
        )
        changed["pkg/plugin.py"] = "from pkg.registry import run_all\n\nrun_all()\n"
    else:
        changed["tests/test_writer.py"] = (
            files["tests/test_writer.py"] + "\n\nINSTALLED = install_names()\n"
        )
    base, ev = _collected(repo, files)
    head = repo.commit(changed)
    plan = _plan(repo, base, head, ev)
    assert {EXT_NAMES, EXT_LOOKUP} <= selected(plan)


def test_a_library_variable_newly_running_a_writer_reaches_the_lookups(repo):
    base, ev = _collected(repo, EXTERNAL)
    head = repo.commit({"pkg/extra.py": EXTERNAL["pkg/extra.py"] + "\n\nINSTALLED = install()\n"})
    plan = _plan(repo, base, head, ev)
    assert {EXT_NAMES, EXT_LOOKUP} <= selected(plan)


@pytest.mark.parametrize("where", ["module level", "called at import", "in a hook"])
def test_a_lookup_on_a_module_written_outside_tests_sees_every_name(repo, where):
    files = dict(EXTERNAL)
    if where == "module level":
        files["pkg/boot.py"] = "import builtins\n\nbuiltins.diffcone_boot = 1\n"
        files["tests/test_boot.py"] = (
            "import pkg.boot  # noqa: F401\n\n\ndef test_boot():\n    pass\n"
        )
    elif where == "called at import":
        files["pkg/boot.py"] = "from pkg.extra import install\n\ninstall()\n"
        files["tests/test_boot.py"] = (
            "import pkg.boot  # noqa: F401\n\n\ndef test_boot():\n    pass\n"
        )
    else:
        files["tests/conftest.py"] = (
            "from pkg.extra import install\n\n\ndef pytest_configure(config):\n    install()\n"
        )
    base, ev = _collected(repo, files)
    head = repo.commit({"pkg/ops.py": ADDED_NAME})
    plan = _plan(repo, base, head, ev)
    assert {EXT_NAMES, EXT_LOOKUP} <= selected(plan)


# Child processes (roadmap item 15): a Python process a test starts through
# subprocess.Popen records itself, and the test is credited with what it ran.

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="children record on POSIX")

WORK = """\
import sys


def handle(x):
    return x + 1


def serve():
    for line in sys.stdin:
        print(handle(int(line)), flush=True)
"""
CHILD = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/work.py": WORK,
    "pkg/tool.py": (
        'from pkg.work import handle\n\nif __name__ == "__main__":\n    print(handle(1))\n'
    ),
    "pkg/serve.py": "from pkg.work import serve\n\nserve()\n",
    "pkg/other.py": "def g():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_other.py": "from pkg.other import g\n\n\ndef test_other():\n    assert g() == 1\n",
}
CHILD_TEST = """\
import os
import subprocess
import sys


def test_child(tmp_path):
    {setup}
    out = subprocess.run({argv}, capture_output=True, text=True{kwargs})
    assert out.stdout.strip() == "2", out.stderr
"""
TOOL = "[sys.executable, '-m', 'pkg.tool']"
FAKE_UV = (
    "uv = tmp_path / 'uv'\n"
    "    uv.write_text('#!/bin/sh\\nshift\\n\"$@\"\\nexit $?\\n')\n"
    "    uv.chmod(0o755)"
)
HANDLED = WORK.replace("return x + 1", "return 1 + x")
OTHER_CHANGE = {"pkg/other.py": "def g():\n    return 1 + 0\n"}
CHILD_ID = "tests/test_child.py::test_child"
OTHER_ID = "tests/test_other.py::test_other"


def _child_files(argv: str, setup: str = "pass", kwargs: str = "") -> dict[str, str]:
    test = CHILD_TEST.format(argv=argv, setup=setup, kwargs=kwargs)
    return {**CHILD, "tests/test_child.py": test}


def _child_plans(repo, files, command=None):
    """Plans for a change only the child ran, and for an unrelated one."""
    base = repo.commit(files)
    ev = repo.collect(command=command)
    handled = repo.commit({"pkg/work.py": HANDLED})
    other = repo.commit({"pkg/work.py": WORK, **OTHER_CHANGE})
    plan = lambda head: repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)  # noqa: E731
    return plan(handled), plan(other)


@posix_only
@pytest.mark.parametrize(
    "argv, setup",
    [
        (TOOL, "pass"),
        ("[sys.executable, 'pkg/tool.py']", "pass"),
        # A launcher: the Python it starts records itself, and ran its command.
        (f"[str(tmp_path / 'uv'), 'run', {TOOL[1:]}", FAKE_UV),
    ],
    ids=["module", "script", "launcher"],
)
def test_a_child_process_is_credited_to_its_test(repo, argv, setup):
    handled, other = _child_plans(repo, _child_files(argv, setup))
    assert selected(handled) == {CHILD_ID}
    assert rules(handled, CHILD_ID) == {"executed_changed"}
    # Flagged, so selected for every change, before.
    assert selected(other) == {OTHER_ID}


@posix_only
@pytest.mark.parametrize(
    "argv, setup, kwargs",
    [
        # Another program: what it runs itself is not seen.
        (f"['sh', '-c', ' '.join({TOOL})]", "pass", ""),
        # The child's environment loses the recorder's settings.
        (TOOL, "pass", ", env={'PATH': os.environ['PATH']}"),
        # No sitecustomize: no site, or PYTHONPATH ignored.
        ("[sys.executable, '-S', '-m', 'pkg.tool']", "pass", ""),
        (
            "[sys.executable, '-E', '-m', 'pkg.tool']",
            "pass",
            ", env={**os.environ, 'PYTHONPATH': os.getcwd()}",
        ),
        # A launcher whose command recorded nothing.
        ("[str(tmp_path / 'uv'), 'run', sys.executable, '-S', '-m', 'pkg.tool']", FAKE_UV, ""),
        # Project code under -c: the snippet may read any name of it.
        ("[sys.executable, '-c', 'from pkg.work import handle; print(handle(1))']", "pass", ""),
    ],
    ids=["shell", "environment", "no-site", "ignore-environment", "launcher", "snippet"],
)
def test_a_child_that_does_not_record_itself_still_flags(repo, argv, setup, kwargs):
    handled, other = _child_plans(repo, _child_files(argv, setup, kwargs))
    assert CHILD_ID in selected(handled)
    assert selected(other) == {CHILD_ID, OTHER_ID}
    assert rules(other, CHILD_ID) == {"subprocess"}


@posix_only
def test_a_running_child_is_credited_to_every_later_test(repo):
    """A warm worker started by one test serves the next: the second test
    ran its handler, though it did not start it (a credit at the spawn only
    missed it)."""
    warm = """\
import subprocess
import sys

WORKER = []


def test_start():
    WORKER.append(
        subprocess.Popen(
            [sys.executable, "-m", "pkg.serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
    )


def test_use():
    worker = WORKER[0]
    worker.stdin.write("1\\n")
    worker.stdin.flush()
    assert worker.stdout.readline().strip() == "2"
    worker.stdin.close()
    worker.wait()


def test_after():
    assert not WORKER[0].poll()
"""
    handled, other = _child_plans(repo, {**CHILD, "tests/test_warm.py": warm})
    start, use, after = (
        "tests/test_warm.py::" + n for n in ("test_start", "test_use", "test_after")
    )
    assert selected(handled) == {start, use}
    # It runs once the worker has ended.
    assert after not in selected(handled)
    assert selected(other) == {OTHER_ID}


@posix_only
@pytest.mark.parametrize(
    "module",
    [
        # A child of the child.
        "import subprocess\nimport sys\n\n"
        "out = subprocess.run([sys.executable, '-m', 'pkg.tool'], capture_output=True, text=True)\n"
        "print(out.stdout.strip())\n",
        # A fork of the child writes to its record.
        "import os\n\nfrom pkg import work\n\nr, w = os.pipe()\npid = os.fork()\n"
        "if pid == 0:\n    os.write(w, str(work.handle(1)).encode())\n    os._exit(0)\n"
        "os.waitpid(pid, 0)\nprint(os.read(r, 10).decode())\n",
    ],
    ids=["grandchild", "fork"],
)
def test_what_a_child_starts_is_followed(repo, module):
    files = {**_child_files("[sys.executable, '-m', 'pkg.outer']"), "pkg/outer.py": module}
    handled, other = _child_plans(repo, files)
    assert selected(handled) == {CHILD_ID}
    assert selected(other) == {OTHER_ID}


@posix_only
def test_a_shared_fixtures_child_is_credited_to_every_test_using_it(repo, tmp_path):
    """Two pytest processes, as two xdist workers: the first computes the
    session fixture in a child, the second reads it from a file."""
    cache = tmp_path / "cache.txt"
    conftest = f"""\
import os
import subprocess
import sys

import pytest

CACHE = {str(cache)!r}


@pytest.fixture(scope="session")
def value():
    if not os.path.exists(CACHE):
        out = subprocess.run([sys.executable, "-m", "pkg.tool"], capture_output=True, text=True)
        with open(CACHE, "w") as f:
            f.write(out.stdout.strip())
    with open(CACHE) as f:
        return f.read()
"""
    files = {
        **CHILD,
        "tests/conftest.py": conftest,
        "tests/test_a.py": "def test_a(value):\n    assert value == '2'\n",
        "tests/test_b.py": "def test_b(value):\n    assert value == '2'\n",
    }
    py = sys.executable
    command = (
        f'sh -c \'{py} -m pytest "$@" tests/test_a.py tests/test_other.py && '
        f'{py} -m pytest "$@" tests/test_b.py\' sh'
    )
    handled, other = _child_plans(repo, files, command=command)
    assert selected(handled) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert selected(other) == {OTHER_ID}


# --------------------------------------------------------------------------- shared helpers


PKG = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _evidence_plan(repo, base, head, evidence, targets=()):
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], evidence=evidence)


NOOP_BENCH = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")


def _noop_plan(repo, base, head, evidence, **kwargs):
    return repo.plan(
        base, head, [NOOP_BENCH], discover_runners=["pytest"], evidence=evidence, **kwargs
    )


# --------------------------------------------------------------------------- import-time code


STATE = """\
MODE = ["slow"]


def set_mode(m):
    _store(m)
    return m


def _store(m):
    MODE[0] = m


def mode():
    return MODE[0]
"""

READS_MODE = """\
from pkg.state import mode


def test_b():
    assert mode() == "slow"
"""

IMPORTS_CONFIG = "import pkg.config  # noqa: F401\n\n\ndef test_a():\n    pass\n"
A_ID, B_ID = "tests/test_a.py::test_a", "tests/test_b.py::test_b"

# Static planning reaches a benchmark of the changed function itself (the static
# side of EVP-2 and EVP-6, reaching the readers of what it writes, is not
# evidence planning's: ASV targets keep their static decision).
MODE_BENCH = asv_target("benchmarks.TimeMode.time_set_mode", "pkg.state.set_mode")


@pytest.mark.parametrize("where", ["library", "conftest", "class"])
def test_evp2_code_run_at_import_writes_state_others_read(repo, where):
    """``set_mode`` runs only while another module is imported, and writes
    ``MODE`` (through a helper) in a third: a change to its body reaches the
    tests reading ``MODE``, which never ran it."""
    files = {**PKG, "pkg/state.py": STATE, "tests/test_b.py": READS_MODE}
    if where == "library":
        files["pkg/config.py"] = 'from pkg.state import set_mode\n\nX = set_mode("slow")\n'
        files["tests/test_a.py"] = IMPORTS_CONFIG
    elif where == "class":
        # Calling a class runs its constructor.
        files["pkg/config.py"] = (
            "from pkg.state import set_mode\n\n\nclass Mode:\n    def __init__(self, m):\n"
            "        set_mode(m)\n\n\nX = Mode('slow')\n"
        )
        files["tests/test_a.py"] = IMPORTS_CONFIG
    else:
        files["tests/conftest.py"] = 'from pkg.state import set_mode\n\nset_mode("slow")\n'
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"pkg/state.py": STATE.replace("    _store(m)\n", "    _store(m + '!')\n")})
    plan = _evidence_plan(repo, base, head, ev, [MODE_BENCH])
    want = {B_ID, MODE_BENCH.runner_id} | ({A_ID} if where != "conftest" else set())
    assert selected(plan) == want


IMPORT_CALLS = {
    "library variable": ("pkg/config.py", "from pkg.state import set_mode\n\nX = {}\n", True),
    "library class attribute": (
        "pkg/config.py",
        "from pkg.state import set_mode\n\n\nclass K:\n    x = {}\n",
        True,
    ),
    "test decorator": (
        "tests/test_a.py",
        "import pytest\n\nfrom pkg.state import set_mode\n\n\n"
        '@pytest.mark.parametrize("x", [{}])\ndef test_a(x):\n    pass\n',
        False,
    ),
    "test module variable": (
        "tests/test_a.py",
        "from pkg.state import set_mode\n\nX = {}\n\n\ndef test_a():\n    pass\n",
        False,
    ),
}


@pytest.mark.parametrize("case", [*IMPORT_CALLS, "added test"])
def test_evp6_an_import_time_call_with_a_changed_argument(repo, case):
    """What an import-time call writes is read by tests that never ran it:
    changing its argument, or adding a test whose decorator makes the call,
    reaches them."""
    old, new = 'set_mode("slow")', 'set_mode("faster")'
    if case == "added test":
        path = "tests/test_a.py"
        text = "import pytest\n\nfrom pkg.state import set_mode\n\n\ndef test_a():\n    pass\n"
        extra = False
        after = text + f'\n\n@pytest.mark.parametrize("x", [{new}])\ndef test_new(x):\n    pass\n'
    else:
        path, template, extra = IMPORT_CALLS[case]
        text, after = template.format(old), template.format(new)
    files = {**PKG, "pkg/state.py": STATE, "tests/test_b.py": READS_MODE, path: text}
    if extra:
        files["tests/test_a.py"] = IMPORTS_CONFIG
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({path: after})
    plan = _evidence_plan(repo, base, head, ev)
    want = {A_ID, B_ID} | ({"tests/test_a.py::test_new"} if case == "added test" else set())
    assert selected(plan) == want


CYTHON_STATE = """\
# cython: profile=True
MODE = ["slow"]


def set_mode(m):
    MODE[0] = m
    return m


def mode():
    return MODE[0]
"""


def test_evp2_cython_code_run_at_import_writes_state_others_read(repo):
    """The Cython side: ``set_mode`` in an extension runs only while
    ``pkg.config`` is imported and writes the extension's ``MODE``; a change
    to its body reaches ``test_b``, which reads it through ``mode``."""
    if sys.version_info < (3, 13):
        pytest.skip("a profiled Cython build reports to sys.monitoring from Python 3.13")
    pytest.importorskip("Cython")
    pytest.importorskip("setuptools")
    if shutil.which("cc") is None and shutil.which("gcc") is None:
        pytest.skip("no C compiler")
    repo.commit(
        {
            **PKG,
            ".gitignore": "__pycache__/\n.diffcone/\nbuild/\n*.c\n*.so\n*.pyd\n",
            "setup.py": "from setuptools import setup\nfrom Cython.Build import cythonize\n\n"
            'setup(ext_modules=cythonize("pkg/_state.pyx"))\n',
            "pkg/_state.pyx": CYTHON_STATE,
            "pkg/config.py": 'from pkg._state import set_mode\n\nX = set_mode("slow")\n',
            "tests/test_a.py": IMPORTS_CONFIG,
            "tests/test_b.py": READS_MODE.replace("pkg.state", "pkg._state"),
        }
    )
    subprocess.run(
        [sys.executable, "setup.py", "-q", "build_ext", "--inplace"],
        cwd=repo.path,
        check=True,
        capture_output=True,
    )
    ev = repo.collect()
    assert "pkg/_state.pyx::set_mode" in ev.import_by
    head = repo.commit({"pkg/_state.pyx": CYTHON_STATE.replace("= m\n", "= m + '!'\n")})
    plan = _evidence_plan(repo, ev.commit, head, ev)
    assert selected(plan) == {A_ID, B_ID}


# The strata shape: escalating a test module follows the code that runs when
# it is imported, not the bodies of its fixtures.
FIXTURE_MODULE = """\
import pytest

from pkg.state import set_mode

CASES = {cases}


@pytest.fixture{fixture_args}
def fast():
    set_mode("fast")
    yield


@pytest.mark.parametrize("c", CASES)
def test_a(c):
    pass


def test_r(fast):
    pass
"""


@pytest.mark.parametrize(
    "fixture_args, readers",
    [
        # The fixture's body runs in the tests that use it, which the
        # recording saw: a changed parameter list changes nothing it does.
        ("", False),
        ('(scope="module")', False),
        # Its decorator's argument calls the writer as the module is
        # imported: what that writes reaches every reader.
        ('(params=[set_mode("fast")])', True),
    ],
)
def test_evp6_a_fixture_body_is_not_import_time_code(repo, fixture_args, readers):
    """``CASES`` changing escalates the test module; before, that followed
    every fixture's body as if it ran at import, so a fixture writing shared
    state selected every test reading it (strata: one changed test-data list
    selected 6 175 of 6 231 tests)."""
    module = FIXTURE_MODULE.format(cases='["x"]', fixture_args=fixture_args)
    files = {**PKG, "pkg/state.py": STATE, "tests/test_b.py": READS_MODE}
    files["tests/test_a.py"] = module
    base = repo.commit(files)
    ev = repo.collect()
    changed = FIXTURE_MODULE.format(cases='["x", "y"]', fixture_args=fixture_args)
    head = repo.commit({"tests/test_a.py": changed})
    plan = _evidence_plan(repo, base, head, ev)
    assert {A_ID, "tests/test_a.py::test_r"} <= selected(plan)
    # (test_b may be selected for its own reasons: the recording saw it stat
    # tests/test_a.py.) Only the decorator's call reaches it as a reader.
    (b,) = [d for d in plan.decisions if d.target.runner_id == B_ID]
    assert ("executed_reader" in {r.rule for r in b.reasons}) is readers


# A test module whose data list changes (strata's MALFORMED), read by its
# top-level code.
DATA_TEST = (
    "import pytest\n\nCASES = [1, 2]\n\n\n"
    "@pytest.mark.parametrize('case', CASES)\ndef test_data(case):\n    assert case\n"
)

# A lookup on builtins, which library code writes to (only inside tests).
BUILTINS = {
    "pkg/names.py": (
        "import builtins\n\n\ndef lookup(name):\n    return getattr(builtins, name, None)\n"
    ),
    "pkg/extra.py": "import builtins\n\n\ndef install():\n    builtins.diffcone_x = 1\n",
    "tests/test_names.py": (
        "from pkg.names import lookup\n\n\ndef test_names():\n    assert lookup('len') is len\n"
    ),
    "tests/test_install.py": (
        "import builtins\n\nfrom pkg.extra import install\n\n\n"
        "def test_install():\n    install()\n    del builtins.diffcone_x\n"
    ),
}
BUILTIN_NAMES_ID = "tests/test_names.py::test_names"


@pytest.mark.parametrize(
    "data, reached",
    [
        (DATA_TEST, False),
        (DATA_TEST.replace("CASES = [1, 2]", "CASES = [len(str(1)), 2]"), False),
        (
            "from pkg.extra import install\n\n"
            + DATA_TEST.replace("CASES = [1, 2]", "CASES = [1, 2]")
            + "\n\nSET_UP = install()\n",
            True,
        ),
    ],
    ids=["a literal", "a computed value", "import code that runs the writer"],
)
def test_w1_import_time_code_reaches_an_external_lookup_only_through_a_writer(repo, data, reached):
    # strata: ``MALFORMED = [...]`` changed in a test module, whose top-level
    # code (a parametrize) reads it; configure_logging's ``getattr(logging,
    # level)`` is a lookup on a module library code writes to. The changed
    # import-time code can run no writer, so the lookup cannot see it.
    files = {**BUILTINS, "tests/test_data.py": data}
    base = repo.commit({**PKG, **files})
    ev = repo.collect()
    head = repo.commit({"tests/test_data.py": data.replace("2]", "2, 3]")})
    plan = _evidence_plan(repo, base, head, ev)
    assert (BUILTIN_NAMES_ID in selected(plan)) is reached


@pytest.mark.parametrize("imported", ["pkg.boot", "pkg.quiet"])
def test_w1_a_new_import_runs_what_the_imported_module_runs_at_import(repo, imported):
    # The import-time code that changed is a new import: what the imported
    # module's own import runs (pkg.boot runs the writer) now runs there.
    files = {
        **BUILTINS,
        "pkg/boot.py": "from pkg.extra import install\n\ninstall()\n",
        "pkg/quiet.py": "QUIET = 1\n",
        "tests/test_data.py": DATA_TEST,
    }
    base = repo.commit({**PKG, **files})
    ev = repo.collect()
    head = repo.commit({"tests/test_data.py": f"import {imported}  # noqa: F401\n" + DATA_TEST})
    plan = _evidence_plan(repo, base, head, ev)
    assert (BUILTIN_NAMES_ID in selected(plan)) is (imported == "pkg.boot")


NOOP_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}

MODES = "def mode():\n    return 'a'\n"
MODES_B = MODES.replace("'a'", "'b'")

READS_ENV = "import os\n\n\ndef test_env():\n    assert os.environ.get('MODE', 'a') in 'ab'\n"
QUIET = "def test_quiet():\n    pass\n"
ENV, SUB = "tests/other/test_env.py::test_env", "tests/sub/test_s.py::test_s"
QUIET_ID = "tests/test_quiet.py::test_quiet"

SETS_AT_IMPORT = """\
import os

from pkg.modes import mode

os.environ['MODE'] = mode()


def test_sets():
    pass
"""
SETS = "tests/test_sets.py::test_sets"
IMPORT_FILES = {
    **NOOP_BASE,
    "pkg/modes.py": MODES,
    "tests/other/__init__.py": "",
    "tests/other/test_env.py": READS_ENV,
    "tests/test_quiet.py": QUIET,
}


def test_w12_import_time_write_in_evidence_mode(repo):
    """Under evidence the import ran ``mode`` outside every test, so the
    change escalates the test module statically, which now reaches every
    test."""
    base = repo.commit({**IMPORT_FILES, "tests/test_sets.py": SETS_AT_IMPORT})
    evidence = repo.collect()
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _noop_plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {SETS, ENV, QUIET_ID}


CHILD_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/other.py": "def g():\n    return 3\n",
    "tests/__init__.py": "",
    "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    assert g() == 3\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.other import g\n\n\ndef time_g():\n    g()\n",
}
G_BENCH = asv_target("bench.time_g", "benchmarks.bench.time_g")

REGISTRY_MODULE = "REG = {}\n\n\ndef get():\n    return REG.get('k')\n"
READS_REG = "from pkg.registry import get\n\n\ndef test_b():\n    assert get() == 'slow'\n"
WRITER = "def set_mode(d, v='slow'):\n    d['k'] = v\n"
CALLS_AT_IMPORT = "from pkg.registry import REG\nfrom pkg.state import set_mode\n\nset_mode(REG)\n"
W4_FILES = {
    "pkg/registry.py": REGISTRY_MODULE,
    "pkg/state.py": WRITER,
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": CALLS_AT_IMPORT,
    "tests/sub/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": READS_REG,
}

RECEIVER = (
    "class Registry:\n    def __init__(self):\n        self.items = []\n\n"
    "    def register(self, x):\n        self._add(x)\n\n"
    "    def _add(self, x):\n        self.items.append(x)\n\n\n"
    "registry = Registry()\n\n\ndef get():\n    return registry.items\n"
)
RECEIVER_FILES = {
    "pkg/registry.py": RECEIVER,
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": "from pkg.registry import registry\n\nregistry.register('slow')\n",
    "tests/sub/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": (
        "from pkg.registry import get\n\n\ndef test_b():\n    assert get() == ['slow']\n"
    ),
}


@pytest.mark.parametrize(
    "case",
    ["argument: writer body", "argument: call", "receiver: writer body", "receiver: call"],
)
def test_w4_evidence_a_call_at_import_writing_through_an_argument(repo, case):
    """The same in evidence mode: test_b never runs the conftest's call."""
    if case.startswith("argument"):
        files = W4_FILES
        change = (
            {"pkg/state.py": WRITER.replace("'slow'", "'fast'")}
            if case.endswith("body")
            else {"tests/sub/conftest.py": CALLS_AT_IMPORT.replace("(REG)", "(REG, 'x')")}
        )
    else:
        files = RECEIVER_FILES
        conftest = files["tests/sub/conftest.py"]
        change = (
            {"pkg/registry.py": RECEIVER.replace("self.items.append(x)", "self.items.append(1)")}
            if case.endswith("body")
            else {"tests/sub/conftest.py": conftest.replace("slow", "fast")}
        )
    base = repo.commit({**CHILD_BASE, **files})
    evidence = repo.collect()
    head = repo.commit(change)
    plan = repo.plan(base, head, [G_BENCH], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


# A write through a module attribute and an item or method.
W15_STORE = (
    "_CACHE = {'l': [], 'x': 1, 'y': 2, 'n': {}}\nREG = []\n\n\n"
    "def get():\n    return _CACHE.get('k'), list(REG)\n"
)
W15_WRITES = {
    "item through the module": "from pkg import store\n\nstore._CACHE['k'] = 'slow'\n",
    "method through the module": "from pkg import store\n\nstore.REG.append('slow')\n",
    "item through the package": "import pkg.store\n\npkg.store._CACHE['k'] = 'slow'\n",
    "update through an alias": "import pkg.store as s\n\ns._CACHE.update(k='slow')\n",
    "method on an item": "from pkg import store\n\nstore._CACHE['l'].append('slow')\n",
    "augmented item": "from pkg import store\n\nstore._CACHE['l'] += ['slow']\n",
    "deleted item": "from pkg import store\n\ndel store._CACHE['x' if 'slow' else 'y']\n",
    "nested item": "from pkg import store\n\nstore._CACHE['n']['j'] = 'slow'\n",
    "in a function": (
        "from pkg import store\n\n\ndef fill():\n    store._CACHE['k'] = 'slow'\n\n\nfill()\n"
    ),
}


def _w15_files(how):
    return {
        "pkg/store.py": W15_STORE,
        "tests/sub/__init__.py": "",
        "tests/sub/conftest.py": W15_WRITES[how],
        "tests/sub/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": "from pkg.store import get\n\n\ndef test_b():\n    assert get()\n",
    }


@pytest.mark.parametrize("how", list(W15_WRITES))
def test_w15_evidence_a_write_through_a_module_attribute(repo, how):
    base = repo.commit({**CHILD_BASE, **_w15_files(how)})
    evidence = repo.collect()
    head = repo.commit({"tests/sub/conftest.py": W15_WRITES[how].replace("slow", "fast")})
    plan = repo.plan(base, head, [G_BENCH], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_w5_a_reflection_site_run_at_import_reaches_who_reads_what_it_built(repo):
    """``dir(pkg.reg)`` runs while ``pkg.user`` is imported, outside every
    test: the site joins E, but no test ran it. A site is a reader: one at
    module level is followed like the variable it initialises, through to
    the test reading that variable."""
    base = repo.commit(
        {
            **NOOP_BASE,
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
    plan = _noop_plan(repo, base, head, evidence)
    assert selected(plan) == {"tests/test_u.py::test_u"}


PLUG_REGISTRY = {
    "pkg/reg.py": "REG = {}\n\n\ndef names():\n    return sorted(REG)\n",
    "pkg/plug.py": "import pkg.reg\n\npkg.reg.REG['a'] = 1\n",
    "tests/test_y.py": "from pkg.reg import names\n\n\ndef test_y():\n    assert names() == []\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.reg import names\n\n\ndef time_names():\n    names()\n",
}


def test_r24_evidence_follows_a_newly_run_registering_module(repo):
    """Evidence mode: no record shows ``pkg.plug`` running at C (nothing
    imported it), so what its import-time code writes is a changed value
    for the registry's readers."""
    base = repo.commit({**PKG, **PLUG_REGISTRY, "tests/test_x.py": "def test_x():\n    pass\n"})
    evidence = repo.collect()
    head = repo.commit({"tests/test_x.py": "import pkg.plug\n\n\ndef test_x():\n    pass\n"})
    plan = _evidence_plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {"tests/test_x.py::test_x", "tests/test_y.py::test_y"}
    assert "executed_reader" in rules(plan, "tests/test_y.py::test_y")


BENCH_PKG = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
}

PRELOADED_REG = """\
REG = {}


def register(f):
    REG[f.__name__] = f()
    return f


@register
def one():
    return 1


def two():
    return 2
"""
TEST_PRELOADED_REG = """\
from pkg.reg import REG, two


def test_one():
    assert REG["one"] == 1


def test_two():
    assert two() == 2
"""
BENCH_REG = """\
from pkg.reg import one, two


class TimeReg:
    def time_one(self):
        one()

    def time_two(self):
        two()
"""
BENCH_ONE = asv_target("bench.TimeReg.time_one", "benchmarks.bench.TimeReg.time_one")
BENCH_TWO = asv_target("bench.TimeReg.time_two", "benchmarks.bench.TimeReg.time_two")
ONE, TWO = "tests/test_reg.py::test_one", "tests/test_reg.py::test_two"
THREE = "tests/test_other.py::test_three"
TEST_OTHER = "from pkg.other import three\n\n\ndef test_three():\n    assert three() == 3\n"
PRELOAD_PLUGIN = {"tests/plugin.py": "import pkg.reg  # noqa: F401\n"}


def test_r28_modules_imported_before_the_recorder_are_planned_from_the_code(
    repo, monkeypatch, capsys
):
    """Without ``site`` (``python -S``) the recorder's ``sitecustomize``
    cannot run, so ``-p tests.plugin`` from ``addopts`` imports ``pkg.reg``
    before the plugin starts: nothing records what ran then. The modules
    already imported are noted (``preloaded``), ``collect`` warns, and any
    change in one is planned from the code: a change to ``two``, which no
    import runs, still reaches both tests of the module's importers."""
    files = {
        **BENCH_PKG,
        **PRELOAD_PLUGIN,
        "pytest.ini": "[pytest]\naddopts = -p tests.plugin\n",
        "pkg/reg.py": PRELOADED_REG,
        "pkg/other.py": "def three():\n    return 3\n",
        "tests/test_reg.py": TEST_PRELOADED_REG,
        "tests/test_other.py": TEST_OTHER,
        "benchmarks/bench.py": BENCH_REG,
    }
    base = repo.commit(files)
    monkeypatch.setenv("PYTHONPATH", sysconfig.get_path("purelib"))
    command = f"{sys.executable} -S -m pytest"
    argv = ["collect", "--repo", str(repo.path), "--command", command, "--no-cache"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    assert "imported before the recorder started" in err
    assert "pkg.reg" in err and "tests.plugin" in err
    evidence = load_store(Path(out.strip()))
    assert evidence.preloaded == {"pkg", "pkg.reg", "tests", "tests.plugin"}
    assert "pkg.reg.one" not in evidence.import_phase
    head = repo.commit({"pkg/reg.py": PRELOADED_REG.replace("return 2", "return 3")})
    plan = _evidence_plan(repo, base, head, evidence, [BENCH_ONE, BENCH_TWO])
    # ``tests.plugin``, loaded for the session, imports ``pkg.reg``: its
    # import-time state reaches every test, ``test_three`` included.
    assert selected(plan) == {ONE, TWO, THREE, BENCH_TWO.runner_id}
    assert all("escalated" in rules(plan, t) for t in (ONE, TWO, THREE))
    assert plan.evidence["preloaded_modules"] == ["pkg", "pkg.reg", "tests", "tests.plugin"]
    # ``pkg.other`` was imported under the recorder: its change is precise.
    head = repo.commit(
        {"pkg/other.py": "def three():\n    return 4\n", "pkg/reg.py": PRELOADED_REG}
    )
    plan = _evidence_plan(repo, base, head, evidence, [BENCH_ONE, BENCH_TWO])
    assert selected(plan) == {THREE}


# --------------------------------------------------------------------------- registries and caches


def _evidence_selects(repo, files, change, roots=None):
    base = repo.commit({".gitignore": "__pycache__/\n.diffcone/\n", **files})
    evidence = repo.collect(source_roots=roots)
    head = repo.commit(change)
    plan = repo.plan(
        base, head, [], source_roots=roots, discover_runners=["pytest"], evidence=evidence
    )
    return selected(plan)


DECORATOR_REGISTRY = (
    "REGISTRY = []\n\n\ndef register(f):\n    REGISTRY.append(f)\n    return f\n\n\n"
    "def count():\n    return len(REGISTRY)\n"
)


def test_e3_adding_a_registering_definition_reaches_the_registrys_users(repo):
    handlers = "from lib.reg import register\n\n\n@register\ndef one():\n    return 1\n"
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "from lib import handlers\n",
            "lib/reg.py": DECORATOR_REGISTRY,
            "lib/handlers.py": handlers,
            "tests/__init__.py": "",
            "tests/test_r.py": (
                "import lib\nfrom lib.reg import count\n\n\n"
                "def test_count():\n    assert count() == 1\n"
            ),
        },
        {"lib/handlers.py": handlers + "\n\n@register\ndef two():\n    return 2\n"},
    )
    assert "tests/test_r.py::test_count" in chosen


SUBS = (
    "class Base:\n    registry = []\n\n    def __init_subclass__(cls, **kw):\n"
    "        super().__init_subclass__(**kw)\n        Base.registry.append(cls)\n\n\n"
    "def names():\n    return [c.__name__ for c in Base.registry]\n"
)


def test_e3_adding_a_subclass_whose_base_registers_it(repo):
    plugins = "from lib.base import Base\n\n\nclass A(Base):\n    pass\n"
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "from lib import plugins\n",
            "lib/base.py": SUBS,
            "lib/plugins.py": plugins,
            "tests/__init__.py": "",
            "tests/test_p.py": (
                "import lib\nfrom lib.base import names\n\n\n"
                "def test_names():\n    assert names() == ['A']\n"
            ),
        },
        {"lib/plugins.py": plugins + "\n\nclass B(Base):\n    pass\n"},
    )
    assert "tests/test_p.py::test_names" in chosen


CALC_REG = """\
from pkg.calc import compute

REG = {init}


def register(k):
    REG[k] = compute(k)


def lookup(k):
    return REG.get(k)
"""
CALC = "def compute(k):\n    return k * 2\n"

RESET = """\
import pytest

from pkg import reg


@pytest.fixture(autouse=True)
def _reset():
    yield
    {body}
"""
TEST_REGISTER = (
    "from pkg.reg import lookup, register\n\n\ndef test_register():\n"
    "    register(1)\n    assert lookup(1) in (2, 3)\n"
)
BENCH_LOOKUP = "from pkg.reg import lookup\n\n\ndef time_lookup():\n    lookup(1)\n"
REGISTER, REG_OTHER = "tests/test_reg.py::test_register", "tests/test_other.py::test_other"
LOOKUP_BENCH = asv_target("bench_reg.time_lookup", "benchmarks.bench_reg.time_lookup")


def _reg_files(init="{}", body="reg.REG.clear()", extra=None):
    return {
        **NOOP_BASE,
        "pkg/calc.py": CALC,
        "pkg/reg.py": CALC_REG.format(init=init),
        "tests/conftest.py": RESET.format(body=body),
        "tests/test_reg.py": TEST_REGISTER,
        "tests/test_other.py": "def test_other():\n    pass\n",
        "benchmarks/bench_reg.py": BENCH_LOOKUP,
        **(extra or {}),
    }


def _reg_plan(repo, base, head, **kwargs):
    return repo.plan(base, head, [NOOP_BENCH, LOOKUP_BENCH], discover_runners=["pytest"], **kwargs)


def test_w14_import_time_registration_in_evidence_mode(repo):
    """Evidence: import-time code fills the registry with other arguments,
    so the registry holds something else; ``lookup`` reads it (and the test
    running it), the fixture that only empties it does not."""
    files = _reg_files(
        extra={
            "pkg/boot.py": "from pkg.reg import register\n\nregister(5)\n",
            "tests/test_reg.py": "import pkg.boot  # noqa: F401\n" + TEST_REGISTER,
        }
    )
    base = repo.commit(files)
    evidence = repo.collect()
    head = repo.commit({"pkg/boot.py": "from pkg.reg import register\n\nregister(6)\n"})
    plan = _reg_plan(repo, base, head, evidence=evidence)
    assert REGISTER in selected(plan)
    assert REG_OTHER in unselected(plan)


TABLE_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/handlers.py": "def a():\n    return {}\n",
    "pkg/core.py": (
        "from pkg import handlers\n\nTABLE = {'k': 'a'}\n\n\n"
        "def run():\n    return getattr(handlers, TABLE['k'], None)\n"
    ),
    "tests/__init__.py": "",
    # Runs after test_a, in the same session.
    "tests/test_s.py": "from pkg.core import run\n\n\ndef test_s():\n    assert run() is None\n",
}
WRITERS = {
    "a module handed on": (
        {
            "pkg/api.py": "from pkg import core  # noqa: F401\n",
            "pkg/mutate.py": "def mutate(m):\n    m.core.TABLE['k'] = 'c'\n",
        },
        "from pkg import api\nfrom pkg.mutate import mutate\n\n\ndef test_a():\n    mutate(api)\n",
    ),
    "a function's globals": (
        {"pkg/mutate.py": "def mutate(f):\n    f.__globals__['TABLE']['k'] = 'c'\n"},
        "from pkg.core import run\nfrom pkg.mutate import mutate\n\n\n"
        "def test_a():\n    mutate(run)\n",
    ),
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_w16_w18_a_table_written_elsewhere_in_evidence_mode(repo, writer):
    # test_a points pkg.core.TABLE at ``c``, which handlers lacks at C; the
    # change adds it. test_s runs after test_a and now gets 1: its lookup
    # must see the added name.
    files, test_a = WRITERS[writer]
    base = repo.commit({**TABLE_BASE, **files, "tests/test_a.py": test_a})
    ev = repo.collect()
    head = repo.commit(
        {"pkg/handlers.py": TABLE_BASE["pkg/handlers.py"] + "\n\ndef c():\n    return 1\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert "tests/test_s.py::test_s" in selected(plan)
    assert "lookup_site" in rules(plan, "tests/test_s.py::test_s")


def reasons_of(plan, runner_id):
    (decision,) = [d for d in plan.decisions if d.target.runner_id == runner_id]
    return decision.reasons


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
# Filled through code that runs nothing of ``compute`` (audit E36).
HELPER_LIB = """\
_CACHE = {}


def compute(k):
    return k * 2


def _put(k, v):
    _CACHE[k] = v


def get(k):
    if k not in _CACHE:
        _put(k, compute(k))
    return _CACHE[k]
"""
METHOD_HELPER_LIB = """\
_CACHE = {}


def compute(k):
    return k * 2


class C:
    def _put(self, k, v):
        _CACHE[k] = v

    def get(self, k):
        if k not in _CACHE:
            self._put(k, compute(k))
        return _CACHE[k]


def get(k):
    return C().get(k)
"""
TWO_HELPERS_LIB = HELPER_LIB.replace(
    "def _put(k, v):\n    _CACHE[k] = v\n",
    "def _put2(k, v):\n    _CACHE[k] = v\n\n\ndef _put(k, v):\n    _put2(k, v)\n",
)
FILLER_LIB = HELPER_LIB.replace(
    "def _put(k, v):\n    _CACHE[k] = v\n",
    "def _fill(d, k, v):\n    d[k] = v\n\n\ndef _put(k, v):\n    _fill(_CACHE, k, v)\n",
)
APPEND_LIB = """\
_CACHE = []


def compute(k):
    return k * 2


def _keep(v):
    _CACHE.append(v)


def get(k):
    if not _CACHE:
        _keep(compute(k))
    return _CACHE[0]
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
        (HELPER_LIB, USES, ("k * 2", "k + k")),
        (METHOD_HELPER_LIB, USES, ("k * 2", "k + k")),
        (TWO_HELPERS_LIB, USES, ("k * 2", "k + k")),
        (APPEND_LIB, USES, ("k * 2", "k + k")),
        (FILLER_LIB, USES, ("k * 2", "k + k")),
    ],
    ids=[
        "direct",
        "through a helper",
        "a method by name",
        "e36 stored by a helper",
        "e36 stored by a method",
        "e36 two helpers down",
        "e36 appended by a helper",
        "e36 through a writer of its parameter",
    ],
)
def test_w7_a_module_cache_filled_by_an_earlier_test(repo, lib, uses, edit):
    """test_m1 and test_m2 run between test_a and test_z in both orders, so
    the cache is always filled when they run and their records never hold
    ``compute``: the reverse-order check cannot flag them. ``get`` can run
    ``compute`` and writes ``_CACHE``, or hands what it computed to code
    that does (audit E36), so the tests that read ``_CACHE`` are
    selected."""
    second = uses.format("m2").split("\n\n\n", 1)[1]
    base = repo.commit(
        {
            **NOOP_BASE,
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
    plan = _noop_plan(repo, base, head, evidence)
    assert selected(plan) == CACHE_TESTS
    assert "stores into pkg.lib._CACHE" in next(
        r.detail for r in reasons_of(plan, "tests/test_m.py::test_m1")
    )


# What ``get`` writes into ``_SEEN`` (audit E35): only a value derived from
# code that can run ``compute`` may hold what it computed for a later test.
SEEN_LIB = """\
_SEEN = {{}}


def compute(k):
    return k * 2


def get(k):
{0}
    return compute(k)


def seen():
    return len(_SEEN)


def _fill(d, key):
    d[key] = True


def _mark(key):
    _SEEN[key] = True
"""
SEEN_TESTS = {
    "tests/test_a.py": "from pkg.lib import get\n\n\ndef test_a():\n    assert get(2) == 4\n",
    "tests/test_s.py": "from pkg.lib import seen\n\n\ndef test_s():\n    assert seen() >= 0\n",
}


@pytest.mark.parametrize(
    "write, stored",
    [
        ('    _SEEN["k"] = True', False),
        ("    _SEEN.clear()", False),
        ('    _SEEN.setdefault("k", [])', False),
        ("    _SEEN[k] = True", False),
        ('    _fill(_SEEN, "k")', False),
        ("    _fill(_SEEN, compute(k))", True),
        ('    _mark("k")', False),
        ("    _mark(compute(k))", True),
        ("    _mark(k)", False),
        ("    _SEEN[k] = compute(k)", True),
        ("    _SEEN[compute(k)] = True", True),
        ("    v = compute(k)\n    _SEEN[k] = v", True),
        ("    if compute(k) > 2:\n        _SEEN[k] = True", True),
        ("    for x in range(compute(k)):\n        _SEEN[x] = True", True),
        ("    with open(str(compute(k))) as f:\n        _SEEN[k] = f", True),
        ("    _SEEN.update({k: compute(k)})", True),
        ("    _SEEN.update(k=get)", True),
        ('    compute(k)\n    _SEEN["k"] = True', True),
        ('    if compute(k) > 2:\n        return 0\n    _SEEN["k"] = True', True),
        ('    try:\n        compute(k)\n    except ValueError:\n        _SEEN["k"] = True', True),
        ('    compute(k) and _SEEN.setdefault("k", 1)', True),
        ('    for _ in range(2):\n        _SEEN["k"] = True\n        compute(k)', True),
    ],
    ids=[
        "a literal",
        "emptied",
        "a literal key",
        "keyed by a parameter",
        "through a writer of its parameter",
        "the result through a writer of its parameter",
        "a helper handed a literal",
        "a helper handed the result",
        "a helper handed a parameter",
        "the result",
        "the key",
        "through a local",
        "under a branch on it",
        "in a loop over it",
        "in a with on it",
        "a mutator's argument",
        "the chain handed on",
        "after a call to it",
        "after an early return",
        "in a handler",
        "in an expression with it",
        "in a loop calling it",
    ],
)
def test_e35_a_variable_written_by_the_chain_holds_only_what_derives_from_it(repo, write, stored):
    """``get`` can run ``compute`` and writes ``_SEEN``; ``test_s`` reads
    ``_SEEN`` through ``seen`` and runs nothing of ``compute``. A store of
    what does not derive from the chain holds nothing ``compute`` changes;
    a parameter is what the test handed, and code the function runs before
    the write (a call that raises, an early return) decides whether it is
    made."""
    lib = SEEN_LIB.format(write)
    base = repo.commit({**NOOP_BASE, **SEEN_TESTS, "pkg/lib.py": lib})
    evidence = repo.collect()
    head = repo.commit({"pkg/lib.py": lib.replace("return k * 2", "return k + k")})
    plan = _noop_plan(repo, base, head, evidence)
    expected = {"tests/test_a.py::test_a"}
    if stored:
        expected.add("tests/test_s.py::test_s")
    assert selected(plan) == expected


@pytest.mark.parametrize("caller", ["get(compute(k))", "get(k + 1)"])
def test_e35_a_parameter_holds_what_a_caller_in_the_chain_computed(repo, caller):
    """``get`` stores its parameter; ``twice`` hands it ``compute``'s result,
    so what ``_SEEN`` holds may change with ``compute``. Handed ``k + 1``,
    it holds what the test computed."""
    lib = SEEN_LIB.format("    _SEEN[k] = True") + f"\n\ndef twice(k):\n    return {caller}\n"
    tests = {
        **SEEN_TESTS,
        "tests/test_a.py": (
            "from pkg.lib import twice\n\n\ndef test_a():\n    assert twice(2) > 0\n"
        ),
    }
    base = repo.commit({**NOOP_BASE, **tests, "pkg/lib.py": lib})
    evidence = repo.collect()
    head = repo.commit({"pkg/lib.py": lib.replace("return k * 2", "return k + k")})
    plan = _noop_plan(repo, base, head, evidence)
    expected = {"tests/test_a.py::test_a"}
    if caller == "get(compute(k))":
        expected.add("tests/test_s.py::test_s")
    assert selected(plan) == expected


ROUTING = """\
class Router:
    def __init__(self):
        self.routes = {}

    def put(self, path):
        def deco(f):
            self.routes[path] = f
            return f

        return deco


router = Router()


def path():
    return "/x"
"""
ENDPOINT = """\
from pkg.lib import compute
from pkg.routing import path, router


@router.put({0})
def endpoint(k):
{1}    return compute(k)
"""
ROUTER_TESTS = {
    "tests/test_a.py": (
        "from pkg.endpoint import endpoint\n\n\ndef test_a():\n    assert endpoint(2) == 4\n"
    ),
    "tests/test_r.py": (
        "import pkg.endpoint\nfrom pkg.routing import router\n\n\n"
        "def test_r():\n    assert router.routes\n"
    ),
    "tests/test_c.py": (
        "import pkg.endpoint\nfrom pkg.routing import router\n\n\n"
        "def test_c():\n    assert router.routes['/x'](2) == 4\n"
    ),
}


@pytest.mark.parametrize(
    "edit, decorator, body, readers",
    [
        ("compute", "'/x'", "", False),
        ("path", "path()", "", True),
        ("compute", "'/x'", "    router.put(str(compute(k)))(endpoint)\n", True),
    ],
    ids=["a body change", "the decorator's own call changed", "the body registers too"],
)
def test_e32_a_decorators_registration_is_no_stored_result(repo, edit, decorator, body, readers):
    """``@router.put('/x')`` writes ``router`` when the ``def`` runs, at
    import, and stores the function: a body change of what ``endpoint``
    runs reaches the tests that run it (``test_a``, ``test_c`` through the
    router), not ``test_r``, which only reads the router. A change to code
    the decorator runs reaches it through the import rules, and a body
    that writes the router too stores what it computes."""
    lib = "def compute(k):\n    return k * 2\n"
    files = {
        **NOOP_BASE,
        **ROUTER_TESTS,
        "pkg/lib.py": lib,
        "pkg/routing.py": ROUTING,
        "pkg/endpoint.py": ENDPOINT.format(decorator, body),
    }
    base = repo.commit(files)
    evidence = repo.collect()
    changed = (
        {"pkg/lib.py": lib.replace("k * 2", "k + k")}
        if edit == "compute"
        else {"pkg/routing.py": ROUTING.replace('return "/x"', 'return "/" + "x"')}
    )
    head = repo.commit(changed)
    plan = _noop_plan(repo, base, head, evidence)
    expected = {"tests/test_a.py::test_a", "tests/test_c.py::test_c"}
    if readers:
        expected.add("tests/test_r.py::test_r")
    assert selected(plan) == expected


HANDLER_TABLES = (
    "def a():\n    return 1\n\n\ndef b():\n    return {0}\n\n\nTABLE = {{'a': a}}\n"
    "LIST = [a]\nNEST = {{'k': [a]}}\n\n\nclass O:\n    x = a\n\n\nOBJ = O()\n"
)
TABLE_READERS = {
    "TABLE": "def read():\n    return TABLE['x']()\n",
    "LIST": "def read():\n    return LIST[-1]()\n",
    "NEST": "def read():\n    return NEST['k'][-1]()\n",
    "OBJ": "def read():\n    return OBJ.x()\n",
}

BOTH = {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_l6_evidence_a_changed_writer_through_a_handle_selects_the_readers(repo):
    """The recorded run of ``test_b`` executed ``read`` only; ``go`` (run by
    ``test_a`` first) put ``b`` in the table through a handle. Changing
    ``go`` to put ``c`` there changes ``test_b``'s outcome."""
    reg = HANDLER_TABLES.format("2") + "\n\ndef c():\n    return 3\n"
    writer = (
        "import importlib\n\nimport pkg.reg\n\n\ndef go():\n"
        "    importlib.import_module('pkg.reg').TABLE['x'] = pkg.reg.{0}\n"
    )
    files = {
        ".gitignore": "__pycache__/\n.diffcone/\n",
        "pkg/__init__.py": "",
        "pkg/reg.py": reg,
        "pkg/use.py": "from pkg.reg import TABLE\n\n\n" + TABLE_READERS["TABLE"],
        "pkg/w.py": writer.format("b"),
        "tests/__init__.py": "",
        "tests/test_a.py": "import pkg.w\n\n\ndef test_a():\n    pkg.w.go()\n",
        "tests/test_b.py": (
            "from pkg.use import read\n\n\ndef test_b():\n    assert read() in (2, 3)\n"
        ),
    }
    base = repo.commit(files)
    evidence = repo.collect()
    head = repo.commit({"pkg/w.py": writer.format("c")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == BOTH


# The isolation assumption (the user's decision): a store into module state
# made by code that runs only inside one test (the test, a function-scoped
# fixture, a helper of the test module only tests call) is seen by that test
# alone, so it is no dependency of the readers in other tests or benchmarks.
# Code that runs at collection (a test module's top level), a wider-scoped
# fixture, and project code (a cache filled from a test) stay writers.
ISO = {
    "pkg/__init__.py": "",
    "pkg/server.py": "_state = None\n\n\ndef state():\n    return _state\n",
    "pkg/impl.py": "def make():\n    return {0}\n",
    "pkg/cache.py": (
        "import pkg.impl\n\n_CACHE = {}\n\n\ndef fill():\n    _CACHE['k'] = pkg.impl.make()\n\n\n"
        "def get():\n    return _CACHE.get('k')\n"
    ),
    "tests/__init__.py": "",
    "tests/test_other.py": (
        "from pkg.server import state\n\n\ndef test_other():\n    assert state() is None\n"
    ),
    "tests/test_reads_cache.py": (
        "from pkg.cache import get\n\n\ndef test_reads_cache():\n    assert get() is None\n"
    ),
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.server import state\n\n\ndef time_state():\n    state()\n",
}
ISO_BENCH = asv_target("bench.time_state", "benchmarks.bench.time_state")
ISO_HEAD = "import pytest\n\nimport pkg.cache\nimport pkg.impl\nimport pkg.server\n\n\n"


def _iso_plan(repo, test_a: str, evidence: bool = False):
    files = {**ISO, "tests/test_a.py": test_a}
    if evidence:
        files[".gitignore"] = "__pycache__/\n.diffcone/\n"
    base = repo.commit({**files, "pkg/impl.py": ISO["pkg/impl.py"].format("1")})
    recorded = repo.collect() if evidence else None
    head = repo.commit({"pkg/impl.py": ISO["pkg/impl.py"].format("2")})
    return repo.plan(base, head, [ISO_BENCH], discover_runners=["pytest"], evidence=recorded)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_l7_evidence_a_store_by_a_test_helper_reaches_only_its_test(repo):
    test_a = (
        ISO_HEAD
        + "def _put():\n    pkg.server._state = pkg.impl.make()\n\n\n"
        + ("def test_a():\n    _put()\n    pkg.server._state = None\n")
    )
    plan = _iso_plan(repo, test_a, evidence=True)
    assert "tests/test_a.py::test_a" in selected(plan)
    assert "tests/test_other.py::test_other" not in selected(plan)


READER_A, READER_B, READER_C = (
    "tests/test_a.py::test_a",
    "tests/test_b.py::test_b",
    "tests/test_c.py::test_c",
)

READERS = {
    "tests/test_a.py": "from pkg.thing import OBJ\n\n\ndef test_a():\n    assert OBJ.get(1) == 2\n",
    "tests/test_b.py": "from pkg.thing import OBJ\n\n\ndef test_b():\n    assert OBJ.get(1) == 2\n",
    "tests/test_c.py": "from pkg.thing import OBJ, other\n\n\ndef test_c():\n"
    "    assert OBJ.get(1) == 2\n    assert other() == 10\n",
}
OTHER_FUNCTION = "\n\ndef other():\n    return 10\n"
CACHED_PROPERTY = {
    "pkg/thing.py": """\
import functools


def compute():
    return 1


class Thing:
    @functools.cached_property
    def value(self):
        return compute()

    def get(self, x):
        return self.value + x


OBJ = Thing()
"""
    + OTHER_FUNCTION
}
CLOSURE_MEMO = {
    "pkg/thing.py": """\
def memo(f):
    cache = {}

    def wrapper(x):
        if x not in cache:
            cache[x] = f(x)
        return cache[x]

    return wrapper


def compute(x):
    return x + 1


@memo
def cached(x):
    return compute(x)


class Thing:
    def get(self, x):
        return cached(x)


OBJ = Thing()
"""
    + OTHER_FUNCTION
}
CLASS_CACHE = {
    "pkg/thing.py": """\
def compute(x):
    return x + 1


class Thing:
    _cache = {}

    @classmethod
    def get(cls, x):
        if x not in cls._cache:
            cls._cache[x] = compute(x)
        return cls._cache[x]


OBJ = Thing
"""
    + OTHER_FUNCTION
}
INSTANCE_CACHE = {
    "pkg/thing.py": """\
def compute(x):
    return x + 1


class Thing:
    def __init__(self):
        self._cache = {}

    def get(self, x):
        if x not in self._cache:
            self._cache[x] = compute(x)
        return self._cache[x]


OBJ = Thing()
"""
    + OTHER_FUNCTION
}
FUNCTION_ATTRIBUTE = {
    "pkg/thing.py": """\
def compute(x):
    return x + 1


def get(x):
    if x not in get.cache:
        get.cache[x] = compute(x)
    return get.cache[x]


get.cache = {}


class Thing:
    def get(self, x):
        return get(x)


OBJ = Thing()
"""
    + OTHER_FUNCTION
}
FIXTURE_HELD = {
    "pkg/thing.py": CACHED_PROPERTY["pkg/thing.py"].replace("\n\nOBJ = Thing()\n", "\n"),
    "tests/conftest.py": "import pytest\n\nfrom pkg.thing import Thing\n\n\n"
    "@pytest.fixture(scope='session')\ndef thing():\n    return Thing()\n",
    "tests/test_a.py": "def test_a(thing):\n    assert thing.get(1) == 2\n",
    "tests/test_b.py": "def test_b(thing):\n    assert thing.get(1) == 2\n",
    "tests/test_c.py": "from pkg.thing import other\n\n\ndef test_c(thing):\n"
    "    assert thing.get(1) == 2\n    assert other() == 10\n",
}
BENCH_THING = asv_target("bench.TimeThing.time_get", "benchmarks.bench.TimeThing.time_get")
BENCH_OTHER = asv_target("bench.TimeThing.time_other", "benchmarks.bench.TimeThing.time_other")
BENCH_THING_SRC = """\
from pkg.thing import compute, other


class TimeThing:
    def time_get(self):
        compute

    def time_other(self):
        other()
"""


@pytest.mark.parametrize(
    "files",
    [CACHED_PROPERTY, CLOSURE_MEMO, CLASS_CACHE, INSTANCE_CACHE, FUNCTION_ATTRIBUTE, FIXTURE_HELD],
    ids=[
        "cached_property on a module singleton",
        "closure memo decorator",
        "class attribute cache",
        "instance cache on a singleton",
        "function attribute cache",
        "cached_property on a session fixture's object",
    ],
)
def test_r22_a_cache_outside_a_module_variable_reaches_every_reader(repo, files):
    """The first test fills the cache by running ``compute``; the others
    read the stored value and never run it, so only the first test's record
    holds ``compute``. The code storing it (through ``self``, ``cls``, a
    closure or a function attribute) is a cache every test that executed it
    may read from; a ``cached_property`` serves its value to readers that
    run nothing of it, so its readers are followed. All three tests fail
    after the change and all three are selected."""
    base = repo.commit({**BENCH_PKG, **READERS, **files, "benchmarks/bench.py": BENCH_THING_SRC})
    evidence = repo.collect()
    head = repo.commit(
        {
            "pkg/thing.py": files["pkg/thing.py"]
            .replace("x + 1", "x + 2")
            .replace("return 1\n", "return 2\n", 1)
        }
    )
    plan = _evidence_plan(repo, base, head, evidence, [BENCH_THING, BENCH_OTHER])
    # Statically, ``get.cache = {}`` at module level refers to ``get``, so
    # its callee's change is import-time state for every importer.
    benches = {BENCH_THING.runner_id} | (
        {BENCH_OTHER.runner_id} if files is FUNCTION_ATTRIBUTE else set()
    )
    assert selected(plan) == {READER_A, READER_B, READER_C} | benches
    assert rules(plan, READER_B) == {"executed_reader"}
    # Precision: a change to code the cache cannot hold is still read only
    # by the test that ran it.
    head = repo.commit({"pkg/thing.py": files["pkg/thing.py"].replace("return 10", "return 11")})
    plan = _evidence_plan(repo, base, head, evidence, [BENCH_THING, BENCH_OTHER])
    assert selected(plan) == {READER_C, BENCH_OTHER.runner_id}


# A lazily initialised constant (pandas' ``find_stack_level``: ``global
# _pkg_dir; if _pkg_dir is None: _pkg_dir = os.path.dirname(pd.__file__)``)
# holds the same value whoever fills it first: running its writer at import
# or in a hook changes nothing the writer later reads.
LAZY_EXC = """\
import os

_pkg_dir: str | None = None
_test_dir = None


def find_stack_level():
    global _pkg_dir, _test_dir
    if _pkg_dir is None or _test_dir is None:
        import pkg as pd

        _pkg_dir = os.path.dirname(pd.__file__) + os.sep
        _test_dir = os.path.join(_pkg_dir, "tests") + os.sep
    return len(_pkg_dir) + len(_test_dir)
"""
LAZY_HOOK = (
    "def pytest_configure(config):\n    from pkg.warn import deprecate\n\n    deprecate('c')\n"
)
LAZY_FILES = {
    **BASE,
    "pkg/exc.py": LAZY_EXC,
    "pkg/warn.py": (
        "from pkg.exc import find_stack_level\n\n\ndef deprecate(msg):\n"
        "    return find_stack_level()\n"
    ),
    "pkg/core.py": "from pkg.warn import deprecate\n\nLEVEL = deprecate('x')\n",
    "conftest.py": LAZY_HOOK,
    "tests/test_core.py": "from pkg.core import LEVEL\n\n\ndef test_level():\n    assert LEVEL\n",
    "tests/test_stack.py": (
        "from pkg.warn import deprecate\n\n\ndef test_stack():\n    assert deprecate('y')\n"
    ),
}
LEVEL_ID, STACK_ID = "tests/test_core.py::test_level", "tests/test_stack.py::test_stack"
DEPRECATE_BENCH = asv_target("bench_warn.time_deprecate", "pkg.warn.deprecate")
_FILL = '        _test_dir = os.path.join(_pkg_dir, "tests") + os.sep\n'
# What makes the variables no lazy constants: each keeps the old plan.
NOT_LAZY = {
    "value from a parameter": {
        "pkg/exc.py": LAZY_EXC.replace(
            "find_stack_level():", "find_stack_level(sub='tests'):"
        ).replace('"tests") + os.sep', "sub) + os.sep")
    },
    "read outside the writer": {
        "pkg/exc.py": LAZY_EXC + "\n\ndef pkg_dir():\n    return _pkg_dir\n",
        "conftest.py": LAZY_HOOK + "    from pkg.exc import pkg_dir\n\n    pkg_dir()\n",
    },
    "store without the guard": {
        "pkg/exc.py": LAZY_EXC.replace(_FILL, "").replace(
            "    return len",
            "    _test_dir = os.path.join(_pkg_dir, 'tests') + os.sep\n    return len",
        )
    },
    "a second writer": {
        "pkg/exc.py": LAZY_EXC + "\n\ndef reset():\n    global _test_dir\n    _test_dir = None\n"
    },
    "a project attribute": {
        "pkg/__init__.py": "HOME = __file__\n",
        "pkg/exc.py": LAZY_EXC.replace("pd.__file__", "pd.HOME"),
    },
    "deleted": {
        "pkg/exc.py": LAZY_EXC + "\n\ndef clear():\n    global _pkg_dir\n    del _pkg_dir\n"
    },
}


def _lazy_evidence_plan(repo, files):
    base = repo.commit(files)
    evidence = repo.collect()
    head = repo.commit({"pkg/core.py": files["pkg/core.py"].replace("'x'", "'xy'")})
    return _evidence_plan(repo, base, head, evidence, [DEPRECATE_BENCH])


def test_a_lazy_constant_filled_in_a_hook_is_no_state_for_later_tests(repo):
    """pandas: changed import-time code can run ``find_stack_level``, which
    a hook also ran. Its ``_pkg_dir`` used to count as state the hook left
    for every later test; the value is the same whoever computes it first,
    and only ``find_stack_level`` reads it, so only the change's own readers
    are selected (statically too: not the benchmark of ``deprecate``)."""
    plan = _lazy_evidence_plan(repo, LAZY_FILES)
    assert selected(plan) == {LEVEL_ID}


def test_a_test_reading_a_lazy_constant_sees_when_it_was_filled(repo):
    """A test reading the variable itself may read it before or after the
    writer ran: it is selected (and the benchmark, statically)."""
    files = {
        **LAZY_FILES,
        "tests/test_dir.py": (
            'import pkg.exc\n\n\ndef test_dir():\n    assert pkg.exc._pkg_dir != ""\n'
        ),
    }
    plan = _lazy_evidence_plan(repo, files)
    assert selected(plan) == {LEVEL_ID, "tests/test_dir.py::test_dir", DEPRECATE_BENCH.runner_id}


@pytest.mark.parametrize("case", sorted(NOT_LAZY))
def test_a_variable_that_is_no_lazy_constant_stays_state(repo, case):
    """Guard: a value computed from a parameter or a project attribute, a
    read outside the writer that ran in the hook, a store without the
    ``is None`` guard, a second writer, a ``del``: what the hook left may
    differ for a later test, so every test is selected as before."""
    plan = _lazy_evidence_plan(repo, {**LAZY_FILES, **NOT_LAZY[case]})
    assert selected(plan) == {LEVEL_ID, STACK_ID, DEPRECATE_BENCH.runner_id, ADD, MUL}
    assert "pytest_hook_changed" in rules(plan, STACK_ID)


# --------------------------------------------------------------------------- hooks and plugins


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
LIFECYCLE_A, LIFECYCLE_B = "tests/test_a.py::test_a", "tests/sub/test_b.py::test_b"
LIFECYCLE_FILES = {
    **NOOP_BASE,
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
            {LIFECYCLE_B},
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
            {LIFECYCLE_B},
        ),
        # The ini option ``usefixtures``.
        (
            {"tests/conftest.py": TURN_ON.format("", "on"), "pytest.ini": "[pytest]\n"},
            {"pytest.ini": "[pytest]\nusefixtures = on\n"},
            {LIFECYCLE_A, LIFECYCLE_B, NOOP_BENCH.runner_id},
        ),
        # A plugin loaded with ``-p`` in addopts.
        (
            {"tests/plugin.py": TURN_ON.format("(autouse=True)", "on"), "pytest.ini": "[pytest]\n"},
            {"pytest.ini": "[pytest]\naddopts = -p tests.plugin\n"},
            {LIFECYCLE_A, LIFECYCLE_B, NOOP_BENCH.runner_id},
        ),
        # A plugin another test module registers with ``pytest_plugins``.
        (
            {"tests/plugin.py": TURN_ON.format("(autouse=True)", "on")},
            {"tests/test_a.py": "pytest_plugins = ['tests.plugin']\n\n\n" + TEST_A},
            {LIFECYCLE_A, LIFECYCLE_B},
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
    plan = _noop_plan(repo, base, head, evidence)
    if since == "base":  # the benchmark is planned statically, base -> head
        picked = picked - {NOOP_BENCH.runner_id}
    assert selected(plan) == picked
    assert "lifecycle_changed" in rules(plan, LIFECYCLE_B)
    assert "since the recording" in next(
        r.detail for r in reasons_of(plan, LIFECYCLE_B) if r.rule == "lifecycle_changed"
    )
    if NOOP_BENCH.runner_id not in picked:  # pytest's configuration changed: everything
        assert NOOP_BENCH.runner_id in unselected(plan)


ENVSETUP = (
    "import os\n\nfrom pkg.modes import mode\n\n\ndef apply():\n    os.environ['MODE'] = mode()\n"
)
REQUIRES_MODE_ENV = "import os\n\n\ndef test_env():\n    assert os.environ['MODE'] in 'ab'\n"
SESSIONSTART_PLUGIN = """\
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


@pytest.mark.parametrize(
    "conftests",
    [{"tests/conftest.py": SESSIONSTART_PLUGIN}, {"tests/sub/conftest.py": GENERATE}],
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
            **NOOP_BASE,
            "pkg/modes.py": MODES,
            "tests/envsetup.py": ENVSETUP,
            "tests/sub/__init__.py": "",
            "tests/sub/test_s.py": "def test_s():\n    pass\n",
            "tests/other/__init__.py": "",
            "tests/other/test_env.py": REQUIRES_MODE_ENV,
            **conftests,
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/modes.py": MODES.replace("'a'", "'b'")})
    plan = _noop_plan(repo, base, head, evidence)
    assert selected(plan) == {ENV, SUB}
    assert rules(plan, ENV) == {"pytest_hook_changed"}
    assert NOOP_BENCH.runner_id in unselected(plan)


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
            **NOOP_BASE,
            "tests/test_ids.py": IDS_TEST,
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    evidence = repo.collect()
    assert evidence.collect_by["tests.test_ids.test_ids"] == {"tests.test_ids"}
    head = repo.commit({"tests/test_ids.py": IDS_TEST.replace("n > 0", "n >= 1")})
    plan = _noop_plan(repo, base, head, evidence)
    assert selected(plan) == {"tests/test_ids.py::test_ids"}


# ------------------------------------------------------------------- code run while collecting

# What pytest runs while it collects a test module (roadmap item 19, rule 3):
# ``ids=`` callables, a conftest's ``pytest_generate_tests`` and what they
# call. ``label`` builds ids only; ``noted`` also stores into ``SEEN``, which
# ``test_b`` reads; ``cases`` gives ``test_a``'s ``case`` parameter;
# ``order_key`` runs in a session hook.
COLLECTION_FMT = """\
import os

SEEN = []


def label(n):
    return f"n{n}"


def noted(n):
    SEEN.append(n)
    return f"n{n}"


def seen():
    return list(SEEN)


def exported(n):
    os.environ["LAST_ID"] = str(n)
    return f"n{n}"


def cases():
    return [1, 2]


def cases_for(metafunc):
    return [1, 2] if metafunc.config.getoption("verbose") > -9 else [1]


def order_key(item):
    return item.nodeid
"""
COLLECTION_CONFTEST = """\
from pkg.fmt import cases, order_key


def pytest_generate_tests(metafunc):
    if "case" in metafunc.fixturenames:
        metafunc.parametrize("case", cases())


def pytest_collection_modifyitems(items):
    items.sort(key=order_key)
"""
COLLECTION_A = """\
import pytest

from pkg.fmt import exported, label, noted


@pytest.mark.parametrize("n", [1, 2], ids=label)
def test_label(n):
    assert n > 0


@pytest.mark.parametrize("n", [1, 2], ids=noted)
def test_noted(n):
    assert n > 0


@pytest.mark.parametrize("n", [1, 2], ids=exported)
def test_exported(n):
    assert n > 0


def test_case(case):
    assert case > 0
"""
COLLECTION_B = """\
import os

from pkg.fmt import seen


def test_seen():
    assert isinstance(seen(), list)


def test_env():
    assert os.environ.get("LAST_ID", "2") in "12"


def test_plain():
    pass
"""
LABEL_BENCH = asv_target("bench_fmt.time_label", "benchmarks.bench_fmt.time_label")
COLLECTION_FILES = {
    **NOOP_BASE,
    "pkg/fmt.py": COLLECTION_FMT,
    "tests/conftest.py": COLLECTION_CONFTEST,
    "tests/test_a.py": COLLECTION_A,
    "tests/test_b.py": COLLECTION_B,
    "benchmarks/bench_fmt.py": "from pkg.fmt import label\n\n\ndef time_label():\n    label(1)\n",
}
TESTS_A = {
    "tests/test_a.py::test_label",
    "tests/test_a.py::test_noted",
    "tests/test_a.py::test_exported",
    "tests/test_a.py::test_case",
}
SEEN_ID = "tests/test_b.py::test_seen"
TESTS_B = {SEEN_ID, "tests/test_b.py::test_env", "tests/test_b.py::test_plain"}


def _collection_plan(repo, change, conftest=COLLECTION_CONFTEST):
    base = repo.commit({**COLLECTION_FILES, "tests/conftest.py": conftest})
    evidence = repo.collect()
    head = repo.commit(change)
    return evidence, repo.plan(
        base,
        head,
        [NOOP_BENCH, LABEL_BENCH],
        discover_runners=["pytest"],
        evidence=evidence,
    )


def test_code_that_built_one_modules_test_ids_selects_that_modules_tests(repo):
    """``label`` ran only while pytest built the ids of ``test_a``'s tests:
    its change can change those tests (their ids, which parameters run) and
    nothing that runs for another module, so ``test_b`` keeps its record.
    Before, code run while collecting counted as a hook's and selected
    everything."""
    evidence, plan = _collection_plan(
        repo,
        {"pkg/fmt.py": COLLECTION_FMT.replace('f"n{n}"\n\n\ndef noted', 'f"N{n}"\n\n\ndef noted')},
    )
    assert evidence.collect_by["pkg.fmt.label"] == {"tests.test_a"}
    assert "pkg.fmt.label" not in evidence.hook_phase
    assert selected(plan) == TESTS_A | {LABEL_BENCH.runner_id}
    assert rules(plan, "tests/test_a.py::test_case") == {"collection_code"}
    assert NOOP_BENCH.runner_id in unselected(plan)


def test_code_run_by_a_session_hook_still_selects_everything(repo):
    """``order_key`` runs in ``pytest_collection_modifyitems``, after every
    module is collected: what a hook does reaches every test."""
    evidence, plan = _collection_plan(
        repo, {"pkg/fmt.py": COLLECTION_FMT.replace("item.nodeid", "str(item.nodeid)")}
    )
    assert "pkg.fmt.order_key" in evidence.hook_phase
    assert "pkg.fmt.order_key" not in evidence.collect_by
    assert selected(plan) == TESTS_A | TESTS_B
    assert rules(plan, "tests/test_b.py::test_plain") == {"pytest_hook_changed"}


def test_state_written_while_collecting_reaches_its_readers(repo):
    """``noted`` stores into ``SEEN`` while ``test_a`` is collected, and
    ``test_b`` reads it through ``seen()``, never running ``noted``: its
    change reaches ``test_b::test_seen`` as well as ``test_a``'s tests."""
    evidence, plan = _collection_plan(
        repo, {"pkg/fmt.py": COLLECTION_FMT.replace("SEEN.append(n)", "SEEN.append(n * 1)")}
    )
    assert evidence.collect_by["pkg.fmt.noted"] == {"tests.test_a"}
    assert selected(plan) == TESTS_A | {SEEN_ID}
    assert rules(plan, SEEN_ID) == {"executed_reader"}


def test_process_state_written_while_collecting_selects_everything(repo):
    """``exported`` sets an environment variable while ``test_a`` is
    collected; any later test may read it without a static path (``test_b``
    does): written to the process, it is a hook's doing."""
    evidence, plan = _collection_plan(
        repo, {"pkg/fmt.py": COLLECTION_FMT.replace("str(n)\n", "str(n + 0)\n")}
    )
    assert evidence.collect_by["pkg.fmt.exported"] == {"tests.test_a"}
    assert selected(plan) == TESTS_A | TESTS_B
    assert "pytest_hook_changed" in rules(plan, "tests/test_b.py::test_env")


def test_a_generate_tests_helper_reaches_the_module_it_parametrizes(repo):
    """The conftest's ``pytest_generate_tests`` runs for every module, and
    calls ``cases`` only for ``test_a``, whose ``case`` parameter it gives:
    a change to which parameters ``test_a`` gets selects ``test_a``'s tests."""
    evidence, plan = _collection_plan(
        repo,
        {
            "pkg/fmt.py": COLLECTION_FMT.replace(
                "return [1, 2]\n\n\ndef cases_for", "return [1, 2, 3]\n\n\ndef cases_for"
            )
        },
    )
    assert evidence.collect_by["pkg.fmt.cases"] == {"tests.test_a"}
    assert evidence.collect_by["tests.conftest.pytest_generate_tests"] == {
        "tests.test_a",
        "tests.test_b",
    }
    assert selected(plan) == TESTS_A
    assert rules(plan, "tests/test_a.py::test_label") == {"collection_code"}


def test_a_generate_tests_helper_handed_the_session_selects_everything(repo):
    """``cases_for`` is handed the ``metafunc`` and reads its ``config``:
    through it, code run while one module is collected can leave state for
    the hooks acting on every test, so it counts as a hook's."""
    conftest = COLLECTION_CONFTEST.replace("cases, order_key", "cases_for, order_key").replace(
        "cases())", "cases_for(metafunc))"
    )
    evidence, plan = _collection_plan(
        repo,
        {"pkg/fmt.py": COLLECTION_FMT.replace("else [1]", "else [2]")},
        conftest=conftest,
    )
    assert evidence.collect_by["pkg.fmt.cases_for"] == {"tests.test_a"}
    assert selected(plan) == TESTS_A | TESTS_B
    assert rules(plan, "tests/test_b.py::test_plain") == {"pytest_hook_changed"}


PLUGIN_OBJECT = """\
from tests import envsetup


class _Plugin:
    def {hook}(self, {arg}):
        envsetup.apply()


def pytest_configure(config):
    config.pluginmanager.register(_Plugin())
"""

HOOK_FILES = {
    **NOOP_BASE,
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
def test_w11_hook_code_in_evidence_mode(repo, conftests):
    """The same under evidence: the helper ran in a hook at the recording,
    and the change escalates to static planning, which reaches every test
    through the hook."""
    base = repo.commit({**HOOK_FILES, **conftests})
    evidence = repo.collect()
    head = repo.commit({"pkg/modes.py": MODES_B})
    plan = _noop_plan(repo, base, head, evidence=evidence)
    assert ENV in selected(plan)
    assert SUB in selected(plan)


FLAG_PLUGIN = """\
import pytest

from pkg.flags import FLAGS


@pytest.fixture(autouse=True)
def _turn_on():
    FLAGS["on"] = True
    yield
    FLAGS.clear()
"""

FLAG_FILES = {
    **PKG,
    "pkg/flags.py": "FLAGS = {}\n\n\ndef flag():\n    return FLAGS.get('on', False)\n",
    "tests/plugin.py": FLAG_PLUGIN,
    "tests/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": "from pkg.flags import flag\n\n\ndef test_b():\n    assert not flag()\n",
}
REGISTERS = 'pytest_plugins = ["tests.plugin"]\n\n\ndef test_a():\n    pass\n'


@pytest.mark.parametrize("how", ["added", "edited", "deleted", "in a module without tests"])
def test_evp5_pytest_plugins_in_a_test_module_reaches_the_session(repo, how):
    """``pytest_plugins`` in a test module registers its plugins for the
    whole session: a change to it reaches every test, not only the module's."""
    files = dict(FLAG_FILES)
    path, after = "tests/test_a.py", REGISTERS
    if how == "edited":
        files[path] = REGISTERS.replace('["tests.plugin"]', "[]")
    elif how == "deleted":
        files[path], after = REGISTERS, FLAG_FILES[path]
    elif how == "in a module without tests":
        path = "tests/test_plugins.py"
        files[path], after = "pytest_plugins = []\n", 'pytest_plugins = ["tests.plugin"]\n'
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({path: after})
    bench = asv_target("benchmarks.TimeFlag.time_flag", "pkg.flags.flag")
    plan = _evidence_plan(repo, base, head, ev, [bench])
    assert _pytest(plan) == {A_ID, "tests/test_b.py::test_b"}
    rules = {r.rule for d in plan.decisions if d.selected for r in d.reasons}
    assert "pytest_hook_changed" in rules


def _pytest(plan) -> set[str]:
    return {
        d.target.runner_id for d in plan.decisions if d.selected and d.target.runner == "pytest"
    }


# --------------------------------------------------------------------------- dynamic lookups


FLUSHED = """\
def flushed(x):
    flush = getattr(x, "flush", None)
    return flush is None or flush() is None
"""

BARE_CLASS = """\
class _Base:
    pass


class _Bare(_Base):
    closed = False


def test_bare():
    assert not _Bare.closed
"""

OPENS = """\
from types import SimpleNamespace

from pkg.stream import flushed


def test_open():
    assert flushed(SimpleNamespace())
"""

HOLDS = {
    "an ancestor's __subclasses__": (
        "from tests.test_bare import _Base\n",
        "_Base.__subclasses__()",
    ),
    "type.__subclasses__": (
        "from tests.test_bare import _Base\n",
        "type.__subclasses__(_Base)",
    ),
    "object.__subclasses__, recursively": (
        "",
        "_walk(object)",
    ),
    "the collector": (
        "import gc\n",
        "[o for o in gc.get_objects() if isinstance(o, type) and o.__name__ == '_Bare']",
    ),
}
WALK = (
    "def _walk(c):\n    out = []\n    for s in type.__subclasses__(c):\n"
    "        if s.__module__.startswith('tests.'):\n            out.append(s)\n"
    "        out += _walk(s)\n    return out\n\n\n"
)
HELD_ID = "tests/test_held.py::test_held"


@pytest.mark.parametrize("how", list(HOLDS))
def test_evp1_a_fake_found_without_naming_it(repo, how):
    """Item 13 guards a name match on a test fake's member by the code that
    can hand the fake on. Code that finds the class without naming it (a
    base's ``__subclasses__()``, the collector) is such code."""
    imports, finder = HOLDS[how]
    held = (
        f"from pkg.stream import flushed\n{imports}\n\n"
        + (WALK if "_walk" in finder else "")
        + f"def test_held():\n    assert all(flushed(c()) for c in {finder})\n"
    )
    files = {
        **PKG,
        "pkg/stream.py": FLUSHED,
        "tests/test_bare.py": BARE_CLASS,
        "tests/test_held.py": held,
        "tests/test_open.py": OPENS,
    }
    base = repo.commit(files)
    ev = repo.collect()
    flush = "    closed = False\n\n    def flush(self):\n        return 1\n"
    head = repo.commit({"tests/test_bare.py": BARE_CLASS.replace("    closed = False\n", flush)})
    bench = asv_target("benchmarks.TimeStream.time_flushed", "pkg.stream.flushed")
    plan = _evidence_plan(repo, base, head, ev, [bench])
    # test_open never holds a fake: the narrowing stands.
    # test_bare reads ``closed`` only: a method added to its class changes
    # nothing it does (its own file is not in its record, E8).
    assert _pytest(plan) == {HELD_ID}


NAME_LOADER = """\
import importlib


def load(modname, attr):
    return getattr(importlib.import_module(modname), attr, None)


def build(modname, name):
    return getattr(importlib.import_module(modname), name)()


def call(obj, name):
    return getattr(obj, name, None)
"""

LOADS_BY_NAME = """\
import os

from pkg.loader import load

MOD = "tests." + "test_a"


def test_b():
    assert load(MOD, os.environ.get("DIFFCONE_NAME", "HELPER")) is None
"""


def test_evp3_library_code_imports_a_test_module_by_name(repo):
    """``load`` imports a module a test names at run time and looks a name
    up on it: a name added to that test module reaches the test."""
    files = {
        **PKG,
        "pkg/loader.py": NAME_LOADER,
        "tests/test_a.py": "VALUE = 1\n\n\ndef test_a():\n    assert VALUE\n",
        "tests/test_b.py": LOADS_BY_NAME,
        "tests/test_c.py": "def test_c():\n    pass\n",
    }
    base = repo.commit(files)
    ev = repo.collect()
    helper = files["tests/test_a.py"] + "\n\ndef HELPER():\n    pass\n"
    head = repo.commit({"tests/test_a.py": helper})
    bench = asv_target("benchmarks.TimeLoad.time_load", "pkg.loader.load")
    plan = _evidence_plan(repo, base, head, ev, [bench])
    # test_a uses no ``HELPER`` (its own file is not in its record, E8).
    assert _pytest(plan) == {"tests/test_b.py::test_b"}


def test_evp3_library_code_builds_a_fake_from_a_test_module(repo):
    """The item-13 side: library code builds the fake by importing the test
    module that holds it, so the test that asked for it holds the fake."""
    fakes = "class _Fake:\n    closed = False\n\n\ndef test_fakes():\n    assert _Fake\n"
    files = {
        **PKG,
        "pkg/loader.py": NAME_LOADER,
        "pkg/stream.py": FLUSHED,
        "tests/test_fakes.py": fakes,
        "tests/test_build.py": (
            "from pkg.loader import build\nfrom pkg.stream import flushed\n\n\n"
            "def test_build():\n"
            '    assert flushed(build("tests." + "test_fakes", "_" + "Fake"))\n'
        ),
        "tests/test_open.py": OPENS,
    }
    base = repo.commit(files)
    ev = repo.collect()
    flush = "    closed = False\n\n    def flush(self):\n        return 1\n"
    head = repo.commit({"tests/test_fakes.py": fakes.replace("    closed = False\n", flush)})
    plan = _evidence_plan(repo, base, head, ev)
    # test_fakes checks only that ``_Fake`` exists (E8).
    assert selected(plan) == {"tests/test_build.py::test_build"}


def test_evp3_a_test_class_instance_handed_to_a_library_lookup(repo):
    """A member added to a test-code class is seen by a library lookup by a
    name nothing bounds, on an instance a test handed it."""
    helpers = "class Helper:\n    x = 0\n\n\ndef test_helpers():\n    assert Helper.x == 0\n"
    files = {
        **PKG,
        "pkg/loader.py": NAME_LOADER,
        "tests/test_helpers.py": helpers,
        "tests/test_h.py": (
            "import os\n\nfrom pkg.loader import call\nfrom tests.test_helpers import Helper\n\n\n"
            "def test_h():\n"
            '    assert call(Helper(), os.environ.get("DIFFCONE_NAME", "go")) is None\n'
        ),
        "tests/test_other.py": (
            "from pkg.loader import call\n\n\ndef test_other():\n"
            '    assert call(object(), "x") is None\n'
        ),
    }
    base = repo.commit(files)
    ev = repo.collect()
    go = "    x = 0\n\n    def go(self):\n        return 1\n"
    head = repo.commit({"tests/test_helpers.py": helpers.replace("    x = 0\n", go)})
    plan = _evidence_plan(repo, base, head, ev)
    # test_other runs the same lookup, but never holds a Helper.
    # test_helpers reads ``Helper.x`` only (E8).
    assert selected(plan) == {"tests/test_h.py::test_h"}


DATA_CHANGED = DATA_TEST.replace("[1, 2]", "[1, 2, 3]")

# Library code that looks names up on an object from elsewhere, and a test
# that runs it.
LOOK = "def look(obj, names):\n    return [getattr(obj, name, None) for name in names]\n"
TEST_LOOK = (
    "from pkg.look import look\n\n\nclass Thing:\n    x = 1\n\n\n"
    "def test_look():\n    assert look(Thing, ['x']) == [1]\n"
)
LOOKER = "tests/test_look.py::test_look"


def _data_plan(repo, files):
    base = repo.commit({**PKG, "pkg/look.py": LOOK, "tests/test_look.py": TEST_LOOK, **files})
    ev = repo.collect()
    head = repo.commit({"tests/test_data.py": DATA_CHANGED})
    return _evidence_plan(repo, base, head, ev)


LAZY = """\
import importlib

_LAZY = {"Thing": ("pkg.things", "Thing")}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(name)
    return getattr(importlib.import_module(target[0]), target[1])
"""

# A module named by data, handed on: any module, so any table may change.
LOADER = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"


@pytest.mark.parametrize("loaded", ["in a test", "at import"])
def test_w1_a_lazy_table_names_a_test_module_only_after_its_writers_ran(repo, loaded):
    # strata.__getattr__ runs at import (a test module imports pkg.Thing),
    # and its table may change only through pkg.loader.load's handle. When
    # load runs only inside tests, the getter cannot have obtained a test
    # module outside every test.
    files = {
        "pkg/__init__.py": LAZY,
        "pkg/things.py": "class Thing:\n    pass\n",
        "pkg/loader.py": LOADER,
        "tests/test_data.py": DATA_TEST,
        "tests/test_lazy.py": (
            "from pkg import Thing\nfrom pkg.loader import load\n"
            + ("\nload('json')\n" if loaded == "at import" else "")
            + "\n\ndef test_lazy():\n    assert Thing and load('json')\n"
        ),
    }
    plan = _data_plan(repo, files)
    if loaded == "at import":
        assert LOOKER in selected(plan)
        assert "lookup_site" in rules(plan, LOOKER)
    else:
        assert LOOKER not in selected(plan)


@pytest.mark.parametrize("call, walks", [("get_stats()", False), ("get_objects()", True)])
def test_w1_a_gc_call_walks_the_graph_only_when_it_hands_objects_out(repo, call, walks):
    # strata's GCTracker.install (gc.callbacks) and metrics (gc.get_stats).
    files = {
        "pkg/stats.py": f"import gc\n\n\ndef stats():\n    return gc.{call}\n",
        "tests/test_data.py": DATA_TEST,
        "tests/test_stats.py": (
            "from pkg.stats import stats\n\n\ndef test_stats():\n    assert stats() is not None\n"
        ),
    }
    plan = _data_plan(repo, files)
    assert ("tests/test_stats.py::test_stats" in selected(plan)) is walks


@pytest.mark.parametrize("kept", [False, True])
def test_w1_an_import_whose_module_is_not_kept_hands_it_to_nothing(repo, kept):
    # strata's pool_worker.warm_imports.
    body = "return __import__(name)" if kept else "__import__(name)"
    files = {
        "pkg/warm.py": f"def warm(names):\n    for name in names:\n        {body}\n",
        "tests/test_data.py": DATA_TEST,
        "tests/test_warm.py": (
            "from pkg.warm import warm\n\n\ndef test_warm():\n    warm(['json'])\n"
        ),
    }
    plan = _data_plan(repo, files)
    assert ("tests/test_warm.py::test_warm" in selected(plan)) is kept


READER = "def read(obj, names):\n    return getattr(obj, names[0])()\n"
OPS_WITH_OTHER = "def add(a, b):\n    return a + b\n\n\ndef other():\n    return 1\n"


def test_w9_a_module_passed_on_in_evidence_mode(repo):
    base = repo.commit(
        {
            **PKG,
            "pkg/ops.py": OPS_WITH_OTHER,
            "pkg/reader.py": READER,
            "tests/test_r.py": (
                "from pkg import ops\nfrom pkg.reader import read\n\n\n"
                "def test_r():\n    assert read(ops, ['other']) == 1\n"
            ),
        }
    )
    ev = repo.collect()
    head = repo.commit({"pkg/ops.py": OPS_WITH_OTHER + "\n\ndef added():\n    return 3\n"})
    plan = _evidence_plan(repo, base, head, ev)
    # An added member is seen by the lookup on an object from elsewhere.
    assert "tests/test_r.py::test_r" in selected(plan)


@pytest.mark.parametrize("added", ["helper", "workerinput"])
def test_w13_hasattr_with_a_literal_notices_only_that_name(repo, added):
    # strata's conftest: ``hasattr(session.config, "workerinput")`` was a
    # reflection site that saw every name added anywhere.
    thing = "class Thing:\n    def run(self):\n        return 1\n"
    base = repo.commit(
        {
            **PKG,
            "pkg/things.py": thing,
            "pkg/check.py": 'def check(obj):\n    return hasattr(obj, "workerinput")\n',
            "tests/test_check.py": (
                "from pkg.check import check\n\n\n"
                "def test_check():\n    assert not check(object())\n"
            ),
        }
    )
    ev = repo.collect()
    head = repo.commit({"pkg/things.py": thing + f"\n    def {added}(self):\n        return 2\n"})
    plan = _evidence_plan(repo, base, head, ev)
    assert ("tests/test_check.py::test_check" in selected(plan)) is (added == "workerinput")


@pytest.mark.parametrize(
    "obtain",
    [
        "pickle.loads(data)",
        "importlib.util.module_from_spec(importlib.util.spec_from_file_location('m', data))",
    ],
    ids=["unpickler", "loader"],
)
def test_w23_an_unpickler_or_loader_may_obtain_a_test_module(repo, obtain):
    """``peek`` looks a name nothing bounds up on what an unpickler or a
    loader of a path nothing bounds handed it: that may be a test module
    (or one of its functions), so a change to a test module's name is seen
    there."""
    files = {
        "pkg/__init__.py": "",
        "pkg/lib.py": (
            "import importlib.util\nimport pickle\n\n\ndef peek(data, name):\n"
            f"    obj = {obtain}\n    return getattr(obj, name, None)\n"
        ),
        "tests/__init__.py": "",
        "tests/test_a.py": "VALUE = 1\n\n\ndef test_a():\n    assert VALUE\n",
        "tests/test_b.py": (
            "import pickle\n\nfrom pkg.lib import peek\n\n\ndef test_b(tmp_path):\n"
            "    path = tmp_path / 'm.py'\n    path.write_text('VALUE = 1\\n')\n"
            "    data = pickle.dumps(1) if 'pickle' in peek.__code__.co_names else str(path)\n"
            "    peek(data, 'VALUE')\n"
        ),
        "tests/test_c.py": "def test_c():\n    pass\n",
    }
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"tests/test_a.py": files["tests/test_a.py"].replace("1", "2")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert "tests/test_b.py::test_b" in selected(plan)
    assert "tests/test_c.py::test_c" not in selected(plan)


@pytest.mark.parametrize("handed", [False, True], ids=["unknown-value", "module-handed-on"])
def test_w20_evidence_a_variable_read_off_a_value(repo, handed):
    """``pkg.conf.LIMIT`` changes; ``get`` reads ``v.LIMIT`` off a value
    of unknown type. It reads ``pkg.conf.LIMIT`` only if ``pkg.conf`` can
    be that value: a test handing the module on."""
    use = "from pkg import conf\n\n\ndef make():\n    return conf\n" if handed else ""
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": "LIMIT = 1\n",
        "pkg/lib.py": f"{use}\n\ndef get(v):\n    return v.LIMIT\n",
        "tests/__init__.py": "",
        "tests/test_get.py": (
            "import types\n\nfrom pkg.lib import get\n\n\n"
            "def test_get():\n    assert get(types.SimpleNamespace(LIMIT=1)) == 1\n"
        ),
        "tests/test_conf.py": (
            "from pkg.conf import LIMIT\n\n\ndef test_conf():\n    assert LIMIT\n"
        ),
    }
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"pkg/conf.py": "LIMIT = 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    expected = {"tests/test_conf.py::test_conf"}
    if handed:
        expected.add("tests/test_get.py::test_get")
    assert selected(plan) == expected


@pytest.mark.parametrize("holder", [False, True], ids=["no-holder", "request-module"])
def test_w20_evidence_a_test_module_name_read_off_a_value(repo, holder):
    """A test module's ``LIMIT`` changes; ``get`` reads ``v.LIMIT``. pytest
    may hand the test module out (``request.module``), so the reader is
    guarded: it counts in a test that also ran code obtaining a module."""
    fixture = (
        "import pytest\n\n\n@pytest.fixture\ndef mod(request):\n    return request.module\n"
        if holder
        else ""
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/lib.py": "def get(v):\n    return getattr(v, 'LIMIT', 0)\n",
        "tests/__init__.py": "",
        "tests/conftest.py": fixture,
        "tests/test_m.py": "LIMIT = 1\n\n\ndef test_m():\n    assert LIMIT\n",
        "tests/test_n.py": (
            "import types\n\nfrom pkg.lib import get\n\n\n"
            + (
                "def test_n(mod):\n    assert get(mod) is not None\n"
                if holder
                else "def test_n():\n    assert get(types.SimpleNamespace(LIMIT=1))\n"
            )
        ),
    }
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit({"tests/test_m.py": files["tests/test_m.py"].replace("1", "2")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    expected = {"tests/test_m.py::test_m"}
    if holder:
        expected.add("tests/test_n.py::test_n")
    assert selected(plan) == expected


INSTALLER = (
    "import sys\nimport types\n\n\ndef install(name):\n"
    "    sys.modules[name] = types.SimpleNamespace(X=1)\n"
)
W25_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "X = 1\n",
    "pkg/inst.py": INSTALLER,
    "tests/__init__.py": "",
    "tests/test_core.py": "import pkg.core\n\n\ndef test_core():\n    assert pkg.core.X\n",
    "tests/test_inst.py": (
        "from pkg.inst import install\n\n\ndef test_inst():\n    install('w25_' + 'name')\n"
    ),
}
W25_CHANGE = {"pkg/inst.py": INSTALLER.replace("X=1", "X=2")}


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_w25_evidence_mode_follows_no_install(repo):
    """Evidence mode records what an installed object ran: ``test_core``
    never ran ``install`` and is not selected."""
    base = repo.commit(W25_FILES)
    ev = repo.collect()
    head = repo.commit(W25_CHANGE)
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {"tests/test_inst.py::test_inst"}
    static = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_core.py::test_core" in selected(static)


STAR = "__all__ = [{}]\n\n\ndef one():\n    return 1\n\n\ndef two():\n    return 2\n"


def test_e8_e9_star_import_names_and_module_getattr(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/a.py": STAR.format("'one'"),
            "lib/c.py": (
                "from lib.a import *\n\n\ndef has_two():\n    try:\n        two\n"
                "    except NameError:\n        return False\n    return True\n"
            ),
            "lib/mod.py": "def helper():\n    return 1\n",
            "lib/use.py": (
                "def flag():\n    try:\n        from lib.mod import FEATURE\n"
                "    except ImportError:\n        return False\n    return FEATURE\n"
            ),
            "tests/__init__.py": "",
            "tests/test_b.py": "from lib.c import has_two\n\n\ndef test_two():\n    has_two()\n",
            "tests/test_use.py": "from lib.use import flag\n\n\ndef test_flag():\n    flag()\n",
        },
        {
            "lib/a.py": STAR.format("'one', 'two'"),
            "lib/mod.py": (
                "def helper():\n    return 1\n\n\ndef __getattr__(name):\n"
                "    if name == 'FEATURE':\n        return True\n    raise AttributeError(name)\n"
            ),
        },
    )
    assert {"tests/test_b.py::test_two", "tests/test_use.py::test_flag"} <= chosen


TEST_ID = "tests/test_core.py::test_core"

EV_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "tests/test_other.py": "def test_other():\n    assert True\n",
}


@pytest.mark.parametrize(
    "core, test, after",
    [
        (
            'class C:\n    """a"""\n',
            "from pkg.core import C\n\n\ndef test_core():\n    assert C.__doc__ == 'a'\n",
            'class C:\n    """b"""\n',
        ),
        (
            'def f():\n    """a"""\n',
            "import inspect\n\nfrom pkg.core import f\n\n\n"
            "def test_core():\n    assert inspect.getdoc(f) == 'a'\n",
            'def f():\n    """b"""\n',
        ),
    ],
    ids=["class docstring", "function docstring"],
)
def test_w2_evidence_docstring_reader(repo, core, test, after):
    """A docstring-only change runs no other code: evidence selects the
    tests that ran code reading docstrings of what changed."""
    repo.commit({**EV_BASE, "pkg/core.py": core, "tests/test_core.py": test})
    ev = repo.collect()
    head = repo.commit({"pkg/core.py": after})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {TEST_ID}


def test_w2_evidence_method_through_class_dict(repo):
    """Guard: the recording sees the method run (passes before the fix)."""
    core = "class C:\n    def m(self):\n        return {}\n"
    test = "from pkg.core import C\n\n\ndef test_core():\n    assert C.__dict__['m'](None)\n"
    repo.commit({**EV_BASE, "pkg/core.py": core.format(1), "tests/test_core.py": test})
    ev = repo.collect()
    head = repo.commit({"pkg/core.py": core.format(2)})
    plan = repo.plan(ev.commit, head, [], discover_runners=["pytest"], evidence=ev)
    assert selected(plan) == {TEST_ID}


DOC_MOD = "def f():\n    '''{0}'''\n"


def test_r7_evidence_reaches_a_reader_through_a_parameter(repo):
    base = repo.commit(
        {
            **PKG,
            "pkg/mod.py": DOC_MOD.format("hello"),
            "tests/test_a.py": (
                "import inspect\n"
                "from pkg.mod import f\n"
                "\n"
                "\n"
                "def doc_of(fn):\n"
                "    return inspect.getdoc(fn)\n"
                "\n"
                "\n"
                "def test_a():\n"
                "    assert doc_of(f) == 'hello'\n"
            ),
            "tests/test_other.py": (
                "from pkg.mod import f\n\n\ndef test_other():\n    assert f() is None\n"
            ),
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/mod.py": DOC_MOD.format("goodbye")})
    plan = _evidence_plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {"tests/test_a.py::test_a"}


FRAMES = """\
import sys

CODES = set()


def internal(f):
    CODES.add(f.__code__)
    return f


def caller_known():
    name = sys._getframe(1).f_globals.get("__name__")
    return name in sys.modules
"""

FRAMED_WORK = """\
from pkg.frames import caller_known, internal

KNOWN = caller_known()


@internal
def handle(x):
    return x + 1
"""

T_FRAMED = "from pkg.work import handle\n\n\ndef test_work():\n    assert handle(1) == 2\n"


@pytest.mark.parametrize(
    ("extra", "head_work", "widens"),
    [
        ({}, None, False),
        (
            {
                "tests/test_conf.py": (
                    "import docs.conf\n\n\ndef test_conf():\n    assert docs.conf\n"
                )
            },
            None,
            True,
        ),
        (
            {
                "tests/test_lazy.py": (
                    "def test_lazy():\n    import docs.conf\n\n    assert docs.conf.release\n"
                )
            },
            None,
            True,
        ),
        ({}, "import docs.conf\n" + FRAMED_WORK, True),
    ],
    ids=[
        "never imported",
        "imported by a test module",
        "imported inside a test",
        "imported at the head",
    ],
)
def test_e23_a_module_no_test_imported_is_seen_by_no_lookup(repo, extra, head_work, widens):
    """A frame's globals (boltons' ``make_sentinel``) or a function's
    ``__code__`` (jinja's ``internalcode``) is a lookup on an object from
    anywhere, taken to see every module's names: a constant changed in
    ``docs/conf.py``, which no test imports, escalated the importer of the
    code that read a frame and selected every test. A module whose
    top-level code ran nowhere in the recording held no object then; one a
    test imports, even inside the test, still counts, and so does one an
    import added at the head brings in."""
    files = {
        ".gitignore": "__pycache__/\n.diffcone/\n",
        "pkg/__init__.py": "",
        "tests/__init__.py": "",
        "pkg/frames.py": FRAMES,
        "pkg/work.py": FRAMED_WORK,
        "docs/conf.py": 'release = "1.0"\n',
        "tests/test_work.py": T_FRAMED,
        **extra,
    }
    base = repo.commit(files)
    evidence = repo.collect()
    change = {"docs/conf.py": 'release = "2.0"\n'}
    if head_work is not None:
        change["pkg/work.py"] = head_work
    head = repo.commit(change)
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=evidence)
    assert ("tests/test_work.py::test_work" in selected(plan)) is widens


# --------------------------------------------------------------------------- class definitions


@pytest.mark.parametrize(
    ("module", "before", "after"),
    [
        (
            "from dataclasses import asdict, dataclass\n\n\n@dataclass\nclass Config:\n"
            "    x: int = {v}\n\n\ndef make():\n    return asdict(Config())\n",
            "0",
            "1",
        ),
        (
            "from enum import Enum\n\n\nclass Config(Enum):\n    RED = {v}\n\n\n"
            "def make():\n    return Config(1).name\n",
            "1",
            "2",
        ),
    ],
)
def test_e2_an_attribute_consumed_at_class_creation_reaches_its_users(repo, module, before, after):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/cfg.py": module.format(v=before),
            "tests/__init__.py": "",
            "tests/test_cfg.py": "from lib.cfg import make\n\n\ndef test_make():\n    make()\n",
        },
        {"lib/cfg.py": module.format(v=after)},
    )
    assert "tests/test_cfg.py::test_make" in chosen


def test_f2_f3_ordinary_class_and_annotated_function_edits_stay_precise(repo):
    util = (
        "class Conf(object):\n    LIMIT = {limit}\n\n\nclass Err(ValueError):\n    pass\n\n\n"
        "def helper():\n    return 1\n"
    )
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/util.py": util.format(limit=1),
            "tests/__init__.py": "",
            "tests/test_u.py": (
                "from lib.util import helper\n\n\ndef test_helper():\n    helper()\n"
            ),
        },
        {
            "lib/util.py": util.format(limit=2)
            + "\n\nclass New(object):\n    pass\n\n\ndef added(x: int) -> int:\n    return x\n"
        },
    )
    assert chosen == set()


def test_r6_evidence_escalates_a_class_reorder(repo):
    """Evidence mode: a class body reorder is no attribute's own change; it
    escalates the module, as the class statement would."""
    base = repo.commit(
        {
            **PKG,
            "pkg/mod.py": (
                "SEEN = []\n"
                "\n"
                "\n"
                "class A:\n"
                "    def m(self):\n"
                "        return 1\n"
                "\n"
                "    SEEN.append(m)\n"
                "\n"
                "\n"
                "def count():\n"
                "    return len(SEEN)\n"
            ),
            "tests/test_a.py": (
                "from pkg.mod import count\n\n\ndef test_a():\n    assert count() == 1\n"
            ),
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    evidence = repo.collect()
    head = repo.commit(
        {
            "pkg/mod.py": (
                "SEEN = []\n"
                "\n"
                "\n"
                "class A:\n"
                "    SEEN.append(m)\n"
                "\n"
                "    def m(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "def count():\n"
                "    return len(SEEN)\n"
            )
        }
    )
    plan = _evidence_plan(repo, base, head, evidence=evidence)
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert "escalated" in rules(plan, "tests/test_a.py::test_a")


# --------------------------------------------------------------------------- annotations


# ``set_mode`` is named at both revisions, at the top level and in a
# ``def`` header (``ALIAS``, ``g``): the module's own dependencies do not
# change, only what its ``def`` statements run.
CONFIG = (
    "from pkg.state import set_mode\n\nALIAS = set_mode\n\n\ndef g(x: set_mode):\n"
    "    pass\n\n\ndef h(x: int):\n    return x\n\n\nY: int = 1\n"
)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (CONFIG, CONFIG + '\n\ndef f(x: set_mode("fast")):\n    pass\n', BOTH),
        (CONFIG, CONFIG + '\n\ndef f() -> set_mode("fast"):\n    pass\n', BOTH),
        (CONFIG + '\n\ndef f(x: set_mode("slow")):\n    pass\n', CONFIG, BOTH),
        (
            CONFIG,
            CONFIG + '\n\nclass K:\n    def f(self, x: set_mode("fast")):\n        pass\n',
            BOTH,
        ),
        (CONFIG, CONFIG.replace("h(x: int)", 'h(x: set_mode("fast"))'), BOTH),
        # A variable's annotation is evaluated at import too; the variable's
        # readers and what its statement writes are reached, as for
        # ``X = set_mode("fast")`` (its module is not escalated).
        (CONFIG, CONFIG + '\n\nZ: set_mode("fast") = 1\n', {"tests/test_b.py::test_b"}),
        (
            CONFIG,
            CONFIG.replace("Y: int = 1", 'Y: set_mode("fast") = 1'),
            {"tests/test_b.py::test_b"},
        ),
    ],
    ids=[
        "parameter annotation added",
        "return annotation added",
        "annotated function deleted",
        "method annotation added",
        "annotation changed",
        "variable annotation added",
        "variable annotation changed",
    ],
)
def test_q3_an_annotation_that_calls_code_runs_at_import(repo, before, after, expected):
    """Before Python 3.14 a ``def``'s annotations are evaluated with it:
    ``def f(x: set_mode("fast"))`` writes ``MODE`` when ``pkg.config`` is
    imported, which ``test_b`` reads without running anything of
    ``pkg.config``."""
    base = repo.commit(
        {
            **PKG,
            "pkg/state.py": STATE,
            "pkg/config.py": before,
            "tests/test_a.py": IMPORTS_CONFIG,
            "tests/test_b.py": READS_MODE,
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/config.py": after})
    plan = _evidence_plan(repo, base, head, evidence, [MODE_BENCH])
    assert selected(plan) == expected | {MODE_BENCH.runner_id}


@pytest.mark.parametrize(
    "after",
    [
        CONFIG + "\n\ndef f(x: int) -> str:\n    return str(x)\n",
        CONFIG.replace("h(x: int)", "h(x: str)"),
    ],
    ids=["added", "changed"],
)
def test_q3_an_annotation_that_calls_nothing_reaches_its_readers_only(repo, after):
    """Narrowing (F3, and now a definition change): an annotation that only
    names something runs no code at import, so the module is not escalated;
    the code reading the function's annotations still sees the change."""
    base = repo.commit(
        {
            **PKG,
            "pkg/state.py": STATE,
            "pkg/config.py": CONFIG,
            "tests/test_a.py": IMPORTS_CONFIG,
            "tests/test_b.py": READS_MODE,
            "tests/test_hints.py": (
                "import typing\n\nimport pkg.config\n\n\ndef test_hints():\n"
                "    assert typing.get_type_hints(pkg.config.h) == {'x': int}\n"
            ),
            "tests/test_calls.py": (
                "from pkg.config import h\n\n\ndef test_calls():\n    assert h(1) == 1\n"
            ),
        }
    )
    evidence = repo.collect()
    head = repo.commit({"pkg/config.py": after})
    plan = _evidence_plan(repo, base, head, evidence)
    # ``get_type_hints`` is a lookup by a name nothing bounds: it sees an
    # added name of the module it can see as well.
    expected = {"tests/test_hints.py::test_hints"}
    if "h(x: str)" in after:
        expected.add("tests/test_calls.py::test_calls")
    assert selected(plan) == expected


ADD_OPS = "def add(a, b):\n    return a + b\n"
TYPED = """\
from pathlib import Path

from pkg.ops import add


def test_typed(tmp_path: Path) -> None:
    assert add(1, 2) == 3


def test_plain():
    assert add(2, 2) == 4
"""
OTHER_TEST = "from pkg.ops import add\n\n\ndef test_other():\n    assert add(0, 0) == 0\n"
FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": ADD_OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TYPED,
    "tests/test_other.py": OTHER_TEST,
}


def test_e7_a_body_change_of_an_annotated_test_stays_in_its_file(repo):
    """The other tests of the edited file are selected too: each test's run
    stats its own file (``touched_file``), on every Python version."""
    base = repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"tests/test_ops.py": TYPED.replace("add(1, 2) == 3", "add(2, 1) == 3")})
    chosen = selected(_evidence_plan(repo, base, head, ev))
    assert "tests/test_ops.py::test_typed" in chosen
    assert "tests/test_other.py::test_other" not in chosen


CONFTEST = (
    "from pathlib import Path\n\nimport pytest\n\n\n@pytest.fixture\n"
    "def data(tmp_path: Path) -> dict:\n    return {'n': 1}\n"
)
USES_FIXTURE = "def test_uses(data):\n    assert data['n'] == 1\n"
WITH_FIXTURE = {**FILES, "tests/conftest.py": CONFTEST, "tests/test_uses.py": USES_FIXTURE}


def test_e7_a_body_change_of_an_annotated_fixture_selects_its_tests(repo):
    base = repo.commit(WITH_FIXTURE)
    ev = repo.collect()
    head = repo.commit({"tests/conftest.py": CONFTEST.replace("{'n': 1}", "dict(n=1)")})
    assert selected(_evidence_plan(repo, base, head, ev)) == {"tests/test_uses.py::test_uses"}


EVERY = {
    "tests/test_ops.py::test_typed",
    "tests/test_ops.py::test_plain",
    "tests/test_other.py::test_other",
}


def test_e7_an_annotation_change_is_not_narrowed(repo):
    """What ``__annotate__`` evaluates changed: on 3.14 pytest ran it while
    collecting, so every test is selected (code run in a hook); before 3.14
    a fixture's definition change reaches every test in its scope."""
    base = repo.commit(WITH_FIXTURE)
    ev = repo.collect()
    head = repo.commit({"tests/conftest.py": CONFTEST.replace("-> dict", "-> object")})
    assert selected(_evidence_plan(repo, base, head, ev)) == EVERY | {
        "tests/test_uses.py::test_uses"
    }


def test_e7_code_of_its_own_that_ran_in_a_hook_still_counts(repo):
    """A function a hook runs during collection, annotated as well: its body
    ran outside every test, so a body change still reaches every test."""
    order = "def key(item: object) -> str:\n    return item.nodeid\n"
    conftest = (
        "from pkg.order import key\n\n\n"
        "def pytest_collection_modifyitems(items):\n    items.sort(key=key)\n"
    )
    base = repo.commit({**FILES, "pkg/order.py": order, "tests/conftest.py": conftest})
    ev = repo.collect()
    assert "pkg.order.key" in ev.hook_phase and "pkg.order.key" not in ev.annotations_only
    head = repo.commit({"pkg/order.py": order.replace("item.nodeid", "str(item.nodeid)")})
    assert selected(_evidence_plan(repo, base, head, ev)) == EVERY


# Project code whose annotations something reads: a decorator at import (as
# FastAPI, dataclasses or pydantic do) and a hook while pytest collects.
ROUTES = """\
import inspect

VERSION = 1


def route(f):
    inspect.signature(f)  # checks the handler's parameters, as a framework does
    return f


@route
def handler(x: int) -> int:
    return x + 1
"""
ROUTE_TESTS = {
    "tests/test_handler.py": (
        "from pkg.routes import handler\n\n\ndef test_handler():\n    assert handler(1) == 2\n"
    ),
    "tests/test_version.py": (
        "from pkg.routes import VERSION\n\n\ndef test_version():\n    assert VERSION == 1\n"
    ),
}


def test_e7_a_body_change_of_a_function_whose_signature_an_import_read(repo):
    """``route`` evaluated ``handler``'s annotations while ``pkg.routes`` was
    imported; its body ran only in ``test_handler``. Before: the module was
    escalated and ``test_version`` selected too."""
    base = repo.commit({**FILES, "pkg/routes.py": ROUTES, **ROUTE_TESTS})
    ev = repo.collect()
    head = repo.commit({"pkg/routes.py": ROUTES.replace("x + 1", "1 + x")})
    assert selected(_evidence_plan(repo, base, head, ev)) == {"tests/test_handler.py::test_handler"}


def test_e7_a_decorator_that_runs_the_body_at_import_still_escalates(repo):
    """``route`` calls ``handler`` at import: its body ran there, so the
    module's import-time state may differ for every importer."""
    calls = ROUTES.replace("inspect.signature(f)  #", "f(1)  #")
    base = repo.commit({**FILES, "pkg/routes.py": calls, **ROUTE_TESTS})
    ev = repo.collect()
    head = repo.commit({"pkg/routes.py": calls.replace("x + 1", "x + 2")})
    assert {"tests/test_handler.py::test_handler", "tests/test_version.py::test_version"} <= (
        selected(_evidence_plan(repo, base, head, ev))
    )


API = "def endpoint(x: int) -> int:\n    return x * 2\n"
SIGNATURE_HOOK = (
    "import inspect\n\nfrom pkg.api import endpoint\n\n\n"
    "def pytest_configure(config):\n    config.endpoint_signature = inspect.signature(endpoint)\n"
)


def test_e7_a_body_change_of_a_function_whose_signature_a_hook_read(repo):
    """A hook evaluated ``endpoint``'s annotations; its body ran only in
    ``test_endpoint``. Before: every test was selected (code run in a hook)."""
    base = repo.commit(
        {
            **FILES,
            "pkg/api.py": API,
            "tests/conftest.py": SIGNATURE_HOOK,
            "tests/test_endpoint.py": (
                "from pkg.api import endpoint\n\n\ndef test_endpoint():\n"
                "    assert endpoint(2) == 4\n"
            ),
        }
    )
    ev = repo.collect()
    head = repo.commit({"pkg/api.py": API.replace("x * 2", "2 * x")})
    assert selected(_evidence_plan(repo, base, head, ev)) == {
        "tests/test_endpoint.py::test_endpoint"
    }


# A fixture in project code (a plugin module): pytest reads its signature
# while collecting, and it reads ``FACTOR`` when it runs.
FACTOR_PLUGIN = """\
import pytest

FACTOR = 2


@pytest.fixture
def factor(tmp_path: object) -> int:
    return FACTOR
"""
PLUGIN_FILES = {
    **FILES,
    "pkg/plugin.py": FACTOR_PLUGIN,
    "tests/conftest.py": 'pytest_plugins = ["pkg.plugin"]\n',
    "tests/test_factor.py": "def test_factor(factor):\n    assert factor == 2\n",
}


def test_e7_a_reader_whose_annotations_do_not_name_the_change(repo):
    """``factor`` reads ``FACTOR`` in its body, which ran only for its test;
    what ran during collection was its annotations (``object``, ``int``).
    Before: a reader that ran during collection, so every test."""
    base = repo.commit(PLUGIN_FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/plugin.py": FACTOR_PLUGIN.replace("FACTOR = 2", "FACTOR = 3")})
    assert selected(_evidence_plan(repo, base, head, ev)) == {"tests/test_factor.py::test_factor"}


def test_e7_a_reader_whose_annotations_name_the_change(repo):
    """``Annotated[object, FACTOR]``: evaluating the annotations during
    collection read ``FACTOR``, so collection saw the change: every test."""
    annotated = "from typing import Annotated\n\n" + FACTOR_PLUGIN.replace(
        "tmp_path: object", "tmp_path: Annotated[object, FACTOR]"
    )
    base = repo.commit({**PLUGIN_FILES, "pkg/plugin.py": annotated})
    ev = repo.collect()
    head = repo.commit({"pkg/plugin.py": annotated.replace("FACTOR = 2", "FACTOR = 3")})
    plan = selected(_evidence_plan(repo, base, head, ev))
    if sys.version_info >= (3, 14):
        assert plan == EVERY | {"tests/test_factor.py::test_factor"}
    else:
        assert "tests/test_factor.py::test_factor" in plan


# --------------------------------------------------------------------------- child processes


G_ID = "tests/test_other.py::test_g"

# A script that imports project code, and the module it imports.
GEN = "from pkg.core import value\n\nprint(value())\n"
CORE = {"pkg/core.py": "def value():\n    return 1\n"}


def test_w3_evidence_follows_the_child_itself(repo):
    """Evidence mode records the child process: the test is selected by a
    change to what the script ran, not by any change."""
    files = {
        **CORE,
        "scripts/__init__.py": "",
        "scripts/gen.py": GEN,
        "tests/test_script.py": (
            "import subprocess\nimport sys\n\n\ndef test_script():\n"
            "    subprocess.run([sys.executable, '-m', 'scripts.gen'], check=True)\n"
        ),
    }
    base = repo.commit({**CHILD_BASE, **files})
    evidence = repo.collect()
    for change, want in [
        ({"pkg/core.py": "def value():\n    return 2\n"}, {"tests/test_script.py::test_script"}),
        (
            {"pkg/core.py": CORE["pkg/core.py"], "pkg/other.py": "def g():\n    return 4\n"},
            # A child on Windows records nothing: its test is always selected.
            {G_ID, G_BENCH.runner_id}
            | ({"tests/test_script.py::test_script"} if sys.platform == "win32" else set()),
        ),
    ]:
        head = repo.commit(change)
        plan = repo.plan(base, head, [G_BENCH], discover_runners=["pytest"], evidence=evidence)
        assert selected(plan) == want


@pytest.mark.parametrize(
    "program", ["scripts/gen-data.py", "os.environ.get('S', 'scripts/gen.py')"]
)
def test_w3_evidence_a_script_no_module_name_maps_to(repo, program):
    """The child runs a file the index does not read (or one it cannot
    name): a change to that file reaches the test in evidence mode too."""
    script = program if program.endswith(".py") else "scripts/gen.py"
    arg = repr(program) if program.endswith(".py") else program
    files = {
        **CORE,
        script: GEN,
        "tests/test_script.py": (
            "import os\nimport subprocess\nimport sys\n\n\ndef test_script():\n"
            f"    subprocess.run([sys.executable, {arg}], check=True)\n"
        ),
    }
    base = repo.commit({**CHILD_BASE, **files})
    evidence = repo.collect()
    head = repo.commit({script: GEN + "X = 1\n"})
    plan = repo.plan(base, head, [G_BENCH], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/test_script.py::test_script"}


# --------------------------------------------------------------------------- build inputs


@pytest.mark.parametrize("path", ["setup.py", "lib/kernel.F90", "Cargo.toml"])
def test_e19_build_inputs_select_everything_in_evidence_mode(repo, path):
    chosen = _evidence_selects(
        repo,
        {
            "setup.py": "X = 1\n",
            "lib/kernel.F90": "! kernel\n",
            "Cargo.toml": "[package]\nname = 'k'\n",
            "lib/__init__.py": "",
            "lib/m.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "from lib.m import f\n\n\ndef test_f():\n    f()\n",
        },
        {path: "X = 22\n" if path == "setup.py" else "! changed\n"},
    )
    assert "tests/test_m.py::test_f" in chosen


BUILD_FILES = {
    **PKG,
    "pkg/build.py": "def make():\n    return 1\n",
    "pkg/other.py": "def g():\n    return 2\n",
    "tests/test_a.py": "from pkg.build import make\n\n\ndef test_a():\n    assert make() == 1\n",
    "tests/test_b.py": "from pkg.other import g\n\n\ndef test_b():\n    assert g() == 2\n",
}


def test_q17_an_ordinary_build_module_selects_its_users(repo):
    """``pkg/build.py`` is an ordinary module (no ``pyproject.toml`` beside
    it): editing it selects the tests that ran it, as static planning does,
    not every test."""
    base = repo.commit(BUILD_FILES)
    evidence = repo.collect()
    head = repo.commit({"pkg/build.py": "def make():\n    x = 1\n    return x\n"})
    bench = asv_target("benchmarks.TimeOther.time_g", "pkg.other.g")
    plan = _evidence_plan(repo, base, head, evidence, [bench])
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert not plan.fallbacks


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("build.py", {}),
        ("pkg/build.py", {"pkg/pyproject.toml": "[project]\nname = 'pkg'\n"}),
        ("pkg/setup.py", {}),
    ],
    ids=["build.py at the root", "build.py beside pyproject.toml", "setup.py"],
)
def test_q17_a_build_script_still_selects_everything(repo, path, extra):
    """A real build script decides what is installed: every test, whether it
    ran it or not."""
    files = {**BUILD_FILES, **extra, path: "def make():\n    return 1\n"}
    files["tests/test_a.py"] = (
        f"from {path[:-3].replace('/', '.')} import make\n\n\ndef test_a():\n"
        "    assert make() == 1\n"
    )
    base = repo.commit(files)
    evidence = repo.collect()
    head = repo.commit({path: "def make():\n    x = 1\n    return x\n"})
    plan = _evidence_plan(repo, base, head, evidence)
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}


# --------------------------------------------------------------------------- doctest namespace


def test_an_autouse_doctest_namespace_fixture_does_not_make_every_test_a_lookup(repo):
    """pandas' shape (roadmap item 19): an autouse fixture stores the package,
    which imports ``gc``, into pytest's ``doctest_namespace``. Every test runs
    it; it was an object-graph walker, a lookup site that can see every test
    module, so a name added to one test module selected every test."""
    files = {
        ".gitignore": "__pycache__/\n.diffcone/\n",
        "pkg/__init__.py": "import gc  # noqa: F401\n\nfrom pkg.ops import add  # noqa\n",
        "pkg/ops.py": "def add(a, b):\n    return a + b\n",
        "tests/__init__.py": "",
        "tests/conftest.py": (
            "import pytest\n\nimport pkg as pd\n\n\n@pytest.fixture(autouse=True)\n"
            "def add_doctest_imports(doctest_namespace):\n    doctest_namespace['pd'] = pd\n"
        ),
        "tests/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": (
            "from pkg.ops import add\n\n\ndef test_b():\n    assert add(1, 2) == 3\n"
        ),
    }
    base = repo.commit(files)
    ev = repo.collect()
    head = repo.commit(
        {"tests/test_a.py": files["tests/test_a.py"] + "\n\ndef helper():\n    pass\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"], evidence=ev)
    assert "tests/test_b.py::test_b" not in selected(plan)


def test_e29_an_excluded_file_is_planned_from_who_read_it(repo):
    """``collect`` refuses a checkout holding a ``.py`` file it cannot
    analyse (black's test data); excluded, the file is data, and a change
    to it selects the tests that read it."""
    files = {
        **BASE,
        "tests/data/case.py": "def broken(:\n",
        "tests/test_data.py": (
            "from pathlib import Path\n\n\ndef test_reads():\n"
            "    assert (Path(__file__).parent / 'data' / 'case.py').read_text()\n"
        ),
    }
    repo.commit(files)
    with pytest.raises(EvidenceError):
        repo.collect()
    base, ev = _collected(repo, {"diffcone.toml": '[index]\nexclude = ["tests/data/*"]\n'})
    head = repo.commit({"tests/data/case.py": "def broken(:\n    x\n"})
    plan = _plan(repo, base, head, ev)
    # The benchmarks have no evidence: planned statically, a file the index
    # does not read selects them.
    assert selected(plan) == {"tests/test_data.py::test_reads"} | BENCHES
    assert rules(plan, "tests/test_data.py::test_reads") == {"touched_file"}

"""Evidence-mode scenarios (internal/evidence_design.md).

Each records real evidence in a fixture repository (``diffcone collect``
runs its suite under the recorder), commits a change and plans with that
evidence. pytest targets come from static discovery; ASV targets from a
manifest, which evidence does not cover, so they keep their static
selection. Assertions check exact target sets and the rules behind them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from diffcone.testing import asv_target, reason, rules, selected

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
    # pytest stats a test's own file inside the test: the file's tests, and
    # no benchmark (everything was selected).
    assert selected(plan) == {ADD, MUL}


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

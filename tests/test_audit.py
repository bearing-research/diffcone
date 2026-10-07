"""Regression scenarios for the pre-release audit (internal/audit.md).

Each test names the finding it guards. Every one failed before its fix: a
test or benchmark whose outcome changes was not selected.
"""

from __future__ import annotations

import json
import sys

import pytest

from diffcone.cli import main
from diffcone.manifest import ManifestError, parse_manifest
from diffcone.testing import asv_target, py_target, selected, unselected

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'


# P1: a target's lifecycle dependencies at the base count too.


def test_p1_deleting_an_autouse_fixture_selects_its_tests(repo):
    """The conftest and its autouse fixture are gone at head, so head
    discovery no longer links the test to them."""
    bench = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/conftest.py": (
                "import os\nimport pytest\n\n\n@pytest.fixture(autouse=True)\n"
                "def mode():\n    os.environ['MODE'] = 'x'\n"
            ),
            "tests/test_v.py": (
                "import os\n\n\ndef test_v():\n    assert os.environ['MODE'] == 'x'\n"
            ),
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit({"tests/conftest.py": None})
    plan = repo.plan(base, head, [bench], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_v.py::test_v"}
    assert "bench.time_noop" in unselected(plan)


def test_p1_deleting_a_fixture_override_selects_its_tests(repo):
    """With the inner override deleted, the test resolves to the outer
    fixture: a different value, reached only through the base's edge."""
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 1\n",
            "tests/sub/__init__.py": "",
            "tests/sub/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 2\n"
            ),
            "tests/sub/test_v.py": "def test_v(value):\n    assert value == 2\n",
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    head = repo.commit({"tests/sub/conftest.py": None})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/sub/test_v.py::test_v"}


def test_p1_deleting_an_asv_module_setup_selects_its_benchmarks(repo):
    """A module-level ``setup`` is an inert ``def``: deleting it changes
    nothing at import, only what ASV runs before the benchmark."""
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "",
            "benchmarks/b.py": (
                "import os\n\n\ndef setup():\n    os.environ['N'] = '5'\n\n\n"
                "def time_x():\n    int(os.environ['N'])\n"
            ),
            "benchmarks/c.py": "def time_y():\n    pass\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
        }
    )
    path = "benchmarks/b.py"
    head = repo.commit({path: "import os\n\n\ndef time_x():\n    int(os.environ['N'])\n"})
    plan = repo.plan(base, head, [], discover_runners=["asv", "pytest"])
    assert selected(plan) == {"b.time_x"}
    assert {"c.time_y", "tests/test_a.py::test_a"} <= unselected(plan)


# P2: added edges change the behaviour of the symbol that gained them.


OPS = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"


def test_p2_adding_a_missing_import_selects_the_test_and_benchmark(repo):
    """``add`` was an unresolved name (NameError); the fix adds the import
    and touches nothing else."""
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS,
            "tests/__init__.py": "",
            "tests/test_ops.py": "def test_add():\n    assert add(1, 2) == 3\n",
            "tests/test_mul.py": "from pkg.ops import mul\n\n\ndef test_mul():\n    mul(1, 2)\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench_ops.py": "class T:\n    def time_add(self):\n        add(1, 2)\n",
        }
    )
    head = repo.commit(
        {
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
            ),
            "benchmarks/bench_ops.py": (
                "from pkg.ops import add\n\n\n"
                "class T:\n    def time_add(self):\n        add(1, 2)\n"
            ),
        }
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_ops.py::test_add", "bench_ops.T.time_add"}
    assert "tests/test_mul.py::test_mul" in unselected(plan)


REGISTRY = {
    "asv.conf.json": ASV_CONF,
    "pkg/__init__.py": "",
    "pkg/registry.py": "HANDLERS = {}\n\n\ndef lookup(name):\n    return HANDLERS[name]\n",
    "pkg/json_handler.py": "from pkg.registry import HANDLERS\n\nHANDLERS['json'] = 1\n",
    "pkg/util.py": "def helper():\n    return 1\n",
    "pkg/api.py": "from pkg.registry import lookup\nimport pkg.util\n",
    "tests/__init__.py": "",
    "tests/test_api.py": (
        "import pkg.api\nfrom pkg.registry import lookup\n\n\n"
        "def test_json():\n    assert lookup('json') == 1\n"
    ),
    "tests/test_util.py": "from pkg.util import helper\n\n\ndef test_helper():\n    helper()\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_api.py": "import pkg.api\n\n\ndef time_api():\n    pass\n",
}


def test_p2_an_import_that_runs_new_code_reaches_the_importers(repo):
    """``pkg.api`` now imports a module whose top level registers a handler:
    importing ``pkg.api`` runs code it did not run before."""
    base = repo.commit(REGISTRY)
    api = REGISTRY["pkg/api.py"] + "import pkg.json_handler\n"
    head = repo.commit({"pkg/api.py": api})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_api.py::test_json", "bench_api.time_api"}
    assert "tests/test_util.py::test_helper" in unselected(plan)


def test_p2_an_import_of_code_that_already_ran_selects_nothing(repo):
    """Binding a name from a module ``pkg.api`` already imports runs nothing
    new; no existing code uses the new name."""
    base = repo.commit(REGISTRY)
    api = REGISTRY["pkg/api.py"] + "from pkg.util import helper\n"
    head = repo.commit({"pkg/api.py": api})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == set()


# P3: rebinding a def's name at module level is an import-time change.


def test_p3_rebinding_a_function_name_selects_its_users(repo):
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "pkg/__init__.py": "",
            "pkg/m.py": "def helper():\n    return 1\n\n\ndef f():\n    return helper()\n",
            "pkg/other.py": "def g():\n    return 2\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "from pkg.m import f\n\n\ndef test_f():\n    assert f() == 1\n",
            "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    g()\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench_m.py": "from pkg.m import f\n\n\ndef time_f():\n    f()\n",
        }
    )
    m = "def helper():\n    return 1\n\n\ndef f():\n    return helper()\n"
    for rebinding in ("helper = 3\n", "from pkg.other import g\n\nhelper = g\n"):
        repo.git("reset", "-q", "--hard", base)  # each edit on its own
        head = repo.commit({"pkg/m.py": m + "\n\n" + rebinding})
        plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
        assert selected(plan) == {"tests/test_m.py::test_f", "bench_m.time_f"}, rebinding
        assert "tests/test_other.py::test_g" in unselected(plan)


# P4: a target depends on its entry's module, manifest or not.


def test_p4_a_manifest_target_sees_import_time_changes_of_its_module(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/sub.py": "import os\n\nos.environ['MODE'] = 'a'\n",
            "tests/__init__.py": "",
            "tests/test_a.py": (
                "import os\n\nimport pkg.sub\n\n\n"
                "def test_mode():\n    assert os.environ['MODE'] == 'a'\n"
            ),
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit({"pkg/sub.py": "import os\n\nos.environ['MODE'] = 'b'\n"})
    targets = [
        py_target("tests/test_a.py::test_mode", "tests.test_a.test_mode"),
        asv_target("bench.time_noop", "benchmarks.bench.time_noop"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"tests/test_a.py::test_mode"}
    assert unselected(plan) == {"bench.time_noop"}


# P5: a misspelt manifest key is an error, not a dependency silently dropped.


@pytest.mark.parametrize(
    "data",
    [
        {"targets": [], "source_root": ["src"]},
        {
            "targets": [
                {
                    "runner": "pytest",
                    "runner_id": "t::a",
                    "entry_symbol": "t.a",
                    "lifecycle_dependency": ["tests.conftest.db"],
                }
            ]
        },
    ],
)
def test_p5_unknown_manifest_keys_are_rejected(data):
    with pytest.raises(ManifestError, match="unknown key"):
        parse_manifest(data)


# X1: no failure exits 1, which means "a plan, degraded".


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
            "--command",
            "no-such-pytest-command",
        ]
    )
    assert code == 2


# X2: discover exits with the plan's codes, and its notes survive the manifest.


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


# X3: a source root that matches nothing is an error, not a complete empty plan.


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


# P6: a docstring change counts where code runs or reads it, and only there.


DOCS = {
    "asv.conf.json": ASV_CONF,
    "pkg/__init__.py": "",
    "pkg/deco.py": (
        "def doc(**kw):\n    def wrap(f):\n        f.__doc__ = f.__doc__.format(**kw)\n"
        "        return f\n    return wrap\n"
    ),
    "pkg/a.py": (
        "from pkg.deco import doc\n\n\n@doc(klass='Frame')\ndef reduce():\n"
        '    """Reduce a {klass}."""\n    return 1\n\n\n'
        'def greet():\n    """Hello."""\n    return 2\n\n\n'
        'def plain():\n    """Plain."""\n    return 3\n'
    ),
    "tests/__init__.py": "",
    "tests/test_reduce.py": "from pkg.a import reduce\n\n\ndef test_reduce():\n    reduce()\n",
    "tests/test_doc.py": (
        "from pkg.a import greet\n\n\ndef test_doc():\n    assert greet.__doc__ == 'Hello.'\n"
    ),
    "tests/test_plain.py": "from pkg.a import plain\n\n\ndef test_plain():\n    plain()\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}


def _doc_plan(repo, old, new):
    base = repo.commit(DOCS)
    head = repo.commit({"pkg/a.py": DOCS["pkg/a.py"].replace(old, new)})
    return repo.plan(base, head, [], discover_runners=["pytest", "asv"])


def test_p6_a_docstring_formatted_by_a_decorator_runs_at_import(repo):
    """``{axis}`` makes the decorator raise when ``pkg.a`` is imported: every
    importer fails."""
    plan = _doc_plan(repo, "Reduce a {klass}.", "Reduce a {klass} along {axis}.")
    assert {
        "tests/test_reduce.py::test_reduce",
        "tests/test_doc.py::test_doc",
        "tests/test_plain.py::test_plain",
    } <= selected(plan)
    assert "bench.time_noop" in unselected(plan)


def test_p6_a_docstring_read_through_doc_selects_the_reader(repo):
    plan = _doc_plan(repo, '"""Hello."""', '"""Hi."""')
    assert selected(plan) == {"tests/test_doc.py::test_doc"}


def test_p6_a_plain_docstring_edit_still_selects_nothing(repo):
    plan = _doc_plan(repo, '"""Plain."""', '"""Plain, documented."""')
    assert selected(plan) == set()


# I1: every module-level binding of a name is a dependency of its users.


@pytest.mark.parametrize(
    "binding",
    [
        "import sys\n\nif sys.version_info >= (3, 8):\n    from pkg.new import impl\n"
        "else:\n    from pkg.old import impl\n",
        "def impl():\n    return 0\n\n\ntry:\n    from pkg.new import impl\n"
        "except ImportError:\n    pass\n",
        "try:\n    from pkg.new import impl\nexcept ImportError:\n"
        "    def impl():\n        return 0\n",
        "try:\n    from pkg.old import impl\nexcept ImportError:\n    from pkg.new import impl\n",
    ],
)
def test_i1_every_binding_of_a_name_reaches_its_users(repo, binding):
    files = {
        "asv.conf.json": ASV_CONF,
        "pkg/__init__.py": "",
        "pkg/new.py": "def impl():\n    return 1\n",
        "pkg/old.py": "def impl():\n    return 2\n",
        "pkg/a.py": binding + "\n\ndef use():\n    return impl()\n",
        "tests/__init__.py": "",
        "tests/test_a.py": "from pkg.a import use\n\n\ndef test_use():\n    use()\n",
        "tests/test_other.py": "def test_other():\n    pass\n",
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": "from pkg.a import use\n\n\ndef time_use():\n    use()\n",
    }
    base = repo.commit(files)
    head = repo.commit({"pkg/new.py": "def impl():\n    return 10\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_a.py::test_use", "bench.time_use"}
    assert "tests/test_other.py::test_other" in unselected(plan)


# I2: a name rebound by any form is no longer the literal it was bound to.


@pytest.mark.parametrize(
    "use",
    [
        "def use():\n    name = 'fast'\n    name += '_path'\n    return getattr(impls, name)()\n",
        "def use():\n    name = 'fast'\n    if (name := name + '_path'):\n"
        "        return getattr(impls, name)()\n",
        "def use():\n    name = 'fast'\n    name, _ = name + '_path', 1\n"
        "    return getattr(impls, name)()\n",
        "def use():\n    name = 'fast'\n\n    def grow():\n        nonlocal name\n"
        "        name += '_path'\n\n    grow()\n    return getattr(impls, name)()\n",
        "NAME = 'fast'\nNAME += '_path'\n\n\ndef use():\n    return getattr(impls, NAME)()\n",
        "NAME = 'fast'\n\n\ndef grow():\n    global NAME\n    NAME = NAME + '_path'\n\n\n"
        "grow()\n\n\ndef use():\n    return getattr(impls, NAME)()\n",
    ],
)
def test_i2_a_rebound_name_is_not_bounded_by_its_first_literal(repo, use):
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "pkg/__init__.py": "",
            "pkg/impls.py": "def fast():\n    return 1\n\n\ndef fast_path():\n    return 2\n",
            "pkg/a.py": "from pkg import impls\n\n\n" + use,
            "pkg/b.py": "def other():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "from pkg.a import use\n\n\ndef test_use():\n    use()\n",
            "tests/test_b.py": "from pkg.b import other\n\n\ndef test_other():\n    other()\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    impls = "def fast():\n    return 1\n\n\ndef fast_path():\n    return 20\n"
    head = repo.commit({"pkg/impls.py": impls})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_a.py::test_use" in selected(plan)
    assert {"tests/test_b.py::test_other", "bench.time_noop"} <= unselected(plan)


# I3: moving or reordering imports changes what runs at import.

PLUGIN = {
    "asv.conf.json": ASV_CONF,
    "pkg/__init__.py": "",
    "pkg/registry.py": "HANDLERS = {}\n",
    "pkg/plugin.py": "from pkg.registry import HANDLERS\n\nHANDLERS['p'] = 1\n",
    "pkg/other.py": "def g():\n    return 2\n",
    "pkg/extra.py": "E = 1\n",
    "tests/__init__.py": "",
    "tests/test_a.py": (
        "import pkg.a\nfrom pkg.registry import HANDLERS\n\n\n"
        "def test_use():\n    assert HANDLERS['p'] == 1\n"
    ),
    "tests/test_other.py": "from pkg.other import g\n\n\ndef test_other():\n    g()\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Into an existing ``if TYPE_CHECKING:`` block: never runs any more.
        (
            "from typing import TYPE_CHECKING\n\nimport pkg.plugin\n\n"
            "if TYPE_CHECKING:\n    import pkg.extra\n",
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n    import pkg.extra\n    import pkg.plugin\n",
        ),
        # Into an ``except`` branch: runs only when the ``try`` fails.
        (
            "import pkg.plugin\n\ntry:\n    import pkg.other\n"
            "except ImportError:\n    import pkg.extra\n",
            "try:\n    import pkg.other\n"
            "except ImportError:\n    import pkg.extra\n    import pkg.plugin\n",
        ),
    ],
)
def test_i3_moving_an_import_is_a_change(repo, before, after):
    base = repo.commit({**PLUGIN, "pkg/a.py": before})
    head = repo.commit({"pkg/a.py": after})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_a.py::test_use" in selected(plan)
    assert "bench.time_noop" in unselected(plan)


def test_i3_reordering_imports_is_a_definition_change(repo):
    base = repo.commit({**PLUGIN, "pkg/a.py": "import pkg.plugin\nimport pkg.other\n"})
    head = repo.commit({"pkg/a.py": "import pkg.other\nimport pkg.plugin\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert [(c.id, c.changes) for c in plan.changes] == [("pkg.a", ("definition_changed",))]


def test_i3_adding_an_import_elsewhere_is_still_only_an_addition(repo):
    base = repo.commit({**PLUGIN, "pkg/a.py": "import pkg.plugin\n"})
    head = repo.commit({"pkg/a.py": "import pkg.plugin\nimport pkg.registry\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert [(c.id, c.changes) for c in plan.changes] == [("pkg.a", ("imports_added",))]
    assert selected(plan) == set()


# I4: an import in a class body runs at import too.


def test_i4_an_import_in_a_class_body_reaches_the_importers(repo):
    base = repo.commit(
        {
            **PLUGIN,
            "pkg/plugin.py": "X = 1\n",
            "pkg/a.py": "class A:\n    import pkg.plugin\n",
            "tests/test_a.py": "import pkg.a\n\n\ndef test_use():\n    pass\n",
        }
    )
    head = repo.commit({"pkg/plugin.py": "raise RuntimeError('broken')\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_a.py::test_use" in selected(plan)
    assert {"tests/test_other.py::test_other", "bench.time_noop"} <= unselected(plan)


# I5: a module __getattr__ serves the names the module does not bind.


def test_i5_a_name_served_by_module_getattr_reaches_its_users(repo):
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "pkg/__init__.py": (
                "import importlib\n\n\ndef __getattr__(name):\n"
                "    if name == 'thing':\n"
                "        return importlib.import_module('pkg._impl').real\n"
                "    raise AttributeError(name)\n"
            ),
            "pkg/_impl.py": "def real():\n    return 1\n",
            "pkg/other.py": "def g():\n    return 2\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "from pkg import thing\n\n\ndef test_use():\n    thing()\n",
            "tests/test_other.py": "from pkg.other import g\n\n\ndef test_other():\n    g()\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit({"pkg/_impl.py": "def real():\n    return 10\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_a.py::test_use" in selected(plan)
    assert {"tests/test_other.py::test_other", "bench.time_noop"} <= unselected(plan)


# I6: a relative import above the top-level package is unknown, not external.


def test_i6_a_relative_import_above_the_package_is_unbounded(repo):
    """With the root ``tests`` while ``tests/__init__.py`` exists, the test
    module is ``test_x`` here but ``tests.test_x`` at runtime: what
    ``from . import helpers`` imports is not known."""
    base = repo.commit(
        {
            "tests/__init__.py": "",
            "tests/helpers.py": "def make():\n    return 1\n",
            "tests/test_x.py": (
                "from . import helpers\n\n\ndef test_use():\n    assert helpers.make() == 1\n"
            ),
            "tests/bench_x.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit({"tests/helpers.py": "def make():\n    return 2\n"})
    targets = [
        py_target("tests/test_x.py::test_use", "test_x.test_use", "test_x"),
        asv_target("bench_x.time_noop", "bench_x.time_noop"),
    ]
    plan = repo.plan(base, head, targets, source_roots=["tests"])
    assert selected(plan) == {"tests/test_x.py::test_use"}


# I7: a module variable's annotation runs at import.


def test_i7_a_module_variable_annotation_change_runs_at_import(repo):
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "pkg/__init__.py": "",
            "pkg/check.py": (
                "def check(n):\n    if n > 1:\n        raise ValueError(n)\n    return int\n"
            ),
            "pkg/a.py": "from pkg.check import check\n\nX: check(1) = 3\n",
            "pkg/b.py": "def g():\n    return 2\n",
            "tests/__init__.py": "",
            "tests/test_a.py": "import pkg.a\n\n\ndef test_a():\n    pass\n",
            "tests/test_b.py": "from pkg.b import g\n\n\ndef test_b():\n    g()\n",
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit({"pkg/a.py": "from pkg.check import check\n\nX: check(2) = 3\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_a.py::test_a" in selected(plan)
    assert {"tests/test_b.py::test_b", "bench.time_noop"} <= unselected(plan)


CONSTANT = (
    "TIMEOUT: int = {}\n\n\ndef read():\n    return TIMEOUT\n\n\ndef other():\n    return 1\n"
)


def test_i7_a_value_change_of_an_annotated_constant_reaches_only_its_readers(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": CONSTANT.format(30),
            "tests/__init__.py": "",
            "tests/test_read.py": "from pkg.a import read\n\n\ndef test_read():\n    read()\n",
            "tests/test_other.py": "from pkg.a import other\n\n\ndef test_other():\n    other()\n",
        }
    )
    head = repo.commit({"pkg/a.py": CONSTANT.format(31)})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_read.py::test_read"}


# I8: a cached index reports errors under the revision as spelt this time.


def test_i8_cached_errors_name_the_revision_as_given(repo, tmp_path):
    from diffcone.cache import IndexCache
    from diffcone.planner import plan
    from diffcone.report import to_dict

    repo.commit({"pkg/__init__.py": "", "pkg/bad.py": "def f(:\n"})
    repo.git("tag", "v1")
    sha = repo.git("rev-parse", "HEAD").strip()
    head = repo.commit({"pkg/ok.py": "X = 1\n"})
    cache = IndexCache(tmp_path / "cache")
    plan(repo.path, "v1", head, None, cache=cache)  # stores the index under v1's spelling
    cached = to_dict(plan(repo.path, sha, head, None, cache=cache))
    fresh = to_dict(plan(repo.path, sha, head, None))
    assert cached["analysis_errors"] == fresh["analysis_errors"]


# D1: a test file outside the source roots is reported, not silently missed.


def test_d1_test_files_outside_the_source_roots_make_discovery_incomplete(repo, tmp_path):
    base = repo.commit(
        {
            "src/calc/__init__.py": "",
            "src/calc/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_ops.py": "from calc.ops import add\n\n\ndef test_add():\n    add(1, 2)\n",
        }
    )
    head = repo.commit({"src/calc/ops.py": "def add(a, b):\n    return b + a\n"})
    code = main(
        [
            "plan",
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--discover",
            "pytest",
            "--source-root",
            "src",
            "-o",
            str(tmp_path / "plan.json"),
        ]
    )
    assert code == 3
    report = json.loads((tmp_path / "plan.json").read_text())
    assert report["discovery_incomplete"]
    notes = report["discovery"][0]["notes"]
    assert any(n["kind"] == "test_file_outside_roots" for n in notes)


# D2, D8: pytest 9's config files, in pytest's order; INI values split like a shell.


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pytest.toml", '[pytest]\npython_files = ["check_*.py"]\n'),
        (".pytest.toml", '[pytest]\npython_files = ["check_*.py"]\n'),
        (".pytest.ini", "[pytest]\npython_files = check_*.py\n"),
    ],
)
def test_d2_pytest_9_config_files_are_read_first(repo, name, text):
    rev = repo.commit(
        {
            name: text,
            # Lower precedence: must be ignored.
            "pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["test_*.py"]\n',
            "tests/check_a.py": "def test_a():\n    pass\n",
            "tests/test_b.py": "def test_b():\n    pass\n",
        }
    )
    plan = repo.plan(rev, rev, [], discover_runners=["pytest"])
    assert {t.runner_id for t in plan.targets} == {"tests/check_a.py::test_a"}


def test_d8_quoted_addopts_lose_their_quotes(repo):
    rev = repo.commit(
        {
            "pytest.ini": '[pytest]\naddopts = --doctest-glob="*.rst"\n',
            "docs/guide.rst": ">>> 1 + 1\n2\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
        }
    )
    plan = repo.plan(rev, rev, [], discover_runners=["pytest"])
    assert "docs/guide.rst::guide.rst" in {t.runner_id for t in plan.targets}


def _discovered(repo, files, runner="pytest"):
    rev = repo.commit(files)
    plan = repo.plan(rev, rev, [], discover_runners=[runner])
    notes = {n.kind for d in plan.discovery for n in d.notes}
    return {t.runner_id for t in plan.targets}, notes


# D3, D4, D5, D11: what pytest collects beyond ``def test_*`` and ``class Test*``.


def test_d3_tests_bound_by_assignment_are_targets(repo):
    targets, notes = _discovered(
        repo,
        {
            "tests/test_a.py": (
                "import unittest\n\n\ndef test_orig():\n    pass\n\n\n"
                "def _check():\n    pass\n\n\n"
                "test_alias = test_orig\ntest_from_helper = _check\n\n\n"
                "class TestK:\n    def test_m(self):\n        pass\n\n    test_attr = test_m\n\n\n"
                "class Machine:\n    TestCase = unittest.TestCase\n\n\n"
                "TestMachine = Machine.TestCase\n"
                "test_data = [1, 2]\n"
            ),
        },
    )
    assert {
        "tests/test_a.py::test_alias",
        "tests/test_a.py::test_from_helper",
        "tests/test_a.py::TestK::test_attr",
    } <= targets
    assert "unmodelled_test_binding" in notes  # TestMachine


def test_d4_d5_unittest_runtest_and_imported_testcases_under_any_name(repo):
    targets, _ = _discovered(
        repo,
        {
            "tests/__init__.py": "",
            "tests/base.py": (
                "import unittest\n\n\nclass SharedChecks(unittest.TestCase):\n"
                "    def test_shared(self):\n        pass\n"
            ),
            "tests/test_a.py": (
                "import unittest\n\nfrom tests.base import SharedChecks as ImportedChecks\n\n\n"
                "class RunOnly(unittest.TestCase):\n    def runTest(self):\n        pass\n"
            ),
        },
    )
    assert {
        "tests/test_a.py::RunOnly::runTest",
        "tests/test_a.py::ImportedChecks::test_shared",
    } <= targets


def test_d11_a_function_marked_test_is_collected(repo):
    targets, _ = _discovered(
        repo, {"tests/test_a.py": "def verify():\n    pass\n\n\nverify.__test__ = True\n"}
    )
    assert "tests/test_a.py::verify" in targets


# D6: only a test framework's TestCase is excused as a base discovery cannot see.


def test_d6_an_unresolvable_testcase_base_is_reported(repo):
    _, notes = _discovered(
        repo,
        {
            "tests/test_a.py": (
                "import unittest\n\n\ndef make_base():\n    return unittest.TestCase\n\n\n"
                "SharedTestCase = make_base()\n\n\n"
                "class TestThing(SharedTestCase):\n    def test_x(self):\n        pass\n"
            ),
        },
    )
    assert "unknown_base_class" in notes


def test_d6_a_framework_testcase_base_is_not(repo):
    _, notes = _discovered(
        repo,
        {
            "tests/test_a.py": (
                "from django.test import TestCase\n\n\n"
                "class TestThing(TestCase):\n    def test_x(self):\n        pass\n"
            ),
        },
    )
    assert "unknown_base_class" not in notes


# D7: testpaths as pytest uses it.


def test_d7_a_missing_testpaths_entry_falls_back_to_the_rootdir(repo):
    targets, _ = _discovered(
        repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = test\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
        },
    )
    assert targets == {"tests/test_a.py::test_a"}


def test_d7_a_file_named_in_testpaths_bypasses_python_files(repo):
    targets, _ = _discovered(
        repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests checks/smoke.py\n",
            "checks/smoke.py": "def test_s():\n    pass\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
        },
    )
    assert targets == {"tests/test_a.py::test_a", "checks/smoke.py::test_s"}


# D9: doctests as pytest names and collects them.


def test_d9_doctest_names_follow_pytests_import_mode(repo):
    targets, _ = _discovered(
        repo,
        {
            "pytest.ini": "[pytest]\naddopts = --doctest-modules\n",
            "tools/helpers.py": "def f():\n    '''\n    >>> 1\n    1\n    '''\n",
            "pkg/__init__.py": "",
            "pkg/mod.py": (
                "import sys\n\nif sys.platform:\n    def g():\n        '''\n        >>> 2\n"
                "        2\n        '''\n"
            ),
        },
    )
    # No __init__.py in tools/: pytest imports the module as ``helpers``; a
    # def under ``if`` is a module attribute like any other.
    assert {"tools/helpers.py::helpers.f", "pkg/mod.py::pkg.mod.g"} <= targets


def test_d9_doctests_discovery_cannot_read_are_reported(repo):
    _, notes = _discovered(
        repo,
        {
            "pytest.ini": "[pytest]\naddopts = --doctest-modules --doctest-glob=*.doctest\n",
            "how-to/example.py": "def f():\n    '''\n    >>> 1\n    1\n    '''\n",
            "docs/a.doctest": ">>> 1\n1\n",
            "pkg/__init__.py": "",
            "pkg/mod.py": "__test__ = {'extra': '>>> 1\\n1\\n'}\n",
        },
    )
    assert {"unparsed_file", "unmodelled_test_binding"} <= notes


# D10: ASV's own rules.


def test_d10_asv_names_as_asv_does(repo):
    targets, _ = _discovered(
        repo,
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "def time_root():\n    pass\n",
            "benchmarks/_common.py": (
                "def time_private_module():\n    pass\n\n\n"
                "class Base:\n    def time_base(self):\n        pass\n"
            ),
            "benchmarks/bench_a.py": (
                "from ._common import Base\n\n\n"
                "class Suite:\n    def TimeCamel(self):\n        pass\n\n"
                "    def TrackCamel(self):\n        return 1\n\n"
                "    def time_a(self):\n        pass\n\n    time_alias = time_a\n"
            ),
        },
        runner="asv",
    )
    assert {
        "time_root",
        "_common.time_private_module",
        "_common.Base.time_base",
        "bench_a.Base.time_base",
        "bench_a.Suite.TimeCamel",
        "bench_a.Suite.TrackCamel",
        "bench_a.Suite.time_a",
        "bench_a.Suite.time_alias",
    } <= targets


# R: running and checking.

PYTEST = f"{sys.executable} -m pytest -p no:cacheprovider"
CALC = {
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a + b\n\n\ndef other():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_ops.py": (
        "import os\n\nfrom calc.ops import add, other\n\n\n"
        "def test_x():\n    if os.environ.get('FLAG') == 'on':\n        assert other() == 1\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    ),
}


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_r1_an_empty_evidence_selection_still_checks_the_environment(repo, monkeypatch, capsys):
    """Recorded with FLAG off, test_x never called other(); run with FLAG on,
    the evidence selects nothing for a change to other(), but it does not
    apply here: the static plan runs, and test_x fails."""
    base = repo.commit(CALC)
    monkeypatch.setenv("FLAG", "off")
    repo.collect(env_variables=["FLAG"])
    # A different size: a same-size edit within the second of the recording
    # run would reuse the stale .pyc (Python checks mtime and size).
    ops = CALC["calc/ops.py"].replace("return 1", "return 1 + 1")
    head = repo.commit({"calc/ops.py": ops})
    monkeypatch.setenv("FLAG", "on")
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
            "--evidence",
            "auto",
            "--command",
            PYTEST,
            "--",
            "-q",
        ]
    )
    err = capsys.readouterr().err
    assert "environment differs" in err
    assert code == 1  # test_x ran under the static plan, and failed


def test_r2_a_symlinked_repo_path_selects_the_right_tests(repo, tmp_path, capfd):
    base = repo.commit({**CALC})
    head = repo.commit({"calc/ops.py": CALC["calc/ops.py"].replace("a + b", "b + a")})
    link = tmp_path / "link"
    link.symlink_to(repo.path)
    code = main(
        [
            "run",
            "--repo",
            str(link),
            "--base",
            base,
            "--head",
            head,
            "--discover",
            "pytest",
            "--command",
            PYTEST,
            "--",
            "-q",
        ]
    )
    out = capfd.readouterr()
    assert code == 0, out.err
    assert "1 passed" in out.out and "did not collect" not in out.err


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


def test_r5_validate_sees_outcomes_whatever_the_projects_verbosity(repo, capsys):
    from diffcone.execution import validate_pytest

    base = repo.commit({**CALC, "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n'})
    head = repo.commit({"calc/ops.py": CALC["calc/ops.py"].replace("a + b", "a + b + 1")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    result = validate_pytest(plan, repo=repo.path, command=PYTEST)
    changed = {o.runner_id for o in result.outcomes if o.changed}
    assert changed == {"tests/test_ops.py::test_add"}


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


def test_r8_doctests_asked_for_after_the_separator_are_planned_and_run(repo, capfd):
    base = repo.commit(
        {
            **CALC,
            "calc/ops.py": CALC["calc/ops.py"].replace(
                "def other():\n", "def other():\n    '''\n    >>> other()\n    1\n    '''\n"
            ),
        }
    )
    ops = repo.git("show", "HEAD:calc/ops.py")
    head = repo.commit({"calc/ops.py": ops.replace("    return 1", "    return 2")})
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
            "--command",
            PYTEST,
            "--",
            "-q",
            "--doctest-modules",
        ]
    )
    out = capfd.readouterr()
    assert code == 1, out.err  # the doctest ran, and failed
    assert "calc.ops.other" in out.out


def test_r8_unknown_collected_tests_are_run_not_dropped(repo, capsys):
    from diffcone.execution import run_selected

    base = repo.commit(CALC)
    head = repo.commit({"calc/ops.py": CALC["calc/ops.py"].replace("a + b", "b + a")})
    plan = repo.plan(
        base, head, [py_target("tests/test_ops.py::test_add", "tests.test_ops.test_add")]
    )
    result = run_selected(plan, "pytest", cwd=repo.path, command=PYTEST, extra=["-q"])
    assert result.unknown == ["tests/test_ops.py::test_x"]


# E: evidence mode. Each case records at base, changes one thing, and checks
# that the test whose outcome changes is selected.

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")


def _evidence_selects(repo, files, change, roots=None):
    base = repo.commit({".gitignore": "__pycache__/\n.diffcone/\n", **files})
    evidence = repo.collect(source_roots=roots)
    head = repo.commit(change)
    plan = repo.plan(
        base, head, [], source_roots=roots, discover_runners=["pytest"], evidence=evidence
    )
    return selected(plan)


GEN = (
    "def _counter():\n    n = 0\n    while True:\n        n += {step}\n        yield n\n\n\n"
    "_ids = _counter()\n\n\ndef next_id():\n    return next(_ids)\n"
)


@needs_monitoring
def test_e5_a_generator_resumed_in_a_later_test_is_credited_to_it(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/gen.py": GEN.format(step=1),
            "tests/__init__.py": "",
            "tests/test_a.py": "from lib.gen import next_id\n\n\ndef test_a():\n    next_id()\n",
            "tests/test_b.py": (
                "from lib.gen import next_id\n\n\ndef test_b():\n"
                "    assert next_id() + 1 == next_id()\n"
            ),
        },
        {"lib/gen.py": GEN.format(step=2)},
    )
    assert "tests/test_b.py::test_b" in chosen


@needs_monitoring
def test_e7_a_file_read_through_pkgutil_selects_its_readers(repo):
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/data.json": '{"v": 1}\n',
            "lib/cfg.py": (
                "import json\nimport pkgutil\n\n\ndef load():\n"
                "    return json.loads(pkgutil.get_data('lib', 'data.json'))['v']\n"
            ),
            "tests/__init__.py": "",
            "tests/test_cfg.py": "from lib.cfg import load\n\n\ndef test_load():\n    load()\n",
        },
        {"lib/data.json": '{"v": 2}\n'},
    )
    assert "tests/test_cfg.py::test_load" in chosen


@needs_monitoring
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


REG = (
    "REGISTRY = []\n\n\ndef register(f):\n    REGISTRY.append(f)\n    return f\n\n\n"
    "def count():\n    return len(REGISTRY)\n"
)
SUBS = (
    "class Base:\n    registry = []\n\n    def __init_subclass__(cls, **kw):\n"
    "        super().__init_subclass__(**kw)\n        Base.registry.append(cls)\n\n\n"
    "def names():\n    return [c.__name__ for c in Base.registry]\n"
)


@needs_monitoring
def test_e3_adding_a_registering_definition_reaches_the_registrys_users(repo):
    handlers = "from lib.reg import register\n\n\n@register\ndef one():\n    return 1\n"
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "from lib import handlers\n",
            "lib/reg.py": REG,
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


@needs_monitoring
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


STAR = "__all__ = [{}]\n\n\ndef one():\n    return 1\n\n\ndef two():\n    return 2\n"


@needs_monitoring
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


@needs_monitoring
def test_e1_e6_code_reached_through_dotdot_paths_and_files_outside_the_roots(repo):
    chosen = _evidence_selects(
        repo,
        {
            "src/lib/__init__.py": "",
            "src/lib/core.py": "def f():\n    return 1\n",
            "conftest.py": "import pytest\n\n\n@pytest.fixture\ndef val():\n    return 1\n",
            "tests/conftest.py": (
                "import os\nimport sys\n\n"
                "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            ),
            "tests/test_x.py": (
                "from lib.core import f\n\n\ndef test_x():\n    f()\n\n\n"
                "def test_val(val):\n    pass\n"
            ),
        },
        {"src/lib/core.py": "def f():\n    return 22\n"},
        roots=["src", "tests"],
    )
    assert "tests/test_x.py::test_x" in chosen


def test_e10_a_cython_function_with_profiling_off_selects_through_its_callers():
    from diffcone.cython import read

    module = read(
        "pkg/_fast.pyx",
        "cimport cython\n\n@cython.profile(False)\ncdef int _scale(int x):\n    return x * 3\n\n"
        "def tripled(int x):\n    return _scale(x)\n",
    )
    flags = {f.name: f.unprofiled for f in module.functions}
    assert flags == {"_scale": True, "tripled": False}


@needs_monitoring
def test_e4_a_spawned_multiprocessing_child_flags_its_test(repo):
    work = (
        "import multiprocessing as mp\n\n\ndef square(x):\n    return x * {k}\n\n\n"
        "def run():\n    with mp.get_context('spawn').Pool(1) as pool:\n"
        "        return pool.map(square, [3])[0]\n"
    )
    chosen = _evidence_selects(
        repo,
        {
            "lib/__init__.py": "",
            "lib/work.py": work.format(k="x"),
            "tests/__init__.py": "",
            "tests/test_w.py": "from lib.work import run\n\n\ndef test_spawn():\n    run()\n",
        },
        {"lib/work.py": work.format(k="2")},
    )
    assert "tests/test_w.py::test_spawn" in chosen


# D12: a configured plugin that collects its own files is reported.


def test_d12_a_configured_collecting_plugin_makes_discovery_incomplete(repo):
    _, notes = _discovered(
        repo,
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntyping_checkers = ["mypy"]\n',
            "tests/test_a.py": "def test_a():\n    pass\n",
            "tests/test_types.md": "# cases\n",
        },
    )
    assert "plugin_collects_files" in notes

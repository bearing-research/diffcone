"""Planning across imports: what an import runs and binds, and modules
reached as values.

An import added, removed, moved or reordered changes what runs at import
and reaches the importers; an import of code that already ran does not.
Every binding of an imported name (``try``/``except`` fallbacks, star
imports, version branches, a decorator or base bound another way) reaches
the name's users, as do names a module ``__getattr__`` or ``__dir__``
serves and modules a patcher imports by dotted string. Module names the
source roots cannot bound and objects installed in ``sys.modules`` are
unbounded or reach the importers. A module handed on as a value, obtained
by a literal name or read through another name depends on its members.
An import or a run of a file ``[index] exclude`` keeps out of the analysis
is an unknown dependency; ``pytest.importorskip``, ``find_spec``,
``pkgutil`` and ``pydoc.locate`` import by name as ``import_module`` does.

Scenarios mix pytest and ASV targets and assert exact target sets.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from diffcone.cache import ModuleCache, index_to_dict
from diffcone.classify import ChangeKind, SymbolChange
from diffcone.indexer import build_index
from diffcone.model import (
    INSTALLED,
    Edge,
    EdgeKind,
    SnapshotInfo,
    SymbolKind,
    UnresolvedKind,
)
from diffcone.planner import _Graph, _import_call_effects
from diffcone.snapshot import Snapshot
from diffcone.testing import (
    FixtureRepo,
    asv_target,
    changes,
    path_ids,
    py_target,
    reason,
    rules,
    selected,
    unselected,
)

# --------------------------------------------------------------------------- import changes

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'

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
        (
            "def load():\n    import pkg.plugin\n",
            "import pkg.plugin\n\n\ndef load():\n    pass\n",
        ),
        (
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import pkg.plugin\n",
            "from typing import TYPE_CHECKING\n\nimport pkg.plugin\n\n"
            "if TYPE_CHECKING:\n    import pkg.plugin\n",
        ),
    ],
)
def test_f5_an_import_newly_run_at_import_reaches_the_importers(repo, before, after):
    base = repo.commit({**PLUGIN, "pkg/a.py": before})
    head = repo.commit({"pkg/a.py": after})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_a.py::test_use" in selected(plan)
    assert "bench.time_noop" in unselected(plan)


def test_f6_an_import_moved_across_a_statement_is_a_change(repo):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/config.py": "import os\n\nMODE = os.environ.get('MODE', 'slow')\n",
            "pkg/app.py": "import os\n\nos.environ['MODE'] = 'fast'\nimport pkg.config\n",
            "tests/__init__.py": "",
            "tests/test_app.py": (
                "import pkg.app\nfrom pkg.config import MODE\n\n\n"
                "def test_mode():\n    assert MODE == 'fast'\n"
            ),
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit(
        {"pkg/app.py": "import os\n\nimport pkg.config\nos.environ['MODE'] = 'fast'\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_app.py::test_mode" in selected(plan)
    assert "bench.time_noop" in unselected(plan)


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


BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _plan_discovered(repo, base, head, targets=(), evidence=None, **kwargs):
    return repo.plan(
        base, head, list(targets), discover_runners=["pytest"], evidence=evidence, **kwargs
    )


REG = {
    "pkg/reg.py": "REG = {}\n\n\ndef names():\n    return sorted(REG)\n",
    "pkg/plug.py": "import pkg.reg\n\npkg.reg.REG['a'] = 1\n",
    "tests/test_y.py": "from pkg.reg import names\n\n\ndef test_y():\n    assert names() == []\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.reg import names\n\n\ndef time_names():\n    names()\n",
}

BENCH_NAMES = asv_target("bench.time_names", "benchmarks.bench.time_names", "benchmarks.bench")

IMPORT_CASES = {
    "test_module": (
        "tests/test_x.py",
        "def test_x():\n    pass\n",
        "import pkg.plug\n\n\ndef test_x():\n    pass\n",
    ),
    "from_import": (
        "tests/test_x.py",
        "def test_x():\n    pass\n",
        "from pkg import plug\n\n\ndef test_x():\n    pass\n",
    ),
    "function_local": (
        "tests/test_x.py",
        "def test_x():\n    pass\n",
        "def test_x():\n    import pkg.plug\n",
    ),
    "project_module": (
        "pkg/cli.py",
        "def main():\n    return 0\n",
        "import pkg.plug\n\n\ndef main():\n    return 0\n",
    ),
}


@pytest.mark.parametrize("case", sorted(IMPORT_CASES))
def test_r24_import_added_runs_a_registering_module(repo, case):
    path, before, after = IMPORT_CASES[case]
    files = {**BASE, **REG, path: before}
    if path.startswith("pkg/"):
        files["tests/test_x.py"] = "import pkg.cli\n\n\ndef test_x():\n    assert pkg.cli.main\n"
    base = repo.commit(files)
    head = repo.commit({path: after})
    plan = _plan_discovered(repo, base, head, [BENCH_NAMES])
    assert selected(plan) == {
        "tests/test_x.py::test_x",
        "tests/test_y.py::test_y",
        "bench.time_names",
    }
    why = reason(plan, "tests/test_y.py::test_y")
    assert why.rule == "dependency"
    assert "pkg.reg.REG" in path_ids(why) and "pkg.plug" in path_ids(why)
    assert any(step.kind == "newly_imported_by" for step in why.path)


def test_r24_import_removed_stops_a_registering_module(repo):
    base = repo.commit(
        {**BASE, **REG, "tests/test_x.py": "import pkg.plug\n\n\ndef test_x():\n    pass\n"}
    )
    head = repo.commit({"tests/test_x.py": "def test_x():\n    pass\n"})
    plan = _plan_discovered(repo, base, head, [BENCH_NAMES])
    assert selected(plan) == {
        "tests/test_x.py::test_x",
        "tests/test_y.py::test_y",
        "bench.time_names",
    }
    why = reason(plan, "bench.time_names")
    assert any(step.kind == "no_longer_imported_by" for step in why.path)


def test_r24_import_added_binds_the_submodule_on_its_package(repo):
    """``import pkg.sub`` added in one test module makes ``pkg.sub`` reachable
    under ``import pkg`` in another, which read it through the package."""
    base = repo.commit(
        {
            **BASE,
            "pkg/sub.py": "def f():\n    return 1\n",
            "tests/test_x.py": "def test_x():\n    pass\n",
            "tests/test_y.py": "import pkg\n\n\ndef test_y():\n    assert pkg.sub.f() == 1\n",
        }
    )
    head = repo.commit({"tests/test_x.py": "import pkg.sub\n\n\ndef test_x():\n    pass\n"})
    plan = _plan_discovered(repo, base, head)
    assert selected(plan) == {"tests/test_x.py::test_x", "tests/test_y.py::test_y"}
    assert any(
        step.kind == "bound_on_package" for step in reason(plan, "tests/test_y.py::test_y").path
    )


def test_r24_import_already_in_the_closure_runs_nothing_new(repo):
    """Guard (precision): an import of a module the importer's own imports
    already ran leaves nothing different behind."""
    base = repo.commit(
        {
            **BASE,
            **REG,
            "pkg/core.py": "import pkg.plug\n",
            "tests/test_x.py": "import pkg.core\n\n\ndef test_x():\n    pass\n",
        }
    )
    head = repo.commit(
        {"tests/test_x.py": "import pkg.core\nimport pkg.plug\n\n\ndef test_x():\n    pass\n"}
    )
    plan = _plan_discovered(repo, base, head, [BENCH_NAMES])
    assert changes(plan) == {"tests.test_x": ("imports_added",)}
    assert selected(plan) == set()


# --------------------------------------------------------------------------- what an import runs


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


STATE = (
    'MODE = ["slow"]\n\n\n'
    "def set_mode(m):\n    _store(m)\n    return m\n\n\n"
    "def _store(m):\n    MODE[0] = m\n\n\n"
    "def mode():\n    return MODE[0]\n"
)

READERS = {
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "pkg/state.py": STATE,
    "tests/test_b.py": (
        "from pkg.state import mode\n\n\ndef test_b():\n    assert mode() == 'slow'\n"
    ),
    "benchmarks/bench_mode.py": "from pkg.state import mode\n\n\ndef time_mode():\n    mode()\n",
    "benchmarks/bench_x.py": "def time_x():\n    pass\n",
}

CALL_SITES = {
    "library variable": {
        "pkg/config.py": 'from pkg.state import set_mode\n\nX = set_mode("slow")\n',
        "tests/test_a.py": "import pkg.config  # noqa\n\n\ndef test_a():\n    pass\n",
    },
    "library class attribute": {
        "pkg/config.py": 'from pkg.state import set_mode\n\n\nclass K:\n    x = set_mode("slow")\n',
        "tests/test_a.py": "import pkg.config  # noqa\n\n\ndef test_a():\n    pass\n",
    },
    "library module statement": {
        "pkg/config.py": 'from pkg.state import set_mode\n\nset_mode("slow")\n',
        "tests/test_a.py": "import pkg.config  # noqa\n\n\ndef test_a():\n    pass\n",
    },
    "test decorator": {
        "tests/test_a.py": (
            "import pytest\n\nfrom pkg.state import set_mode\n\n\n"
            '@pytest.mark.parametrize("x", [set_mode("slow")])\ndef test_a(x):\n    pass\n'
        ),
    },
    "test default": {
        "tests/test_a.py": (
            'from pkg.state import set_mode\n\n\ndef test_a(x=set_mode("slow")):\n    pass\n'
        ),
    },
    "test module variable": {
        "tests/test_a.py": (
            'from pkg.state import set_mode\n\nX = set_mode("slow")\n\n\ndef test_a():\n    pass\n'
        ),
    },
}


def test_evp6_module_seed_is_explained_by_the_change_it_runs(repo):
    """A target reached through its module's import of a changed variable
    was explained as a dynamic reference with no detail."""
    sites = CALL_SITES["library variable"]
    base = repo.commit({**READERS, **sites})
    head = repo.commit({"pkg/config.py": sites["pkg/config.py"].replace("slow", "fast")})
    targets = [py_target("tests/test_a.py::test_a", "tests.test_a.test_a")]
    plan = repo.plan(base, head, targets, source_roots=["."])
    assert rules(plan, "tests/test_a.py::test_a") == {"dependency"}
    why = reason(plan, "tests/test_a.py::test_a")
    assert path_ids(why)[-2:] == ["pkg.config", "pkg.config.X"]
    assert why.path[-1].kind == "runs_at_import"
    assert why.changed_symbol == "pkg.config.X"


NOOP = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")

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


# --------------------------------------------------------------------------- imported name bindings


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


@pytest.mark.parametrize(
    "binding",
    [
        "from pkg.a import helper\nfrom pkg.b import *\n",
        "def helper():\n    return 0\n\n\nfrom pkg.b import *\n",
        "from pkg.a import *\nfrom pkg.b import *\n",
        "from pkg.a import *\n\ntry:\n    from pkg.b import *\nexcept ImportError:\n    pass\n",
    ],
)
def test_s4_a_later_star_import_reaches_the_names_users(repo, binding):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def helper():\n    return 1\n",
            "pkg/b.py": "def helper():\n    return 2\n",
            "pkg/s.py": binding + "\n\ndef use():\n    return helper()\n",
            "pkg/other.py": "def g():\n    return 3\n",
            "tests/__init__.py": "",
            "tests/test_s.py": "from pkg.s import use\n\n\ndef test_use():\n    use()\n",
            "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    g()\n",
        }
    )
    head = repo.commit({"pkg/b.py": "def helper():\n    return 22\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_s.py::test_use" in selected(plan)
    assert "tests/test_other.py::test_g" in unselected(plan)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "def f():\n    return dumps({})\n",
            "from json import dumps\n\n\ndef f():\n    return dumps({})\n",
        ),
        (
            "def f(x):\n    return round(x)\n",
            "from math import floor as round\n\n\ndef f(x):\n    return round(x)\n",
        ),
        (
            "import dataclasses\n\n\n@dataclasses.dataclass\nclass D:\n    x: int = 0\n\n\n"
            "def f():\n    return dataclasses.fields(D)[0].type\n",
            "from __future__ import annotations\n\nimport dataclasses\n\n\n@dataclasses.dataclass\n"
            "class D:\n    x: int = 0\n\n\ndef f():\n    return dataclasses.fields(D)[0].type\n",
        ),
    ],
)
def test_s5_an_import_from_outside_the_roots_reaches_the_names_users(repo, before, after):
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": before,
            "pkg/other.py": "def g():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_m.py": (
                "from pkg.m import f\n\n\n"
                "def test_f():\n    f(1.5) if f.__code__.co_argcount else f()\n"
            ),
            "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    g()\n",
        }
    )
    head = repo.commit({"pkg/m.py": after})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert "tests/test_m.py::test_f" in selected(plan)
    assert "tests/test_other.py::test_g" in unselected(plan)


# A benchmark on code no scenario changes: a plan that selects it selected
# everything.
BENCH = {
    "pkg/__init__.py": "",
    "pkg/quiet.py": "def idle():\n    return 0\n",
    "benchmarks/bench_quiet.py": "from pkg.quiet import idle\n\n\ndef time_idle():\n    idle()\n",
}

BENCH_TARGET = asv_target("bench_quiet.time_idle", "benchmarks.bench_quiet.time_idle")


def _plan_test_ids(repo: FixtureRepo, base: dict, head: dict, *tests: str):
    """Plan ``head`` against ``base`` with a pytest target per test id
    (``tests/test_m.py::test_x``) and the benchmark."""
    targets = [py_target(t, t.replace("/", ".").replace(".py::", ".")) for t in tests] + [
        BENCH_TARGET
    ]
    first = repo.commit({**BENCH, **base})
    second = repo.commit(head)
    return repo.plan(first, second, targets)


DECO_READS_DEFAULT = (
    "import os\n\n\ndef final(f):\n    os.environ['DC_Y'] = str(f.__defaults__[0])\n    return f\n"
)

TEST_ENV_Y = "import os\nimport pkg.m\n\n\ndef test_env():\n    assert os.environ['DC_Y'] == '1'\n"


def _q12(repo, imports: str):
    m = imports + "\n\n@final\ndef helper(x={v}):\n    return x\n"
    return _plan_test_ids(
        repo,
        {
            "pkg/deco.py": DECO_READS_DEFAULT,
            "pkg/m.py": m.format(v=1),
            "tests/test_m.py": TEST_ENV_Y,
        },
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_env",
    )


def test_q12_decorator_imported_another_way_in_a_try_is_not_inert(repo):
    imports = (
        "try:\n    from pkg.deco import final\nexcept ImportError:\n    from typing import final\n"
    )
    assert selected(_q12(repo, imports)) == {"tests/test_m.py::test_env"}


def test_q12_decorator_a_star_import_may_rebind_is_not_inert(repo):
    imports = "from typing import final\nfrom pkg.deco import *  # noqa\n"
    assert selected(_q12(repo, imports)) == {"tests/test_m.py::test_env"}


def test_q12_decorator_the_module_rebinds_is_not_inert(repo):
    imports = "from typing import final\nfrom pkg import deco\n\nfinal = deco.final\n"
    assert selected(_q12(repo, imports)) == {"tests/test_m.py::test_env"}


def test_q12_base_imported_another_way_may_call_back(repo):
    # ``framework`` (installed, outside the source roots) may be the live
    # ``Base``, and its code calls ``hook`` on a subclass instance: the class
    # depends on its methods.
    m = (
        "try:\n    from framework import Base\n"
        "except ImportError:\n    from typing import Protocol as Base\n\n\n"
        "class Plugin(Base):\n    def hook(self):\n        return {v}\n"
    )
    test = "from pkg.m import Plugin\n\n\ndef test_hook():\n    assert Plugin().result == 1\n"
    plan = _plan_test_ids(
        repo,
        {"pkg/m.py": m.format(v=1), "tests/test_m.py": test},
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_hook",
    )
    assert selected(plan) == {"tests/test_m.py::test_hook"}


def test_q12_package_attribute_bound_another_way_in_a_try(repo):
    # ``pkg.impl`` is both a submodule and a name the package binds, by one
    # import or the other: a use of it depends on both.
    init = (
        "try:\n    from pkg.fast import impl\nexcept ImportError:\n    from pkg.slow import impl\n"
    )
    fast = "def impl():\n    return {v}\n"
    test = "from pkg import impl\n\n\ndef test_impl():\n    assert impl() == 1\n"
    plan = _plan_test_ids(
        repo,
        {
            "pkg/__init__.py": init,
            "pkg/impl.py": "",
            "pkg/fast.py": fast.format(v=1),
            "pkg/slow.py": "def impl():\n    return 1\n",
            "tests/test_m.py": test,
        },
        {"pkg/fast.py": fast.format(v=2)},
        "tests/test_m.py::test_impl",
    )
    assert selected(plan) == {"tests/test_m.py::test_impl"}


def test_q12_function_local_import_bound_another_way_in_a_try(repo):
    run = (
        "def run():\n    try:\n        import pkg.fast as backend\n"
        "    except ImportError:\n        import pkg.slow as backend\n    return backend.impl()\n"
    )
    fast = "def impl():\n    return {v}\n"
    test = "from pkg.run import run\n\n\ndef test_run():\n    assert run() == 1\n"
    plan = _plan_test_ids(
        repo,
        {
            "pkg/run.py": run,
            "pkg/fast.py": fast.format(v=1),
            "pkg/slow.py": "def impl():\n    return 1\n",
            "tests/test_m.py": test,
        },
        {"pkg/fast.py": fast.format(v=2)},
        "tests/test_m.py::test_run",
    )
    assert selected(plan) == {"tests/test_m.py::test_run"}


# --------------------------------------------------------------------------- module __getattr__


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


# The lazy-export table of 0.2.0.
LAZY = (
    "import importlib\n\n_LAZY = {{'Thing': ('pkg.impl_a', 'Thing')}}\n{extra}\n\n\n"
    "def __getattr__(name):\n    target = _LAZY.get(name)\n"
    "    if target is None:\n        raise AttributeError(name)\n"
    "    return getattr(importlib.import_module(target[0]), target[1])\n"
)


def _constructor_plan(repo, init, test, extra=None):
    base = repo.commit(
        {
            "pkg/__init__.py": init,
            "pkg/impl_b.py": "class Alt:\n    def __init__(self):\n        self.v = 1\n",
            "tests/test_t.py": test,
            "benchmarks/bench_t.py": "def time_nothing():\n    pass\n",
            **(extra or {}),
        }
    )
    head = repo.commit(
        {"pkg/impl_b.py": "class Alt:\n    def __init__(self):\n        self.v = 2\n"}
    )
    targets = [
        py_target("t::test_thing", "tests.test_t.test_thing"),
        asv_target("bench.time_nothing", "benchmarks.bench_t.time_nothing"),
    ]
    return repo.plan(base, head, targets)


LAZY_ALT = LAZY.replace("('pkg.impl_a', 'Thing')", "('pkg.impl_b', 'Alt')").format(extra="")


@pytest.mark.parametrize(
    "init",
    [
        LAZY_ALT,
        "import importlib\n\n\ndef __getattr__(name):\n"
        "    return getattr(importlib.import_module('pkg.impl_b'), 'Alt')\n",
    ],
    ids=["lazy_table", "literal_import_module"],
)
def test_pln1_a_class_served_by_getattr_reaches_its_constructor(repo, init):
    test = "import pkg\n\n\ndef test_thing():\n    assert pkg.Thing().v == 1\n"
    assert selected(_constructor_plan(repo, init, test)) == {"t::test_thing"}


def test_pln1_a_class_named_on_an_unknown_receiver_reaches_its_constructor(repo):
    test = (
        "import importlib\n\nfrom pkg.make import make\n\n\ndef test_thing():\n"
        "    assert make(importlib.import_module('pkg.impl_b')).v == 1\n"
    )
    extra = {"pkg/make.py": "def make(m):\n    return m.Alt()\n"}
    plan = _constructor_plan(repo, "", test, extra)
    assert selected(plan) == {"t::test_thing"}
    # The module ``import_module('pkg.impl_b')`` yields is handed to ``make``
    # (audit round 5, R25): ``Alt`` and its constructor are reached through
    # the module's members, a dependency rather than a name match.
    assert rules(plan, "t::test_thing") == {"dependency"}


ENUM_RUNNERS = {
    "tests/test_x.py": "from pkg import api\n\n\ndef test_x():\n    assert api.run() == 1\n",
    "tests/test_other.py": "def test_other():\n    pass\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg import api\n\n\ndef time_run():\n    api.run()\n",
}

BENCH_RUN = asv_target("bench.time_run", "benchmarks.bench.time_run", "benchmarks.bench")


def test_r21_dir_of_a_module_depends_on_its_dir_hook(repo):
    base = repo.commit(
        {
            **BASE,
            **ENUM_RUNNERS,
            "pkg/mod.py": "def __dir__():\n    return ['a']\n",
            "pkg/api.py": "from pkg import mod\n\n\ndef run():\n    return len(dir(mod))\n",
        }
    )
    head = repo.commit({"pkg/mod.py": "def __dir__():\n    return ['a', 'b']\n"})
    plan = _plan_discovered(repo, base, head, [BENCH_RUN])
    assert changes(plan) == {"pkg.mod.__dir__": ("body_changed",)}
    assert selected(plan) == {"tests/test_x.py::test_x", "bench.time_run"}
    assert "pkg.mod.__dir__" in path_ids(reason(plan, "tests/test_x.py::test_x"))


# --------------------------------------------------------------------------- unbounded module names


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


def test_s1_an_import_the_roots_name_differently_is_unbounded(repo):
    base = repo.commit(
        {
            "src/calc/__init__.py": "",
            "src/calc/ops.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": (
                "from calc.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
            ),
            "benchmarks/__init__.py": "",
            "benchmarks/bench.py": "def time_noop():\n    pass\n",
        }
    )
    head = repo.commit({"src/calc/ops.py": "def add(a, b):\n    return a - b\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_calc.py::test_add" in selected(plan)
    assert any("source root" in u.detail for u in plan.unresolved)


# --------------------------------------------------------------------------- sys.modules installs

HANDLERS = "def a():\n    return {}\n\n\ndef b():\n    return {}\n"

# ``pkg.core.run`` looks a handler up by a name a literal table gives (``a``);
# ``pkg.handlers.b`` changes, so the plan selects ``run``'s users only when
# something may change the table.
CORE = (
    "from pkg import handlers\n\nTABLE = {'k': 'a'}\n\n\n"
    "def run():\n    return getattr(handlers, TABLE['k'])()\n"
)

TEST_RUN = "from pkg.core import run\n\n\ndef test_s():\n    run()\n"

ALT = "def fn():\n    return 1\n"


@pytest.mark.parametrize(
    "compat, reached",
    [
        ("sys.modules['pkg.core'] = types.SimpleNamespace(run=alt.fn)\n", True),
        (
            "\n\ndef test_swap(monkeypatch):\n"
            "    fake = types.SimpleNamespace(run=alt.fn)\n"
            "    monkeypatch.setitem(sys.modules, 'pkg.core', fake)\n",
            False,
        ),
    ],
    ids=["for good", "for one test"],
)
def test_w17_an_import_gets_what_was_installed_for_good(repo, compat, reached):
    # Installed for good at import (of a module only another test imports),
    # pkg.core is the fake for every later import: its importers depend on
    # what the installing code put there. Installed for one test, it is gone
    # before the next.
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/handlers.py": HANDLERS,
            "pkg/core.py": CORE,
            "pkg/alt.py": ALT,
            "pkg/compat.py": f"import sys\nimport types\n\nfrom pkg import alt\n\n{compat}",
            "tests/test_a.py": "import pkg.compat  # noqa: F401\n\n\ndef test_a():\n    pass\n",
            "tests/test_b.py": TEST_RUN.replace("test_s", "test_b"),
            "benchmarks/bench_b.py": "from pkg.core import run\n\n\ndef time_b():\n    run()\n",
        }
    )
    head = repo.commit({"pkg/alt.py": ALT.replace("1", "2")})
    targets = [
        py_target("t::test_b", "tests.test_b.test_b"),
        asv_target("bench.time_b", "benchmarks.bench_b.time_b"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == ({"t::test_b", "bench.time_b"} if reached else set())


@pytest.mark.parametrize("detail", ["", INSTALLED])
def test_w17_a_module_does_not_run_what_is_installed_as_it(detail):
    # The edge from a module to the code installing something as it is a
    # dependency, not a call: a change to the module's import-time code does
    # not run the installer, so what the installer fills stays as it was.
    graph = _Graph()
    graph.add(Edge("pkg.core", "pkg.compat.install", EdgeKind.REFERENCES, detail), ("base", "head"))
    graph.add(
        Edge("pkg.compat.REG", "pkg.compat.install", EdgeKind.REFERENCES, "mutated_by"), ("head",)
    )
    change = SymbolChange("pkg.core", SymbolKind.MODULE, (ChangeKind.BODY_CHANGED,), None, None)
    effects = _import_call_effects(graph, [change.id], {"pkg.core"})
    assert ("pkg.compat.REG" in effects) is (detail == "")


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


@pytest.mark.parametrize(
    ("name", "reached"),
    [("name", True), ("'pkg.' + name", True), ("'other.' + name", False), ("'pkg.x'", False)],
    ids=["unbounded", "prefix", "other-prefix", "literal"],
)
def test_w25_an_install_under_a_name_nothing_bounds(repo, name, reached):
    """``install`` puts an object in ``sys.modules`` for good under a name
    nothing bounds: an import of ``pkg.core`` may get it, so importers of
    every in-scope module (the prefix allowing) depend on the installer."""
    files = {**W25_FILES, "pkg/inst.py": INSTALLER.replace("[name]", f"[{name}]")}
    base = repo.commit(files)
    head = repo.commit({"pkg/inst.py": files["pkg/inst.py"].replace("X=1", "X=2")})
    targets = [
        py_target("t::test_core", "tests.test_core.test_core", "tests.test_core"),
        py_target("t::test_inst", "tests.test_inst.test_inst", "tests.test_inst"),
        asv_target("bench.core", "pkg.core"),
    ]
    plan = repo.plan(base, head, targets)
    expected = {"t::test_inst"} | ({"t::test_core", "bench.core"} if reached else set())
    assert selected(plan) == expected


# --------------------------------------------------------------------------- modules handed on

READER = "def read(obj, names):\n    return getattr(obj, names[0])()\n"

OPS_ADD_OTHER = "def add(a, b):\n    return a + b\n\n\ndef other():\n    return 1\n"


@pytest.mark.parametrize("passed", [True, False])
def test_w9_a_module_passed_on_depends_on_its_members(repo, passed):
    call = "read(ops, ['other'])" if passed else "ops.add(1, 2)"
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS_ADD_OTHER,
            "pkg/reader.py": READER,
            "tests/test_r.py": (
                f"from pkg import ops\nfrom pkg.reader import read\n\n\ndef test_r():\n    {call}\n"
            ),
            "benchmarks/bench_r.py": (
                f"from pkg import ops\nfrom pkg.reader import read\n\n\ndef time_r():\n    {call}\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": OPS_ADD_OTHER.replace("return 1", "return 2")})
    targets = [
        py_target("t::test_r", "tests.test_r.test_r"),
        asv_target("bench.time_r", "benchmarks.bench_r.time_r"),
    ]
    plan = repo.plan(base, head, targets)
    if passed:
        assert selected(plan) == {"t::test_r", "bench.time_r"}
        assert rules(plan, "t::test_r") == {"dependency"}
    else:
        # A module only read from is not handed on: ``ops.add`` alone is
        # what the test depends on.
        assert selected(plan) == set()


PATH_READER = (
    "def read(obj, names):\n    for name in names:\n        obj = getattr(obj, name)\n"
    "    return obj()\n"
)


@pytest.mark.parametrize(
    "api, call",
    [
        ("from pkg.ops import other\n", "read(api, ['other'])"),
        ("from pkg import ops\n", "read(api, ['ops', 'other'])"),
        ("from pkg.ops import *\n", "read(api, ['other'])"),
    ],
    ids=["an imported function", "an imported module", "a star import"],
)
def test_w16_code_handing_a_module_on_depends_on_what_it_imports(repo, api, call):
    # W9 made the code handing a module on depend on its members; what the
    # module imports is as reachable (``read(api, ["ops", "other"])``).
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/ops.py": OPS_ADD_OTHER,
            "pkg/api.py": api,
            "pkg/reader.py": PATH_READER,
            "tests/test_r.py": (
                f"from pkg import api\nfrom pkg.reader import read\n\n\ndef test_r():\n    {call}\n"
            ),
            "benchmarks/bench_r.py": (
                f"from pkg import api\nfrom pkg.reader import read\n\n\ndef time_r():\n    {call}\n"
            ),
        }
    )
    head = repo.commit({"pkg/ops.py": OPS_ADD_OTHER.replace("return 1", "return 2")})
    targets = [
        py_target("t::test_r", "tests.test_r.test_r"),
        asv_target("bench.time_r", "benchmarks.bench_r.time_r"),
    ]
    plan = repo.plan(base, head, targets)
    assert selected(plan) == {"t::test_r", "bench.time_r"}
    assert rules(plan, "t::test_r") == {"dependency"}


BASE_WITH_NOOP = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}

# A test that imports nothing of the project: no rule but select-all reaches it.
CONTROL = "def test_c():\n    assert True\n"

T_C = py_target("tests/test_c.py::test_c", "tests.test_c.test_c")


def _plan_with_noop(repo, base, head, *targets, **kwargs):
    return repo.plan(base, head, [*targets, NOOP], source_roots=["."], **kwargs)


READ = "def read(m, n):\n    return getattr(m, n)()\n"

SUB = "def helper():\n    return 0\n\n\ndef other():\n    return {v}\n"

IMPORT_MODULE = "from importlib import import_module\nfrom pkg.rd import read\n\n\n"

HANDED = {
    "result-handed-on": (
        f"{IMPORT_MODULE}def run(name):\n    return read(import_module('pkg.sub'), name)\n"
    ),
    "local-variable": (
        f"{IMPORT_MODULE}def run(name):\n    m = import_module('pkg.sub')\n"
        "    return read(m, name)\n"
    ),
    "stored-in-a-dict": (
        f"{IMPORT_MODULE}MODS = {{'sub': import_module('pkg.sub')}}\n\n\n"
        "def run(name):\n    return read(MODS['sub'], name)\n"
    ),
    "returned-by-a-helper": (
        f"{IMPORT_MODULE}def _mod():\n    return import_module('pkg.sub')\n\n\n"
        "def run(name):\n    return read(_mod(), name)\n"
    ),
    "dunder-import-top": (
        "from pkg.rd import read\n\n\ndef run(name):\n    top = __import__('pkg.sub')\n"
        "    return read(top.sub, name)\n"
    ),
    "attribute-of-import_module": (
        "from importlib import import_module\nimport pkg.sub\nfrom pkg.rd import read\n\n\n"
        "def run(name):\n    return read(import_module('pkg').sub, name)\n"
    ),
    "importlib-module": (
        "import importlib\nfrom pkg.rd import read\n\n\ndef run(name):\n"
        "    return read(importlib.import_module('pkg.sub'), name)\n"
    ),
}

T_RUN = py_target("tests/test_run.py::test_run", "tests.test_run.test_run")

TEST_USE_RUN = "from pkg.use import run\n\n\ndef test_run():\n    assert run('other') == 1\n"


@pytest.mark.parametrize("use", list(HANDED.values()), ids=list(HANDED))
def test_r25_a_module_obtained_by_a_literal_name_and_handed_on_escapes(repo, use):
    """``read(import_module("pkg.sub"), name)`` reads any attribute of the
    module: a body change of ``other`` reaches the test, as it does when the
    module is handed on by its import name."""
    base = repo.commit(
        {
            **BASE_WITH_NOOP,
            "pkg/use.py": use,
            "pkg/rd.py": READ,
            "pkg/sub.py": SUB.format(v=1),
            "tests/test_run.py": TEST_USE_RUN,
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan_with_noop(repo, base, repo.commit({"pkg/sub.py": SUB.format(v=2)}), T_RUN, T_C)
    assert selected(plan) == {T_RUN.runner_id}


def test_r25_a_module_obtained_by_a_literal_name_and_only_read_does_not(repo):
    """``import_module("pkg.sub").helper()`` reads one name: ``other``
    changing reaches nothing (the precision the rule keeps)."""
    base = repo.commit(
        {
            **BASE_WITH_NOOP,
            "pkg/use.py": (
                "from importlib import import_module\n\n\ndef run(name):\n"
                "    return import_module('pkg.sub').helper()\n"
            ),
            "pkg/sub.py": SUB.format(v=1),
            "tests/test_run.py": TEST_USE_RUN.replace("== 1", "== 0"),
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan_with_noop(repo, base, repo.commit({"pkg/sub.py": SUB.format(v=2)}), T_RUN, T_C)
    assert selected(plan) == set()
    helper_changed = SUB.format(v=1).replace("return 0", "return 3")
    plan2 = _plan_with_noop(repo, base, repo.commit({"pkg/sub.py": helper_changed}), T_RUN, T_C)
    assert selected(plan2) == {T_RUN.runner_id}


# --------------------------------------------------------------------------- modules under aliases
# A module bound to another name (``m = ops``, module-level or local,
# ``m = pkg.ops`` after ``import pkg.ops``) and read through it
# (``m.value()``): the resolver reads an attribute off a name every binding
# of which is a module as off the module (a ``references`` edge; a name that
# may hold something else keeps the name-matched read beside it), and a
# module bound to any name other than its import's is a module a value of
# unknown type can be. Every scenario's change makes the test fail at the
# head commit, and an ASV target beside it is selected or not by the same
# rules.

ALIAS_OPS = "def value():\n    return 1\n\n\ndef other():\n    return 10\n"

ALIAS_OPS_VALUE = ALIAS_OPS.replace("return 1\n", "return 2\n")

ALIAS_BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": ALIAS_OPS,
    "tests/__init__.py": "",
    "tests/helpers.py": "from pkg import ops\n\nm = ops\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_ops.py": (
        "from pkg import ops\n\n\ndef time_value():\n    m = ops\n    m.value()\n"
    ),
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}

ALIAS_BENCH = asv_target("bench_ops.time_value", "benchmarks.bench_ops.time_value")

H = "from pkg import ops\n\n\n"

# Read off the module through a name the resolver follows: a real edge.
FOLLOWED = {
    "local": H + "def test_p():\n    m = ops\n    assert m.value() == 1\n",
    "module_level": H + "m = ops\n\n\ndef test_p():\n    assert m.value() == 1\n",
    "dotted_local": (
        "import pkg.ops\n\n\ndef test_p():\n    m = pkg.ops\n    assert m.value() == 1\n"
    ),
    "dotted_module_level": (
        "import pkg.ops\n\nm = pkg.ops\n\n\ndef test_p():\n    assert m.value() == 1\n"
    ),
    "import_as": "import pkg.ops as o\n\n\ndef test_p():\n    m = o\n    assert m.value() == 1\n",
    "package": (
        "import pkg\nimport pkg.ops\n\n\n"
        "def test_p():\n    m = pkg\n    assert m.ops.value() == 1\n"
    ),
    "bound_method": H + "def test_p():\n    m = ops\n    f = m.value\n    assert f() == 1\n",
    "alias_of_alias": H + "def test_p():\n    a = ops\n    b = a\n    assert b.value() == 1\n",
    "annotated": (
        "import types\n\n" + H + "def test_p():\n    m: types.ModuleType = ops\n"
        "    assert m.value() == 1\n"
    ),
    "walrus": H + "def test_p():\n    if (m := ops):\n        assert m.value() == 1\n",
    "unpacking": H + "def test_p():\n    m, n = ops, 1\n    assert m.value() == n\n",
    "for_literal": H + "def test_p():\n    for m in (ops,):\n        assert m.value() == 1\n",
    "global": (
        H + "def _bind():\n    global m\n    m = ops\n\n\n"
        "def test_p():\n    _bind()\n    assert m.value() == 1\n"
    ),
    "enclosing": (
        H + "def test_p():\n    m = ops\n\n    def inner():\n        return m.value()\n\n"
        "    assert inner() == 1\n"
    ),
}

# The name may hold something else too: the edge, and the name-matched read.
MIXED = {
    "rebound": (
        H + "def make():\n    return object()\n\n\n"
        "def test_p():\n    m = make()\n    m = ops\n    assert m.value() == 1\n"
    ),
    "global_and_none": (
        H + "m = None\n\n\ndef _bind():\n    global m\n    m = ops\n\n\n"
        "def test_p():\n    _bind()\n    assert m.value() == 1\n"
    ),
    "nonlocal": (
        H
        + "def test_p():\n    m = None\n\n    def bind():\n        nonlocal m\n        m = ops\n\n"
        "    bind()\n    assert m.value() == 1\n"
    ),
}

# Forms the resolver does not follow: the read is name-matched, and the
# module is one a value of unknown type can be.
MATCHED = {
    "other_module": (
        "from tests import helpers\n\n\ndef test_p():\n    assert helpers.m.value() == 1\n"
    ),
    "imported_alias": "from tests.helpers import m\n\n\ndef test_p():\n    assert m.value() == 1\n",
    "with_as": (
        "import contextlib\n\n" + H + "def test_p():\n"
        "    with contextlib.nullcontext(ops) as m:\n        assert m.value() == 1\n"
    ),
    "parameter": H
    + "def check(p):\n    assert p.value() == 1\n\n\ndef test_p():\n    check(ops)\n",
    "private_parameter": (
        H + "def _check(p):\n    assert p.value() == 1\n\n\ndef test_p():\n    _check(ops)\n"
    ),
    "returned": (
        H
        + "def get():\n    return ops\n\n\n"
        + "def test_p():\n    m = get()\n    assert m.value() == 1\n"
    ),
    "private_returned": (
        H
        + "def _get():\n    return ops\n\n\n"
        + "def test_p():\n    m = _get()\n    assert m.value() == 1\n"
    ),
    "comprehension": H + "def test_p():\n    assert [m.value() for m in (ops,)] == [1]\n",
    "getattr": (
        "import pkg\nimport pkg.ops\n\n\ndef test_p():\n    m = getattr(pkg, 'ops')\n"
        "    assert m.value() == 1\n"
    ),
    "class_body": H
    + "class Holder:\n    mod = ops\n\n\ndef test_p():\n    assert Holder.mod.value() == 1\n",
    "default": H + "def test_p(m=ops):\n    assert m.value() == 1\n",
    "lambda_default": H + "def test_p():\n    f = lambda m=ops: m.value()\n    assert f() == 1\n",
    "list": H + "def test_p():\n    mods = [ops]\n    assert mods[0].value() == 1\n",
}

T_P = py_target("tests/test_p.py::test_p", "tests.test_p.test_p")


def _alias_plan(repo, test: str, change: str):
    base = repo.commit({**ALIAS_BASE, "tests/test_p.py": test})
    head = repo.commit({"pkg/ops.py": change})
    return repo.plan(base, head, [T_P, ALIAS_BENCH, NOOP], source_roots=["."])


@pytest.mark.parametrize(
    "test",
    [*FOLLOWED.values(), *MIXED.values(), *MATCHED.values()],
    ids=[*FOLLOWED, *MIXED, *MATCHED],
)
def test_alias_a_module_read_through_another_name_is_selected(repo, test):
    """Before the fix, the ``MISSED`` forms and ``other_module`` selected
    nothing (the others selected the test through a module handed on)."""
    plan = _alias_plan(repo, test, ALIAS_OPS_VALUE)
    assert selected(plan) == {T_P.runner_id, ALIAS_BENCH.runner_id}


@pytest.mark.parametrize("test", list(FOLLOWED.values()), ids=list(FOLLOWED))
def test_alias_a_followed_name_is_a_dependency_edge(repo, test):
    plan = _alias_plan(repo, test, ALIAS_OPS_VALUE)
    assert rules(plan, T_P.runner_id) == {"dependency"}
    assert rules(plan, ALIAS_BENCH.runner_id) == {"dependency"}
    step = reason(plan, ALIAS_BENCH.runner_id).path[-1]
    assert (step.target, step.kind, step.detail) == ("pkg.ops.value", "references", "")


ALIAS_OPS_OTHER = ALIAS_OPS.replace("return 10\n", "return 11\n")

# The forms that missed: the use scan follows the name instead of counting
# the module as handed on (the other forms hand it on, so every member of
# it is a dependency of the reader).
MISSED = [
    "local",
    "module_level",
    "dotted_local",
    "dotted_module_level",
    "import_as",
    "package",
    "bound_method",
]


@pytest.mark.parametrize("form", MISSED)
def test_alias_another_function_of_the_module_selects_nothing(repo, form):
    plan = _alias_plan(repo, FOLLOWED[form], ALIAS_OPS_OTHER)
    assert selected(plan) == set()


def _index(test: str, **files: str):
    """The index of ALIAS_BASE with ``test`` as the test module (and ``files``,
    by module name, in place of ALIAS_BASE's: "" drops one), the same whether
    computed, stored in the module cache or loaded back."""
    sources = {k: v for k, v in ALIAS_BASE.items() if k.endswith(".py")}
    sources["tests/test_p.py"] = test
    for module, text in files.items():
        path = module.replace(".", "/") + ".py"
        if text:
            sources[path] = text
        else:
            sources.pop(path)
    snap = Snapshot(
        info=SnapshotInfo(revision="x", commit="x"),
        source_roots=["."],
        files={k: v.encode() for k, v in sources.items()},
    )
    result = build_index(snap)
    with tempfile.TemporaryDirectory() as tmp:
        cache = ModuleCache(Path(tmp))
        assert index_to_dict(build_index(snap, module_cache=cache)) == index_to_dict(result)
        assert index_to_dict(build_index(snap, module_cache=cache)) == index_to_dict(result)
    return result


def _reads(idx) -> tuple[set[str], set[str]]:
    """What ``tests.test_p.test_p`` references, and the attribute names it
    reads off values of unknown type."""
    source = "tests.test_p.test_p"
    targets = {e.target for e in idx.edges if e.source == source and e.kind == "references"}
    names = {
        u.name for u in idx.unresolved if u.symbol == source and u.kind == UnresolvedKind.ATTRIBUTE
    }
    return targets, names


@pytest.mark.parametrize("test", list(FOLLOWED.values()), ids=list(FOLLOWED))
def test_alias_every_binding_a_module_reads_off_the_module(test):
    targets, names = _reads(_index(test))
    assert "pkg.ops.value" in targets
    assert "value" not in names


@pytest.mark.parametrize("test", list(MIXED.values()), ids=list(MIXED))
def test_alias_a_name_that_may_hold_something_else_keeps_the_name_matched_read(test):
    targets, names = _reads(_index(test))
    assert "pkg.ops.value" in targets
    assert "value" in names


@pytest.mark.parametrize("form", ["local", "module_level"])
def test_alias_a_module_bound_to_a_name_is_one_an_unknown_value_can_be(form):
    plain = {
        "tests.helpers": "",
        "benchmarks.bench_ops": H + "def time_value():\n    ops.value()\n",
    }
    assert "pkg.ops" in _index(FOLLOWED[form], **plain).attribute_modules
    # Read through its import alone, it is not.
    direct = H + "def test_p():\n    assert ops.value() == 1\n"
    assert "pkg.ops" not in _index(direct, **plain).attribute_modules


# --------------------------------------------------------------------------- excluded files

# An import or a run of a ``.py`` file ``[index] exclude`` keeps out of the
# analysis is an unknown dependency: what the file runs is not read, so the
# importer is selected on any change (audit round 6, T2).
EXCLUDED_TREE = {
    "asv.conf.json": ASV_CONF,
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_mul.py": "from pkg.ops import mul\n\n\ndef test_mul():\n    mul(1, 2)\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_mul.py": "from pkg.ops import mul\n\n\ndef time_mul():\n    mul(1, 2)\n",
}
ADD_EDITED = {"pkg/ops.py": OPS.replace("a + b", "b + a")}
CALLS_ADD = "from pkg.ops import add\n\nRESULT = add(1, 2)\n"
RUNS = "import importlib\nimport runpy\nimport subprocess\nimport sys\n\n\n"
LOADS = "import importlib.util\n\n\n"
EXCLUDED_IMPORTS = {
    # A test helper the tests import, calling project code.
    "helper": (
        "tests/helpers/**",
        {
            "tests/helpers/__init__.py": "",
            "tests/helpers/util.py": CALLS_ADD,
            "tests/test_h.py": (
                "from tests.helpers.util import RESULT\n\n\ndef test_h():\n    assert RESULT == 3\n"
            ),
            "benchmarks/bench_h.py": (
                "from tests.helpers import util\n\n\ndef time_h():\n    util.RESULT\n"
            ),
        },
        "tests/helpers/util.py",
        {"tests/test_h.py::test_h", "bench_h.time_h"},
    ),
    # Project code importing an excluded re-export.
    "project": (
        "pkg/_vendor/*",
        {
            "pkg/_vendor/__init__.py": "",
            "pkg/_vendor/shim.py": "from pkg.ops import add as plus\n",
            "pkg/api.py": (
                "from pkg._vendor.shim import plus\n\n\ndef total():\n    return plus(1, 2)\n"
            ),
            "tests/test_h.py": (
                "from pkg.api import total\n\n\ndef test_h():\n    assert total() == 3\n"
            ),
            "benchmarks/bench_h.py": "from pkg.api import total\n\n\ndef time_h():\n    total()\n",
        },
        "pkg/_vendor/shim.py",
        {"tests/test_h.py::test_h", "bench_h.time_h"},
    ),
    "import_module": (
        "pkg/plugins/*",
        {
            "pkg/plugins/__init__.py": "",
            "pkg/plugins/p1.py": CALLS_ADD,
            "tests/test_h.py": RUNS + "def test_h():\n"
            "    assert importlib.import_module('pkg.plugins.p1').RESULT == 3\n",
        },
        "pkg/plugins/p1.py",
        {"tests/test_h.py::test_h"},
    ),
    "import_module_prefix": (
        "pkg/plugins/p*",
        {
            "pkg/plugins/__init__.py": "",
            "pkg/plugins/p1.py": CALLS_ADD,
            "tests/test_h.py": RUNS + "def test_h(n='1'):\n"
            "    assert importlib.import_module(f'pkg.plugins.p{n}').RESULT == 3\n",
        },
        "pkg/plugins/p1.py",
        {"tests/test_h.py::test_h"},
    ),
    "run_module": (
        "scripts/*",
        {
            "scripts/__init__.py": "",
            "scripts/tool.py": CALLS_ADD,
            "tests/test_h.py": RUNS + "def test_h():\n"
            "    assert runpy.run_module('scripts.tool')['RESULT'] == 3\n",
        },
        "scripts/tool.py",
        {"tests/test_h.py::test_h"},
    ),
    "script": (
        "tools/*",
        {
            "tools/tool.py": "import sys\n\nsys.path.insert(0, '.')\n" + CALLS_ADD,
            "tests/test_h.py": RUNS + "def test_h():\n"
            "    subprocess.run([sys.executable, 'tools/tool.py'], check=True)\n",
        },
        "tools/tool.py",
        {"tests/test_h.py::test_h"},
    ),
    "dash_m": (
        "tools/*",
        {
            "tools/__init__.py": "",
            "tools/tool.py": CALLS_ADD,
            "tests/test_h.py": RUNS + "def test_h():\n"
            "    subprocess.run([sys.executable, '-m', 'tools.tool'], check=True)\n",
        },
        "tools/tool.py",
        {"tests/test_h.py::test_h"},
    ),
    "file_loader": (
        "tools/*",
        {
            "tools/tool.py": CALLS_ADD,
            "tests/test_h.py": LOADS + "def test_h():\n"
            "    spec = importlib.util.spec_from_file_location('t', 'tools/tool.py')\n"
            "    spec.loader.exec_module(importlib.util.module_from_spec(spec))\n",
        },
        "tools/tool.py",
        {"tests/test_h.py::test_h"},
    ),
    "file_loader_prefix": (
        "tools/gen_*",
        {
            "tools/tool.py": "X = 1\n",
            "tools/gen_a.py": CALLS_ADD,
            "tests/test_h.py": LOADS + "def test_h(n='gen_a'):\n"
            "    spec = importlib.util.spec_from_file_location('t', f'tools/{n}.py')\n"
            "    spec.loader.exec_module(importlib.util.module_from_spec(spec))\n",
        },
        "tools/gen_a.py",
        {"tests/test_h.py::test_h"},
    ),
}


@pytest.mark.parametrize(
    ("pattern", "files", "path", "expected"),
    list(EXCLUDED_IMPORTS.values()),
    ids=list(EXCLUDED_IMPORTS),
)
def test_t2_an_import_of_an_excluded_file_is_an_unknown_dependency(
    repo, pattern, files, path, expected
):
    """The excluded file is imported or run and calls ``add``, which
    changes: its importers are selected, naming the file; code that reaches
    no excluded file stays bounded."""
    toml = f'[index]\nexclude = ["{pattern}"]\n'
    base = repo.commit({**EXCLUDED_TREE, **files, "diffcone.toml": toml})
    head = repo.commit(ADD_EDITED)
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert not plan.degraded
    assert selected(plan) == expected
    assert {"tests/test_mul.py::test_mul", "bench_mul.time_mul"} <= unselected(plan)
    for target in expected:
        assert rules(plan, target) == {"dynamic_reference"}
        detail = reason(plan, target, "dynamic_reference").detail
        assert path in detail and "[index] exclude" in detail


def test_t2_an_excluded_package_init_runs_for_every_import_under_it(repo):
    """``pkg/__init__.py`` runs before any ``pkg.*`` module: excluded, every
    importer of the package depends on anything."""
    base = repo.commit(
        {
            **EXCLUDED_TREE,
            "diffcone.toml": '[index]\nexclude = ["pkg/__init__.py"]\n',
            "pkg/__init__.py": "from pkg.ops import add as plus\n",
            "pkg/extra.py": "def three():\n    return 3\n",
            "tests/test_h.py": (
                "from pkg import plus\n\n\ndef test_h():\n    assert plus(1, 2) == 3\n"
            ),
        }
    )
    head = repo.commit({"pkg/extra.py": "def three():\n    return 4\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {
        "tests/test_h.py::test_h",
        "tests/test_mul.py::test_mul",
        "bench_mul.time_mul",
    }
    assert "pkg/__init__.py" in reason(plan, "bench_mul.time_mul", "dynamic_reference").detail


def test_t2_a_test_runner_given_an_excluded_test_file_depends_on_anything(repo):
    """``pytest.main(["tests/data/test_case.py"])`` runs an excluded file."""
    base = repo.commit(
        {
            **EXCLUDED_TREE,
            "diffcone.toml": '[index]\nexclude = ["tests/data/**"]\n',
            "tests/data/test_case.py": (
                "from pkg.ops import add\n\n\ndef test_case():\n    assert add(1, 2) == 3\n"
            ),
            "tests/test_h.py": (
                "import pytest\n\n\ndef test_h():\n"
                "    assert pytest.main(['tests/data/test_case.py']) == 0\n"
            ),
        }
    )
    head = repo.commit(ADD_EDITED)
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_h.py::test_h"}
    detail = reason(plan, "tests/test_h.py::test_h", "dynamic_reference").detail
    assert "tests/data/test_case.py" in detail and "[index] exclude" in detail


# --------------------------------------------------------------------------- imports by name

# Calls that import a module by a name in a string, as ``import_module``
# does (audit round 6, T9).
OPTIONAL = {
    "asv.conf.json": ASV_CONF,
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "pkg/optional.py": "X = 1\n",
    "tests/__init__.py": "",
    "tests/test_mul.py": "from pkg.ops import mul\n\n\ndef test_mul():\n    mul(1, 2)\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_mul.py": "from pkg.ops import mul\n\n\ndef time_mul():\n    mul(1, 2)\n",
}
BY_NAME = {
    "importorskip": (
        "import pytest\n\nopt = pytest.importorskip('pkg.optional')\n\n\ndef test_h():\n    pass\n"
    ),
    "importorskip_in_test": (
        "import pytest\n\n\ndef test_h():\n    pytest.importorskip('pkg.optional')\n"
    ),
    "importorskip_from": (
        "from pytest import importorskip\n\n\ndef test_h():\n    importorskip('pkg.optional')\n"
    ),
    "find_spec": LOADS + "def test_h():\n"
    "    spec = importlib.util.find_spec('pkg.optional')\n"
    "    spec.loader.exec_module(importlib.util.module_from_spec(spec))\n",
    "resolve_name": "import pkgutil\n\n\ndef test_h():\n"
    "    assert pkgutil.resolve_name('pkg.optional:X') == 1\n",
    "resolve_name_dotted": "import pkgutil\n\n\ndef test_h():\n"
    "    assert pkgutil.resolve_name('pkg.optional.X') == 1\n",
    "locate": "import pydoc\n\n\ndef test_h():\n    assert pydoc.locate('pkg.optional.X') == 1\n",
    "walk_packages": "import pkgutil\n\nimport pkg\n\n\ndef test_h():\n"
    "    for info in pkgutil.walk_packages(pkg.__path__, 'pkg.'):\n        pass\n",
}


@pytest.mark.parametrize("test", list(BY_NAME.values()), ids=list(BY_NAME))
def test_t9_a_module_imported_by_name_fails_to_import(repo, test):
    """The module raises on import now: the test that imports it by name
    skips or fails, so it is selected; so is the benchmark importing it."""
    bench = "import pytest\n\n\ndef time_h():\n    pytest.importorskip('pkg.optional')\n"
    base = repo.commit({**OPTIONAL, "tests/test_h.py": test, "benchmarks/bench_h.py": bench})
    head = repo.commit({"pkg/optional.py": "raise ImportError('gone')\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_h.py::test_h", "bench_h.time_h"}
    assert rules(plan, "tests/test_h.py::test_h") == {"dependency"}


def test_t9_importorskip_runs_the_module_s_import_time_writes(repo):
    """What the module does on import (an ``os.environ`` write) is what the
    test reads; the module body changes."""
    optional = "import os\n\nos.environ['OPT'] = '1'\n"
    test = "import os\n\nimport pytest\n\npytest.importorskip('pkg.optional')\n\n\n"
    test += "def test_h():\n    assert os.environ.get('OPT') == '1'\n"
    base = repo.commit({**OPTIONAL, "pkg/optional.py": optional, "tests/test_h.py": test})
    head = repo.commit({"pkg/optional.py": optional.replace("'1'", "'2'")})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert "tests/test_h.py::test_h" in selected(plan)
    assert "dependency" in rules(plan, "tests/test_h.py::test_h")


def test_t9_find_spec_depends_on_whether_the_module_exists(repo):
    """``find_spec`` finds the module's file: deleting the module changes
    what it returns."""
    test = LOADS + "def test_h():\n    assert importlib.util.find_spec('pkg.optional')\n"
    base = repo.commit({**OPTIONAL, "tests/test_h.py": test})
    head = repo.commit({"pkg/optional.py": None})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_h.py::test_h"}


def test_t9_iter_modules_of_a_package_sees_a_module_added_to_it(repo):
    """``pkgutil.iter_modules(pkg.__path__)`` lists the package's modules:
    a new one changes the listing; a file outside the package does not."""
    test = "import pkgutil\n\nimport pkg\n\n\ndef test_h():\n"
    test += "    assert len(list(pkgutil.iter_modules(pkg.__path__))) == 2\n"
    base = repo.commit({**OPTIONAL, "tests/test_h.py": test})
    head = repo.commit({"pkg/extra.py": "Y = 1\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_h.py::test_h"}
    other = repo.commit({"tests/helpers.py": "Y = 1\n"})
    assert selected(repo.plan(head, other, [], discover_runners=["pytest", "asv"])) == set()


def test_t9_a_spec_found_by_a_computed_name_and_loaded_runs_anything(repo):
    """``find_spec(name)`` then ``exec_module``: any module may run, so a
    change outside the helper's imports reaches it. Without the load it only
    looks the module up, bounded by the helper's imports like a ``getattr``
    of a computed name; ``bench_find`` uses that one."""
    loader = (
        "import importlib.util\n\n\ndef load(prefix):\n"
        "    spec = importlib.util.find_spec(prefix + '.optional')\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(module)\n    return module\n\n\n"
        "def where(prefix):\n    return importlib.util.find_spec(prefix + '.optional').origin\n"
    )
    base = repo.commit(
        {
            **OPTIONAL,
            "pkg/loader.py": loader,
            "tests/test_h.py": (
                "from pkg.loader import load\n\n\ndef test_h():\n    assert load('pkg').X == 1\n"
            ),
            "benchmarks/bench_find.py": (
                "from pkg.loader import where\n\n\ndef time_find():\n    where('pkg')\n"
            ),
        }
    )
    head = repo.commit({"pkg/optional.py": "X = 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    assert selected(plan) == {"tests/test_h.py::test_h"}
    assert rules(plan, "tests/test_h.py::test_h") == {"dynamic_reference"}
    assert "bench_find.time_find" in unselected(plan)

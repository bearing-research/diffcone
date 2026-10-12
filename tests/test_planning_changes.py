"""How a change is classified and what it reaches.

Docstrings (formatted by a decorator, read through ``__doc__``, a parameter
or ``inspect.getdoc``), decorators that may call what they decorate,
annotations and type parameters that run project code, module statements
reordered or a function name rebound, files the index does not read
(runner configuration, lock files, build and startup scripts), and the
manifest and ``diffcone.toml`` themselves: each either reaches exactly the
targets that may notice it or selects everything with the rule that says
why.

Scenarios mix pytest and ASV targets and assert exact target sets.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.indexer import build_index
from diffcone.manifest import ManifestError, Target, parse_manifest
from diffcone.model import SnapshotInfo
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

# --------------------------------------------------------------------------- docstrings

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'

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


@pytest.mark.parametrize(
    "old, new",
    [
        ("Reduce a {klass}.", "Reduce a {klass} along {axis}."),  # a decorator reads it
        ('"""Hello."""', '"""Hi."""'),  # a test reads ``greet.__doc__``
    ],
)
def test_w8_a_docstring_seed_is_explained_by_its_steps(repo, old, new):
    base = repo.commit(DOCS)
    head = repo.commit({"pkg/a.py": DOCS["pkg/a.py"].replace(old, new)})
    plan = repo.plan(base, head, [], discover_runners=["pytest", "asv"])
    reasons = [r for d in plan.decisions if d.selected for r in d.reasons]
    assert reasons
    assert not any("dynamic import/attribute access ()" in r.detail for r in reasons)
    assert all(r.rule == "dependency" and r.changed_symbol for r in reasons)


def test_f1_a_decorator_that_never_reads_docstrings_keeps_doc_edits_free(repo):
    deco = (
        "def set_module(name):\n    def wrap(f):\n        f.__module__ = name\n"
        "        return f\n    return wrap\n"
    )
    a = (
        "from pkg.deco import set_module\n\n\n@set_module('pkg')\ndef f():\n"
        '    """{doc}"""\n    return 1\n'
    )
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/deco.py": deco,
            "pkg/a.py": a.format(doc="Old."),
            "tests/__init__.py": "",
            "tests/test_a.py": "from pkg.a import f\n\n\ndef test_f():\n    f()\n",
        }
    )
    head = repo.commit({"pkg/a.py": a.format(doc="New, longer.")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == set()


def test_f1_a_decorator_the_analysis_cannot_see_may_read_it(repo):
    a = 'import click\n\n\n@click.command()\ndef cli():\n    """{doc}"""\n    return 1\n'
    base = repo.commit(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": a.format(doc="Old."),
            "tests/__init__.py": "",
            "tests/test_a.py": "from pkg.a import cli\n\n\ndef test_help():\n    cli\n",
        }
    )
    head = repo.commit({"pkg/a.py": a.format(doc="New help text.")})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_help"}


BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
}


def _plan_discovered(repo, base, head, targets=(), evidence=None, **kwargs):
    return repo.plan(
        base, head, list(targets), discover_runners=["pytest"], evidence=evidence, **kwargs
    )


DOC_MOD = "def f():\n    '''{0}'''\n"


@pytest.mark.parametrize(
    "helper",
    [
        "import inspect\n\n\ndef doc_of(fn):\n    return inspect.getdoc(fn)\n",
        "def doc_of(obj):\n    return obj.__doc__\n",
        "def doc_of(obj):\n    return getattr(obj, '__doc__')\n",
    ],
    ids=["getdoc", "attribute", "getattr"],
)
def test_r7_a_docstring_read_through_a_parameter(repo, helper):
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": DOC_MOD.format("hello"),
            "pkg/help.py": helper,
            "tests/test_a.py": (
                "from pkg.help import doc_of\n"
                "from pkg.mod import f\n"
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
    head = repo.commit({"pkg/mod.py": DOC_MOD.format("goodbye")})
    plan = _plan_discovered(repo, base, head)
    assert changes(plan) == {"pkg.mod.f": ("docstring_changed",)}
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert any(
        step.kind == "reads_docstring" for step in reason(plan, "tests/test_a.py::test_a").path
    )


@pytest.mark.parametrize(
    "test",
    [
        "import pkg.mod\n\n\ndef test_a():\n    assert pkg.mod.__doc__ == 'hello'\n",
        "from pkg import mod\n\n\ndef test_a():\n    assert mod.__doc__ == 'hello'\n",
    ],
    ids=["dotted", "from_import"],
)
def test_r7_a_module_docstring_read_as_a_module_attribute(repo, test):
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": "'''hello'''\n\n\ndef f():\n    return 1\n",
            "tests/test_a.py": test,
            "tests/test_other.py": (
                "from pkg.mod import f\n\n\ndef test_other():\n    assert f() == 1\n"
            ),
        }
    )
    head = repo.commit({"pkg/mod.py": "'''goodbye'''\n\n\ndef f():\n    return 1\n"})
    plan = _plan_discovered(repo, base, head)
    assert changes(plan) == {"pkg.mod": ("docstring_changed",)}
    assert selected(plan) == {"tests/test_a.py::test_a"}


def test_r7_a_decorator_reading_its_parameter_is_no_reader_of_every_docstring(repo):
    """Guard (precision): ``@doc`` reads ``f.__doc__`` of what it decorates,
    which ``doc_decorated`` covers. The docstring of an undecorated
    function elsewhere changing reaches no test through the decorator."""
    base = repo.commit(
        {
            **BASE,
            "pkg/deco.py": (
                "DOCS = {}\n\n\ndef doc(f):\n    DOCS[f.__name__] = f.__doc__\n    return f\n"
            ),
            "pkg/mod.py": "from pkg.deco import doc\n\n\n@doc\ndef g():\n    '''g'''\n",
            "pkg/other.py": "def h():\n    '''one'''\n",
            "tests/test_doc.py": (
                "from pkg.deco import DOCS\n"
                "import pkg.mod\n"
                "\n"
                "\n"
                "def test_doc():\n"
                "    assert DOCS['g'] == 'g'\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "def h():\n    '''two'''\n"})
    plan = _plan_discovered(repo, base, head)
    assert changes(plan) == {"pkg.other.h": ("docstring_changed",)}
    assert selected(plan) == set()
    # And the decorated function's own docstring still reaches the reader.
    head2 = repo.commit(
        {"pkg/mod.py": "from pkg.deco import doc\n\n\n@doc\ndef g():\n    '''changed'''\n"}
    )
    assert selected(_plan_discovered(repo, base, head2)) == {"tests/test_doc.py::test_doc"}


# --------------------------------------------------------------------------- calling decorators

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


DECO_CALLS = "def final(f):\n    f()\n    return f\n"

TEST_ENV_Z = "import os\nimport pkg.m\n\n\ndef test_env():\n    assert os.environ['DC_Z'] == '1'\n"


def test_q13_decorator_calls_the_function_at_import(repo):
    m = (
        "import os\nfrom pkg.deco import final\n\n\n"
        "@final\ndef helper():\n    os.environ['DC_Z'] = '{v}'\n"
    )
    plan = _plan_test_ids(
        repo,
        {"pkg/deco.py": DECO_CALLS, "pkg/m.py": m.format(v=1), "tests/test_m.py": TEST_ENV_Z},
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_env",
    )
    assert selected(plan) == {"tests/test_m.py::test_env"}


def test_q13_decorator_calls_a_method_while_the_class_body_runs(repo):
    deco = "def run_now(f):\n    f(None)\n    return f\n"
    m = (
        "import os\nfrom pkg.deco import run_now\n\n\n"
        "class C:\n    @run_now\n    def setup(self):\n        os.environ['DC_Z'] = '{v}'\n"
    )
    plan = _plan_test_ids(
        repo,
        {"pkg/deco.py": deco, "pkg/m.py": m.format(v=1), "tests/test_m.py": TEST_ENV_Z},
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_env",
    )
    assert selected(plan) == {"tests/test_m.py::test_env"}


ENV_B = 'import os\nos.environ["DC_X"] = "{v}"\n'


def test_q13_decorator_calls_the_function_later_with_its_own_arguments(repo):
    # ``load("pkg.a")`` calls the wrapper, which imports ``pkg.b``: the call
    # sites do not bound what ``load`` is called with.
    deco = (
        "def with_b(f):\n    def wrapper(*args):\n        return f('pkg.b')\n    return wrapper\n"
    )
    m = (
        "import importlib\nfrom pkg.deco import with_b\n\n\n"
        "@with_b\ndef load(name):\n    return importlib.import_module(name)\n"
    )
    test = (
        "import os\nfrom pkg.m import load\n\n\n"
        "def test_load():\n    load('pkg.a')\n    assert os.environ['DC_X'] == '1'\n"
    )
    plan = _plan_test_ids(
        repo,
        {
            "pkg/deco.py": deco,
            "pkg/m.py": m,
            "pkg/a.py": "",
            "pkg/b.py": ENV_B.format(v=1),
            "tests/test_m.py": test,
        },
        {"pkg/b.py": ENV_B.format(v=2)},
        "tests/test_m.py::test_load",
    )
    assert selected(plan) == {"tests/test_m.py::test_load"}


def test_q13_quiet_decorators_add_nothing(repo):
    # Not a miss: decorators known to call nothing of what they decorate
    # leave a body change to the readers of the function, as before.
    m = (
        "import contextlib\nimport dataclasses\nimport functools\n\n\n"
        "@dataclasses.dataclass\nclass C:\n    a: int = 0\n\n"
        "    @property\n    def x(self):\n        return {v}\n\n"
        "    @functools.cache\n    def y(self):\n        return {v}\n\n"
        "    @staticmethod\n    def z():\n        return {v}\n\n\n"
        "@contextlib.contextmanager\ndef ctx():\n    yield {v}\n\n\n"
        "def unrelated():\n    return 0\n"
    )
    test = "from pkg.m import unrelated\n\n\ndef test_other():\n    assert unrelated() == 0\n"
    plan = _plan_test_ids(
        repo,
        {"pkg/m.py": m.format(v=1), "tests/test_m.py": test},
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_other",
    )
    assert selected(plan) == set()


PASSIVE_DECOS = (
    "import functools\n\n\n"
    "def set_module(module):\n"
    "    def decorator(func):\n"
    "        if module is not None:\n"
    "            func.__module__ = module\n"
    "        return func\n"
    "    return decorator\n\n\n"
    "def logged(func):\n"
    "    @functools.wraps(func)\n"
    "    def wrapper(*args, **kwargs):\n"
    "        return func(*args, **kwargs)\n"
    "    return wrapper\n"
)


def test_q13_passive_project_decorators_add_nothing(repo):
    # Not a miss: a body change reaches the readers of the function only.
    m = (
        "from pkg.deco import logged, set_module\n\n\n"
        "@set_module('pkg')\ndef api():\n    return {v}\n\n\n"
        "@logged\ndef traced():\n    return {v}\n\n\n"
        "def unrelated():\n    return 0\n"
    )
    other = "from pkg.m import unrelated\n\n\ndef test_other():\n    assert unrelated() == 0\n"
    user = "from pkg.m import api\n\n\ndef test_api():\n    assert api() == 1\n"
    plan = _plan_test_ids(
        repo,
        {
            "pkg/deco.py": PASSIVE_DECOS,
            "pkg/m.py": m.format(v=1),
            "tests/test_m.py": other,
            "tests/test_api.py": user,
        },
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_other",
        "tests/test_api.py::test_api",
    )
    assert selected(plan) == {"tests/test_api.py::test_api"}


def test_q13_project_decorators_that_may_call_what_they_decorate(repo):
    # Each decorator here may run ``helper`` while ``pkg.m`` is imported: it
    # hands it on, its returned decorator calls it, or it calls it through a
    # name of its own.
    decos = (
        "def run(f):\n    f()\n\n\n"
        "def hand_on(f):\n    run(f)\n    return f\n\n\n"
        "def factory():\n    def decorator(f):\n        f()\n        return f\n"
        "    return decorator\n\n\n"
        "def alias(f):\n    g = f\n    g()\n    return f\n\n\n"
        "def inner(f):\n    def wrapper():\n        return f()\n    wrapper()\n    return wrapper\n"
    )
    for i, decorator in enumerate(["hand_on", "factory()", "alias", "inner"]):
        m = (
            "import os\nfrom pkg import deco\n\n\n"
            f"@deco.{decorator}\ndef helper():\n    os.environ['DC_Z'] = '{{v}}'\n"
        )
        sub = FixtureRepo(repo.path.parent / f"r{i}", check_cache=True)
        plan = _plan_test_ids(
            sub,
            {"pkg/deco.py": decos, "pkg/m.py": m.format(v=1), "tests/test_m.py": TEST_ENV_Z},
            {"pkg/m.py": m.format(v=2)},
            "tests/test_m.py::test_env",
        )
        assert selected(plan) == {"tests/test_m.py::test_env"}, decorator


# --------------------------------------------------------------------------- annotations


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


REGISTRY = (
    "import os\n\n\nclass Registry:\n    def __class_getitem__(cls, key):\n"
    "        os.environ['DC_Q'] = key\n        return cls\n"
)

TEST_Q = "import os\nimport pkg.m\n\n\ndef test_q():\n    assert os.environ['DC_Q'] == '1'\n"


def test_q22_annotation_of_a_module_variable_runs_at_import(repo):
    m = "from pkg.reg import Registry\n\nX: Registry['1'] = 1\n"
    plan = _plan_test_ids(
        repo,
        {"pkg/reg.py": REGISTRY, "pkg/m.py": m, "tests/test_m.py": TEST_Q},
        {"pkg/reg.py": REGISTRY.replace("= key", "= key + '!'")},
        "tests/test_m.py::test_q",
    )
    assert selected(plan) == {"tests/test_m.py::test_q"}


def test_q22_added_variable_whose_annotation_runs_project_code(repo):
    m = "from pkg.reg import Registry\n\nX: Registry['1'] = 1\n"
    plan = _plan_test_ids(
        repo,
        {"pkg/reg.py": REGISTRY, "pkg/m.py": m, "tests/test_m.py": TEST_Q},
        {"pkg/m.py": m + "Y: Registry['2'] = 2\n"},
        "tests/test_m.py::test_q",
    )
    assert selected(plan) == {"tests/test_m.py::test_q"}


def test_q22_which_headers_and_annotations_run_no_project_code():
    m = (
        "import typing\nfrom typing import Optional\n\nfrom pkg import reg\n"
        "from pkg.reg import Registry\n\n\n"
        "def builtin(x: list[int]) -> dict[str, int]:\n    pass\n\n\n"
        "def typed(x: Optional[int], y: typing.Literal['a']):\n    pass\n\n\n"
        "def subscript(x: Registry['k']):\n    pass\n\n\n"
        "def module_attribute(x: reg.Thing):\n    pass\n\n\n"
        "A: list[int] = []\nB: Registry['k'] = 1\nC: int = 1\n"
    )
    snapshot = Snapshot(
        info=SnapshotInfo(revision="x", commit="x"),
        source_roots=["."],
        files={
            k: v.encode()
            for k, v in {"pkg/__init__.py": "", "pkg/reg.py": REGISTRY, "pkg/m.py": m}.items()
        },
    )
    symbols = build_index(snapshot).symbols
    quiet = {n for n in ("builtin", "typed", "subscript", "module_attribute")
             if symbols[f"pkg.m.{n}"].quiet_header}  # fmt: skip
    assert quiet == {"builtin", "typed"}
    inert = {n for n in "ABC" if symbols[f"pkg.m.{n}"].inert_definition}
    assert inert == {"A", "C"}


# --------------------------------------------------------------------------- type parameters

# A small project: one test and one benchmark on separate code, so a plan
# that selects both selected everything.
TREE = {
    "src/pkg/__init__.py": "",
    "src/pkg/ops.py": "def add(a, b):\n    return a + b\n",
    "src/pkg/other.py": "def helper():\n    return 1\n",
    "tests/test_ops.py": (
        "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    ),
    "benchmarks/bench_other.py": (
        "from pkg.other import helper\n\n\ndef time_helper():\n    helper()\n"
    ),
}

TARGETS = [
    py_target("tests/test_ops.py::test_add", "tests.test_ops.test_add"),
    asv_target("bench_other.time_helper", "benchmarks.bench_other.time_helper"),
]

# Prefixed, so modules keep the names they have under the root ".".
SPLIT_ROOTS = ["src", "tests=tests", "benchmarks=benchmarks"]

# PEP 696 defaults parse from Python 3.13.
needs_defaults = pytest.mark.skipif(
    sys.version_info < (3, 13), reason="type parameter defaults need Python 3.13"
)

TYPE_PARAM_CASES = [
    (
        "def entry[T: int]():\n    return entry.__type_params__[0].__bound__\n",
        "def entry[T: str]():\n    return entry.__type_params__[0].__bound__\n",
    ),
    pytest.param(
        "def entry[T = int]():\n    return entry.__type_params__[0].__default__\n",
        "def entry[T = str]():\n    return entry.__type_params__[0].__default__\n",
        marks=needs_defaults,
    ),
    (
        "def entry[T: (int, str)]():\n    return entry.__type_params__[0].__constraints__\n",
        "def entry[T: (int, bytes)]():\n    return entry.__type_params__[0].__constraints__\n",
    ),
    (
        "def entry[T]():\n    return len(entry.__type_params__)\n",
        "def entry[T, U]():\n    return len(entry.__type_params__)\n",
    ),
    (
        "class C[T: int]:\n    pass\n\n\ndef entry():\n    return C.__type_params__[0].__bound__\n",
        "class C[T: str]:\n    pass\n\n\ndef entry():\n    return C.__type_params__[0].__bound__\n",
    ),
    (
        "class C[T]:\n    pass\n\n\ndef entry():\n    return len(C.__type_params__)\n",
        "class C[T, U]:\n    pass\n\n\ndef entry():\n    return len(C.__type_params__)\n",
    ),
    (
        "type A[T: int] = list[T]\n\n\ndef entry():\n    return A.__type_params__[0].__bound__\n",
        "type A[T: str] = list[T]\n\n\ndef entry():\n    return A.__type_params__[0].__bound__\n",
    ),
]


@pytest.mark.skipif(
    not hasattr(__import__("ast"), "TypeAlias"), reason="PEP 695 syntax needs Python 3.12"
)
@pytest.mark.parametrize("before, after", TYPE_PARAM_CASES)
def test_idx4_type_parameter_change_is_a_change(repo, before, after):
    base = repo.commit(
        {
            **TREE,
            "src/pkg/core.py": before,
            "tests/test_core.py": (
                "from pkg.core import entry\n\n\ndef test_core():\n    assert entry()\n"
            ),
        }
    )
    head = repo.commit({"src/pkg/core.py": after})
    targets = [*TARGETS, py_target("tests/test_core.py::test_core", "tests.test_core.test_core")]
    plan = repo.plan(base, head, targets, source_roots=SPLIT_ROOTS)
    assert changes(plan), "no changed symbol"
    assert selected(plan) == {"tests/test_core.py::test_core"}


@pytest.mark.skipif(
    not hasattr(__import__("ast"), "TypeAlias"), reason="PEP 695 syntax needs Python 3.12"
)
@pytest.mark.parametrize(
    "core",
    [
        "from pkg.make import make\n\n\ndef entry[T: make]():\n"
        "    return entry.__type_params__[0].__bound__()\n",
        pytest.param(
            "from pkg.make import make\n\n\ndef entry[T = make]():\n"
            "    return entry.__type_params__[0].__default__()\n",
            marks=needs_defaults,
        ),
        "from pkg.make import make\n\n\nclass C[T: make]:\n    pass\n\n\n"
        # (``C.__type_params__`` alone is no reference to C: an attribute
        # nothing defines, read off a class, is only a name match.)
        "def entry():\n    cls = C\n    return cls.__type_params__[0].__bound__()\n",
    ],
)
def test_idx4_type_parameter_bound_is_a_dependency(repo, core):
    """A bound or default naming project code depends on it, though Python
    evaluates it only when ``__bound__``/``__default__`` is read."""
    base = repo.commit(
        {
            **TREE,
            "src/pkg/make.py": "def make():\n    return 1\n",
            "src/pkg/core.py": core,
            "tests/test_core.py": (
                "from pkg.core import entry\n\n\ndef test_core():\n    assert entry()\n"
            ),
        }
    )
    head = repo.commit({"src/pkg/make.py": "def make():\n    return 2\n"})
    targets = [*TARGETS, py_target("tests/test_core.py::test_core", "tests.test_core.test_core")]
    plan = repo.plan(base, head, targets, source_roots=SPLIT_ROOTS)
    assert selected(plan) == {"tests/test_core.py::test_core"}
    assert "pkg.make.make" in path_ids(reason(plan, "tests/test_core.py::test_core"))


# --------------------------------------------------------------------------- statement order

BENCH_B = asv_target("bench.time_b", "benchmarks.bench.time_b", "benchmarks.bench")

RUNNERS_B = {
    "tests/test_m.py": "from pkg.m import b\n\n\ndef test_b():\n    assert b() == 2\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.m import b\n\n\ndef time_b():\n    b()\n",
}


def test_r6_reordering_module_statements_is_a_definition_change(repo):
    """``A = 1; A = 2; B = A`` -> ``A = 1; B = A; A = 2``: ``B`` now holds
    1. No hash changed; the module's statement order did."""
    base = repo.commit(
        {**BASE, **RUNNERS_B, "pkg/m.py": "A = 1\nA = 2\nB = A\n\n\ndef b():\n    return B\n"}
    )
    head = repo.commit({"pkg/m.py": "A = 1\nB = A\nA = 2\n\n\ndef b():\n    return B\n"})
    plan = _plan_discovered(repo, base, head, [BENCH_B])
    assert changes(plan) == {"pkg.m": ("definition_changed",)}
    assert selected(plan) == {"tests/test_m.py::test_b", "bench.time_b"}
    assert rules(plan, "bench.time_b") == {"dependency"}


def test_r6_variable_moved_below_the_decorator_reading_it(repo):
    """``X = 1`` moved below ``@deco(X) def f``: the decorator sees no
    ``X`` any more (NameError at import)."""
    deco = "def deco(x):\n    def wrap(f):\n        f.x = x\n        return f\n    return wrap\n"
    base = repo.commit(
        {
            **BASE,
            **RUNNERS_B,
            "pkg/d.py": deco,
            "pkg/m.py": (
                "from pkg.d import deco\n\nX = 1\n\n\n@deco(X)\ndef b():\n    return b.x + 1\n"
            ),
        }
    )
    head = repo.commit(
        {
            "pkg/m.py": (
                "from pkg.d import deco\n\n\n@deco(X)\ndef b():\n    return b.x + 1\n\n\nX = 1\n"
            )
        }
    )
    plan = _plan_discovered(repo, base, head, [BENCH_B])
    assert changes(plan) == {"pkg.m": ("definition_changed",)}
    assert selected(plan) == {"tests/test_m.py::test_b", "bench.time_b"}


def test_r6_def_moved_below_a_fallback_import(repo):
    """``def f`` moved below ``try: from pkg.fast import f except
    ImportError: pass``: the import now wins over the local definition."""
    fast = "def b():\n    return 1\n"
    local = "def b():\n    return 2\n"
    fallback = "try:\n    from pkg.fast import b\nexcept ImportError:\n    pass\n"
    base = repo.commit(
        {**BASE, **RUNNERS_B, "pkg/fast.py": fast, "pkg/m.py": fallback + "\n\n" + local}
    )
    head = repo.commit({"pkg/m.py": local + "\n\n" + fallback})
    plan = _plan_discovered(repo, base, head, [BENCH_B])
    assert "definition_changed" in changes(plan)["pkg.m"]
    assert selected(plan) == {"tests/test_m.py::test_b", "bench.time_b"}


def test_r6_two_registering_defs_swapped(repo):
    """Two ``@register`` defs trade places: the registry's order, read by
    ``first()``, changes. Both defs run code at import, so their order is
    not free."""
    reg = (
        "ORDER = []\n"
        "\n"
        "\n"
        "def register(f):\n"
        "    ORDER.append(f.__name__)\n"
        "    return f\n"
        "\n"
        "\n"
        "def first():\n"
        "    return ORDER[0]\n"
    )
    one = "@register\ndef one():\n    return 1\n"
    two = "@register\ndef two():\n    return 2\n"
    runners = {
        "tests/test_r.py": (
            "from pkg.reg import first\n\n\ndef test_first():\n    assert first() == 'one'\n"
        ),
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": "from pkg.reg import first\n\n\ndef time_first():\n    first()\n",
    }
    base = repo.commit(
        {
            **BASE,
            **runners,
            "pkg/reg.py": reg,
            "pkg/m.py": f"from pkg.reg import register\n\n\n{one}\n\n{two}",
            "tests/test_m.py": "import pkg.m\n\n\ndef test_m():\n    pass\n",
        }
    )
    head = repo.commit({"pkg/m.py": f"from pkg.reg import register\n\n\n{two}\n\n{one}"})
    bench = asv_target("bench.time_first", "benchmarks.bench.time_first", "benchmarks.bench")
    plan = _plan_discovered(repo, base, head, [bench])
    assert changes(plan) == {"pkg.m": ("definition_changed",)}
    # test_m imports pkg.m: its import-time code changed. test_first and the
    # benchmark read ORDER, mutated by register, which pkg.m's import runs.
    assert selected(plan) == {
        "tests/test_m.py::test_m",
        "tests/test_r.py::test_first",
        "bench.time_first",
    }


def test_r6_class_statement_moved_across_a_method(repo):
    """``m = staticmethod(m)`` moved above ``def m`` in the class body:
    NameError at import. The class body hash strips defs, its definition
    hash holds the sorted member names: neither saw it."""
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": "class A:\n    def m(x):\n        return x\n\n    m = staticmethod(m)\n",
            "tests/test_a.py": (
                "from pkg.mod import A\n\n\ndef test_a():\n    assert A().m(1) == 1\n"
            ),
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    head = repo.commit(
        {"pkg/mod.py": "class A:\n    m = staticmethod(m)\n\n    def m(x):\n        return x\n"}
    )
    plan = _plan_discovered(repo, base, head)
    assert changes(plan) == {"pkg.mod.A": ("definition_changed",)}
    assert selected(plan) == {"tests/test_a.py::test_a"}


def test_r6_appending_and_swapping_inert_defs_is_no_reorder(repo):
    """Guard (precision): a function appended at the end of a module is its
    own ``added`` change; two plain defs trading places, and a new def
    inserted between two statements, change nothing that runs."""
    body = "A = 1\n\n\ndef f():\n    return A\n\n\ndef g():\n    return 2\n\n\nB = f()\n"
    base = repo.commit(
        {
            **BASE,
            "pkg/m.py": body,
            "tests/test_m.py": "from pkg.m import B\n\n\ndef test_b():\n    assert B == 1\n",
        }
    )
    appended = repo.commit({"pkg/m.py": body + "\n\ndef h():\n    return 3\n"})
    plan = _plan_discovered(repo, base, appended)
    assert changes(plan) == {"pkg.m.h": ("added",)}
    assert selected(plan) == set()
    swapped = repo.commit(
        {"pkg/m.py": "A = 1\n\n\ndef g():\n    return 2\n\n\ndef f():\n    return A\n\n\nB = f()\n"}
    )
    plan = _plan_discovered(repo, base, swapped)
    assert changes(plan) == {}
    inserted = repo.commit(
        {
            "pkg/m.py": (
                "A = 1\n"
                "\n"
                "\n"
                "def f():\n"
                "    return A\n"
                "\n"
                "\n"
                "def new():\n"
                "    return 0\n"
                "\n"
                "\n"
                "def g():\n"
                "    return 2\n"
                "\n"
                "\n"
                "B = f()\n"
            )
        }
    )
    plan = _plan_discovered(repo, base, inserted)
    assert changes(plan) == {"pkg.m.new": ("added",)}


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


# --------------------------------------------------------------------------- unanalysed files


@pytest.mark.parametrize(
    "path, before, after",
    [
        (
            "pyproject.toml",
            "[tool.pytest.ini_options]\naddopts = ''\n",
            "[tool.pytest.ini_options]\naddopts = '-W error'\n",
        ),
        ("conftest.py", "X = 1\n", "X = 2\n"),
        ("pytest.toml", "[pytest]\n", "[pytest]\naddopts = ['-W', 'error']\n"),
        # What is installed: a lock file, a workspace member's dependencies,
        # a requirements file, a compiled source.
        ("uv.lock", "version = 1\n", "version = 2\n"),
        ("pylock.toml", "lock-version = '1.0'\n", "lock-version = '1.1'\n"),
        ("packages/client/pyproject.toml", "[project]\n", "[project]\ndependencies = ['x']\n"),
        ("requirements/dev.txt", "x==1\n", "x==2\n"),
        ("environment.yml", "name: a\n", "name: b\n"),
        ("csrc/ext.c", "int f;\n", "int g;\n"),
    ],
)
def test_d24_runner_configuration_outside_the_roots_selects_everything(repo, path, before, after):
    base = repo.commit(
        {
            path: before,
            "src/lib/__init__.py": "",
            "src/lib/m.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "def test_m():\n    pass\n",
        }
    )
    head = repo.commit({path: after})
    plan = repo.plan(base, head, [], ["src", "tests=tests"], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_m.py::test_m"}


def test_s9_a_changed_build_script_selects_everything(repo):
    base = repo.commit(
        {
            "setup.py": "from setuptools import setup\n\nsetup(name='lib')\n",
            "lib/__init__.py": "",
            "lib/m.py": "def f():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_m.py": "from lib.m import f\n\n\ndef test_f():\n    f()\n",
        }
    )
    head = repo.commit(
        {"setup.py": "from setuptools import setup\n\nsetup(name='lib', version='2')\n"}
    )
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_m.py::test_f"}


EVERYTHING = {t.runner_id for t in TARGETS}


@pytest.mark.parametrize(
    "path",
    [
        "dev-requirements.txt",
        "test-requirements.txt",
        "Requirements.txt",
        "requirements.pip",
        "ci/deps/actions-311.yaml",
        "hatch.toml",
        "poetry.toml",
        "pdm.toml",
        ".env",
        "zzz.pth",
        "sitecustomize.py",
        "noxfile.py",
        "toxfile.py",
        ".github/workflows/test.yml",
        "mypy.ini",
        "pyrightconfig.json",
    ],
)
def test_pln4_dependency_and_runner_files_outside_the_roots(repo, path):
    base = repo.commit({**TREE, path: "a = 1\n"})
    head = repo.commit({path: "a = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=SPLIT_ROOTS)
    assert not plan.degraded
    assert selected(plan) == EVERYTHING
    assert rules(plan, "tests/test_ops.py::test_add") == {"unanalysed_file_changed"}
    assert path in reason(plan, "bench_other.time_helper", "unanalysed_file_changed").detail


@pytest.mark.parametrize(
    "path",
    [
        "packages/api/setup.py",
        "src/pkg/sub/setup.py",
        "noxfile.py",
        "toxfile.py",
        "sitecustomize.py",
        "src/usercustomize.py",
        "packages/api/pdm_build.py",
    ],
)
def test_pln4_python_build_and_startup_scripts_under_a_root(repo, path):
    """Under a root a build script is an indexed module nobody imports: its
    change used to reach nothing unless it sat at the repository root."""
    base = repo.commit({**TREE, path: "x = 1\n"})
    head = repo.commit({path: "x = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["src", "."])
    assert selected(plan) == EVERYTHING
    assert rules(plan, "tests/test_ops.py::test_add") == {"unanalysed_file_changed"}


def test_pln4_build_py_counts_beside_a_project_file(repo):
    """``build.py`` is a build script beside a ``pyproject.toml`` (poetry's
    ``build`` setting); elsewhere it is an ordinary module, planned as one."""
    tree = {
        **TREE,
        "packages/api/pyproject.toml": "[project]\nname = 'api'\n",
        "packages/api/build.py": "x = 1\n",
        "src/pkg/build.py": "def build():\n    return 1\n",
    }
    base = repo.commit(tree)
    head = repo.commit({"packages/api/build.py": "x = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["src", "."])
    assert selected(plan) == EVERYTHING
    head2 = repo.commit({"src/pkg/build.py": "def build():\n    return 2\n"})
    plan = repo.plan(head, head2, TARGETS, source_roots=["src", "."])
    assert selected(plan) == set()


def test_pln4_lock_file_between_index_and_worktree(repo):
    """Neither side committed: the runner files outside the roots were not
    compared at all."""
    repo.commit({**TREE, "uv.lock": "v1\n"})
    (repo.path / "uv.lock").write_text("v2\n")
    plan = repo.plan("INDEX", "WORKTREE", TARGETS, source_roots=SPLIT_ROOTS)
    assert selected(plan) == EVERYTHING
    assert (
        "uv.lock" in reason(plan, "tests/test_ops.py::test_add", "unanalysed_file_changed").detail
    )
    # Staged, it is the same on both sides.
    repo.git("add", "uv.lock")
    plan = repo.plan("INDEX", "WORKTREE", TARGETS, source_roots=SPLIT_ROOTS)
    assert selected(plan) == set()


@pytest.mark.parametrize("path", ["packages/my-api/setup.py", "my-tools/noxfile.py"])
def test_pln4_build_script_no_module_name_maps_to(repo, path):
    """A build script in a directory that is no package name was neither
    indexed nor counted as a file the index does not read."""
    base = repo.commit({**TREE, path: "x = 1\n"})
    head = repo.commit({path: "x = 2\n"})
    plan = repo.plan(base, head, TARGETS, source_roots=["src", "."])
    assert selected(plan) == EVERYTHING
    assert path in reason(plan, "tests/test_ops.py::test_add", "unanalysed_file_changed").detail


# --------------------------------------------------------------------------- manifests


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


def test_manifest_that_is_not_json_says_so(tmp_path):
    from diffcone.manifest import ManifestError, load_manifest

    path = tmp_path / "targets.json"
    path.write_text("{not json")
    with pytest.raises(ManifestError, match="is not valid JSON"):
        load_manifest(path)


# --------------------------------------------------------------------------- declaration files

ALWAYS_TREE = {
    **TREE,
    "tests/e2e/test_flow.py": "def test_flow():\n    pass\n",
}

ALWAYS_TARGETS = [
    *TARGETS,
    py_target("tests/e2e/test_flow.py::test_flow", "tests.e2e.test_flow.test_flow"),
]


def test_pln6_always_run_with_an_unknown_runner_is_an_analysis_error(repo):
    base = repo.commit(ALWAYS_TREE)
    head = repo.commit(
        {
            "diffcone.toml": '[[always_run]]\ntargets = "tests/e2e/*"\nrunner = "pyest"\n',
            "src/pkg/ops.py": "def add(a, b):\n    return b + a\n",
        }
    )
    plan = repo.plan(base, head, ALWAYS_TARGETS, source_roots=SPLIT_ROOTS)
    assert plan.degraded
    assert [e.path for e in plan.errors] == ["diffcone.toml"]
    assert "'pyest'" in plan.errors[0].message
    assert selected(plan) == {t.runner_id for t in ALWAYS_TARGETS}


def test_pln6_always_run_runner_of_a_manifest_target_or_absent_from_the_plan(repo):
    """A runner label a manifest target uses is a runner; so is ``asv`` in a
    job that plans no benchmarks (one file serves every job)."""
    targets = [
        *(t for t in ALWAYS_TARGETS if t.runner != "asv"),
        Target("nox", "lint", "tests.e2e.test_flow.test_flow", ()),
    ]
    base = repo.commit(ALWAYS_TREE)
    head = repo.commit(
        {
            "diffcone.toml": (
                '[[always_run]]\ntargets = "lint"\nrunner = "nox"\n\n'
                '[[always_run]]\ntargets = "*"\nrunner = "asv"\n'
            ),
            "src/pkg/ops.py": "def add(a, b):\n    return b + a\n",
        }
    )
    plan = repo.plan(base, head, targets, source_roots=SPLIT_ROOTS)
    assert not plan.degraded
    assert selected(plan) == {"tests/test_ops.py::test_add", "lint"}
    assert {a.runner: n for a, n in plan.always_run_matched.items()} == {"nox": 1, "asv": 0}


TREE_AB = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def f():\n    return 1\n",
    "pkg/b.py": "def g():\n    return 1\n",
    "tests/test_a.py": "from pkg.a import f\n\n\ndef test_a():\n    f()\n",
    "benchmarks/bench_b.py": "from pkg.b import g\n\n\ndef time_b():\n    g()\n",
}

TARGETS_AB = [
    py_target("tests/test_a.py::test_a", "tests.test_a.test_a"),
    asv_target("bench_b.time_b", "benchmarks.bench_b.time_b"),
]


def test_unmerged_declaration_file_is_an_analysis_error(repo):
    """A ``diffcone.toml`` the index holds only unmerged (a conflicted merge)
    could not be read, which was taken for no file: its declarations
    silently did nothing. It is an analysis error now, so everything runs."""
    toml = '[[edges]]\nfrom = "pkg.a.f"\nto = "pkg.b.g"\nwhy = "{}"\n'
    base = repo.commit({**TREE_AB, "diffcone.toml": toml.format("one")})
    repo.git("checkout", "-q", "-b", "other")
    repo.commit({"diffcone.toml": toml.format("two")})
    repo.git("checkout", "-q", "main")
    repo.commit({"diffcone.toml": toml.format("three")})
    assert repo.try_git("merge", "-q", "other").returncode != 0
    plan = repo.plan(base, "INDEX", TARGETS_AB, source_roots=["."])
    assert [e.path for e in plan.errors] == ["diffcone.toml"]
    assert selected(plan) == {t.runner_id for t in TARGETS_AB}

"""Regression scenarios for the fourth audit round (the code-quality pass),
indexer core: Q11-Q15.

Each test names the finding it guards and failed before its fix.
"""

from __future__ import annotations

from diffcone.cache import IndexCache
from diffcone.indexer import build_index
from diffcone.model import REFERENCES, SnapshotInfo
from diffcone.snapshot import Snapshot
from diffcone.testing import FixtureRepo, asv_target, py_target, selected

# A benchmark on code no scenario changes: a plan that selects it selected
# everything.
BENCH = {
    "pkg/__init__.py": "",
    "pkg/quiet.py": "def idle():\n    return 0\n",
    "benchmarks/bench_quiet.py": "from pkg.quiet import idle\n\n\ndef time_idle():\n    idle()\n",
}
BENCH_TARGET = asv_target("bench_quiet.time_idle", "benchmarks.bench_quiet.time_idle")


def _plan(repo: FixtureRepo, base: dict, head: dict, *tests: str):
    """Plan ``head`` against ``base`` with a pytest target per test id
    (``tests/test_m.py::test_x``) and the benchmark."""
    targets = [py_target(t, t.replace("/", ".").replace(".py::", ".")) for t in tests] + [
        BENCH_TARGET
    ]
    first = repo.commit({**BENCH, **base})
    second = repo.commit(head)
    return repo.plan(first, second, targets)


# Q11: a caller the index cannot see (a call through a receiver of unknown
# type, or a construction of an escaping class) may pass anything: every rule
# that bounds a parameter by what the call sites pass must give up then.

ENV_B = 'import os\nos.environ["DC_X"] = "{v}"\n'


def test_q11_dynamic_import_called_through_an_unknown_receiver(repo):
    util = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
    user = (
        "from pkg import util\n\n\ndef direct():\n    return util.load('pkg.a')\n\n\n"
        "class Runner:\n    def __init__(self, loader):\n        self.loader = loader\n\n"
        "    def go(self):\n        return self.loader.load('pkg.b')\n"
    )
    test = (
        "import os\nfrom pkg import util\nfrom pkg.user import Runner\n\n\n"
        "def test_go():\n    Runner(util).go()\n    assert os.environ['DC_X'] == '1'\n"
    )
    plan = _plan(
        repo,
        {
            "pkg/util.py": util,
            "pkg/user.py": user,
            "pkg/a.py": "",
            "pkg/b.py": ENV_B.format(v=1),
            "tests/test_m.py": test,
        },
        {"pkg/b.py": ENV_B.format(v=2)},
        "tests/test_m.py::test_go",
    )
    assert selected(plan) == {"tests/test_m.py::test_go"}


def test_q11_dynamic_import_in_the_init_of_an_escaping_class(repo):
    loader = (
        "import importlib\n\n\nclass Loader:\n    def __init__(self, name):\n"
        "        importlib.import_module(name)\n\n\n"
        "def direct():\n    return Loader('pkg.a')\n\n\n"
        "def factory():\n    return Loader\n"
    )
    test = (
        "import os\nfrom pkg.loader import factory\n\n\n"
        "def test_make():\n    factory()('pkg.b')\n    assert os.environ['DC_X'] == '1'\n"
    )
    plan = _plan(
        repo,
        {
            "pkg/loader.py": loader,
            "pkg/a.py": "",
            "pkg/b.py": ENV_B.format(v=1),
            "tests/test_m.py": test,
        },
        {"pkg/b.py": ENV_B.format(v=2)},
        "tests/test_m.py::test_make",
    )
    assert selected(plan) == {"tests/test_m.py::test_make"}


def test_q11_receiver_classes_with_a_caller_through_an_unknown_receiver(repo):
    inv = "def invoke(obj, name):\n    return getattr(obj, name)()\n"
    prov = (
        "class Provider:\n    def ping(self):\n        return 1\n\n\n"
        "class Other:\n    def pong(self):\n        return {v}\n"
    )
    user = (
        "from pkg.inv import invoke\nfrom pkg.prov import Provider\n\n\n"
        "def direct(name):\n    return invoke(Provider(), name)\n"
    )
    test = (
        "from pkg import inv\nfrom pkg.prov import Other\n\n\n"
        "def test_pong():\n    holder = [inv][0]\n"
        "    assert holder.invoke(Other(), 'pong') == 1\n"
    )
    plan = _plan(
        repo,
        {
            "pkg/inv.py": inv,
            "pkg/prov.py": prov.format(v=1),
            "pkg/user.py": user,
            "tests/test_m.py": test,
        },
        {"pkg/prov.py": prov.format(v=2)},
        "tests/test_m.py::test_pong",
    )
    assert selected(plan) == {"tests/test_m.py::test_pong"}


# Q12: a decorator's (or a base's) name is known only through an import that
# is its only binding: an alternative import in a try/except, a star import,
# a later assignment may make it the project's own.

DECO_READS_DEFAULT = (
    "import os\n\n\ndef final(f):\n    os.environ['DC_Y'] = str(f.__defaults__[0])\n    return f\n"
)
TEST_ENV_Y = "import os\nimport pkg.m\n\n\ndef test_env():\n    assert os.environ['DC_Y'] == '1'\n"


def _q12(repo, imports: str):
    m = imports + "\n\n@final\ndef helper(x={v}):\n    return x\n"
    return _plan(
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
    plan = _plan(
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
    plan = _plan(
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
    plan = _plan(
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


# Q13: ``@deco`` is the call ``deco(f)`` at import: a decorator not known to
# call nothing of what it decorates may call it then, or keep it and call it
# later with arguments no call site shows.

DECO_CALLS = "def final(f):\n    f()\n    return f\n"
TEST_ENV_Z = "import os\nimport pkg.m\n\n\ndef test_env():\n    assert os.environ['DC_Z'] == '1'\n"


def test_q13_decorator_calls_the_function_at_import(repo):
    m = (
        "import os\nfrom pkg.deco import final\n\n\n"
        "@final\ndef helper():\n    os.environ['DC_Z'] = '{v}'\n"
    )
    plan = _plan(
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
    plan = _plan(
        repo,
        {"pkg/deco.py": deco, "pkg/m.py": m.format(v=1), "tests/test_m.py": TEST_ENV_Z},
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_env",
    )
    assert selected(plan) == {"tests/test_m.py::test_env"}


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
    plan = _plan(
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
    plan = _plan(
        repo,
        {"pkg/m.py": m.format(v=1), "tests/test_m.py": test},
        {"pkg/m.py": m.format(v=2)},
        "tests/test_m.py::test_other",
    )
    assert selected(plan) == set()


# The project's own decorators are read: one whose own code only reads and
# sets attributes of the function, returns it or a ``functools.wraps``
# wrapper, calls nothing of it while the ``def`` runs. Anything else may.

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
    plan = _plan(
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
        plan = _plan(
            sub,
            {"pkg/deco.py": decos, "pkg/m.py": m.format(v=1), "tests/test_m.py": TEST_ENV_Z},
            {"pkg/m.py": m.format(v=2)},
            "tests/test_m.py::test_env",
        )
        assert selected(plan) == {"tests/test_m.py::test_env"}, decorator


# Q22: a subscript or attribute read in an annotation evaluated at import
# (or in a decorator's argument) may run project code: ``Registry["k"]``
# runs ``Registry.__class_getitem__``.

REGISTRY = (
    "import os\n\n\nclass Registry:\n    def __class_getitem__(cls, key):\n"
    "        os.environ['DC_Q'] = key\n        return cls\n"
)
TEST_Q = "import os\nimport pkg.m\n\n\ndef test_q():\n    assert os.environ['DC_Q'] == '1'\n"


def test_q22_annotation_of_a_module_variable_runs_at_import(repo):
    m = "from pkg.reg import Registry\n\nX: Registry['1'] = 1\n"
    plan = _plan(
        repo,
        {"pkg/reg.py": REGISTRY, "pkg/m.py": m, "tests/test_m.py": TEST_Q},
        {"pkg/reg.py": REGISTRY.replace("= key", "= key + '!'")},
        "tests/test_m.py::test_q",
    )
    assert selected(plan) == {"tests/test_m.py::test_q"}


def test_q22_added_variable_whose_annotation_runs_project_code(repo):
    m = "from pkg.reg import Registry\n\nX: Registry['1'] = 1\n"
    plan = _plan(
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


# Q14: a module too deeply nested for the recursive walks of its syntax tree
# is an analysis error (select everything), not an exception out of planning.


def test_q14_deeply_nested_module_degrades_the_plan(repo, tmp_path):
    deep = "X = " + " + ".join(["1"] * 5000) + "\n"
    first = repo.commit({**BENCH, "pkg/big.py": deep, "pkg/m.py": "def f():\n    return 1\n"})
    second = repo.commit({"pkg/m.py": "def f():\n    return 2\n"})
    test = py_target("tests/test_m.py::test_f", "tests.test_m.test_f")
    plan = repo.plan(first, second, [test, BENCH_TARGET])
    assert plan.degraded
    assert any("too deeply nested" in e.message for e in plan.errors)
    assert selected(plan) == {"tests/test_m.py::test_f", "bench_quiet.time_idle"}
    # Running out of stack depends on the caller: such an index is not
    # cached whole.
    cache = IndexCache(tmp_path / "cache")
    repo.plan(first, second, [test, BENCH_TARGET], cache=cache)
    assert not list((tmp_path / "cache" / "index").glob("*.json"))


# Q15: the edges recorded beside a hit found after an unknown base (an
# override, the constructor) keep the kind they were asked for.


def test_q15_override_edges_keep_their_kind_after_an_uncertain_hit():
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": (
            "import external\n\n\nclass Mixin:\n    def run(self):\n        return 1\n\n"
            "    class Inner:\n        def __init__(self):\n            pass\n\n\n"
            "class Base(external.Thing, Mixin):\n    def go(self):\n        self.run()\n"
            "        self.Inner()\n\n\nclass Sub(Base):\n    def run(self):\n        return 2\n"
        ),
    }
    snapshot = Snapshot(
        info=SnapshotInfo(revision="x", commit="x"),
        source_roots=["."],
        files={k: v.encode() for k, v in files.items()},
    )
    edges = {
        (e.target, e.kind, e.detail)
        for e in build_index(snapshot).edges
        if e.source == "pkg.m.Base.go"
    }
    assert ("pkg.m.Sub.run", REFERENCES, "override") in edges
    assert ("pkg.m.Mixin.Inner.__init__", REFERENCES, "constructor") in edges

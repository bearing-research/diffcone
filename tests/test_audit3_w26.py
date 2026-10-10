"""Regression scenarios for W26 (internal/audit.md, "Known after round 3"):
which stores a test makes are undone when the test ends
(indexer/patches.py).

A patch undone after the test is seen only by code running during that
test, which depends on what it stores: it is no write onto an external
module for a lookup there (precision: marshmallow's
``getattr(dt, self.OBJ_TYPE)`` with one test patching ``datetime.datetime``
selected the whole suite for any change), and puts nothing on an in-scope
module for later code (W24's ``module_writers``). A store that outlives the
test (a patcher started and kept, a ``MonkeyPatch()`` of its own, a
``with patch`` around a ``yield``, any other receiver) is a write in both:
the external lookup sees every change, and the module's lookups follow the
writer (soundness: W24 dropped every patch-like store).
"""

from __future__ import annotations

import pytest

from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
from diffcone.testing import asv_target, selected

HEADER = (
    "import contextlib\nimport unittest\nfrom unittest import mock\n"
    "from unittest.mock import patch\n\nimport pytest\n\n{imp}\n\n\n"
)

# Each form stores ``{new}`` as ``{mod}.{attr}`` in a test of
# tests/test_patch.py, and calls the lookup there (as marshmallow's
# test does).
UNDONE = {
    "with": ("def test_patch():\n    with patch('{mod}.{attr}', {new}):\n        assert {call}\n"),
    "async-with": (
        "async def test_patch():\n    async with patch('{mod}.{attr}', {new}):\n"
        "        assert {call}\n"
    ),
    "with-object": (
        "def test_patch():\n    with mock.patch.object({mod}, '{attr}', {new}):\n"
        "        assert {call}\n"
    ),
    "with-returns": (
        "def test_patch():\n    with patch('{mod}.{attr}', {new}):\n        return {call}\n"
    ),
    "decorator": "@patch('{mod}.{attr}', {new})\ndef test_patch():\n    assert {call}\n",
    "decorator-object": (
        "@mock.patch.object({mod}, '{attr}', {new})\ndef test_patch():\n    assert {call}\n"
    ),
    "class-decorator": (
        "@patch('{mod}.{attr}', {new})\nclass TestPatch(unittest.TestCase):\n"
        "    def test_patch(self):\n        assert {call}\n"
    ),
    # A decorated generator runs unpatched: the patch ends with the call
    # that makes it.
    "generator-decorator": (
        "@pytest.fixture\n@patch('{mod}.{attr}', {new})\ndef patched():\n    yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "monkeypatch": (
        "def test_patch(monkeypatch):\n    monkeypatch.setattr({mod}, '{attr}', {new})\n"
        "    assert {call}\n"
    ),
    "monkeypatch-dotted": (
        "def test_patch(monkeypatch):\n    monkeypatch.setattr('{mod}.{attr}', {new})\n"
        "    assert {call}\n"
    ),
    "monkeypatch-fixture": (
        "@pytest.fixture\ndef patched(monkeypatch):\n"
        "    monkeypatch.setattr({mod}, '{attr}', {new})\n    yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "monkeypatch-method": (
        "class TestPatch:\n    def test_patch(self, monkeypatch):\n"
        "        monkeypatch.setattr({mod}, '{attr}', {new})\n        assert {call}\n"
    ),
    "mocker": (
        "def test_patch(mocker):\n    mocker.patch('{mod}.{attr}', {new})\n    assert {call}\n"
    ),
    "monkeypatch-context": (
        "def test_patch():\n    with pytest.MonkeyPatch.context() as mp:\n"
        "        mp.setattr({mod}, '{attr}', {new})\n        assert {call}\n"
    ),
}

KEPT = {
    "start": "def test_patch():\n    patch('{mod}.{attr}', {new}).start()\n    assert {call}\n",
    "start-stop": (
        "def test_patch():\n    p = patch('{mod}.{attr}', {new})\n    p.start()\n"
        "    assert {call}\n    p.stop()\n"
    ),
    "stored": (
        "PATCHER = patch('{mod}.{attr}', {new})\n\n\n"
        "def test_patch():\n    PATCHER.start()\n    assert {call}\n"
    ),
    "stored-attribute": (
        "class TestPatch(unittest.TestCase):\n    def setUp(self):\n"
        "        self.patcher = mock.patch.object({mod}, '{attr}', {new})\n"
        "        self.patcher.start()\n\n    def test_patch(self):\n        assert {call}\n"
    ),
    "monkeypatch-object": (
        "def test_patch():\n    mp = pytest.MonkeyPatch()\n"
        "    mp.setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    "monkeypatch-object-dotted": (
        "def test_patch():\n    mp = pytest.MonkeyPatch()\n"
        "    mp.setattr('{mod}.{attr}', {new})\n    assert {call}\n"
    ),
    "context-entered": (
        "def test_patch():\n    mp = pytest.MonkeyPatch.context().__enter__()\n"
        "    mp.setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    "yield-fixture": (
        "@pytest.fixture(scope='module')\ndef patched():\n"
        "    with patch('{mod}.{attr}', {new}):\n        yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "yield-context-fixture": (
        "@pytest.fixture(scope='module')\ndef patched():\n"
        "    with pytest.MonkeyPatch.context() as mp:\n"
        "        mp.setattr({mod}, '{attr}', {new})\n        yield\n\n\n"
        "def test_patch(patched):\n    assert {call}\n"
    ),
    "contextmanager": (
        "@contextlib.contextmanager\ndef patched():\n"
        "    with patch('{mod}.{attr}', {new}):\n        yield\n\n\n"
        "def test_patch():\n    with patched():\n        assert {call}\n"
    ),
    "receiver": (
        "def put(patcher):\n    patcher.setattr({mod}, '{attr}', {new})\n\n\n"
        "def test_patch():\n    put(pytest.MonkeyPatch())\n    assert {call}\n"
    ),
    "helper-parameter": (
        "def put(monkeypatch):\n    monkeypatch.setattr({mod}, '{attr}', {new})\n\n\n"
        "def test_patch():\n    put(pytest.MonkeyPatch())\n    assert {call}\n"
    ),
    "closure": (
        "LATER = []\n\n\ndef test_patch(monkeypatch):\n    def later():\n"
        "        monkeypatch.setattr({mod}, '{attr}', {new})\n\n"
        "    LATER.append(later)\n    assert {call}\n"
    ),
    "rebound": (
        "def test_patch(monkeypatch):\n    monkeypatch = pytest.MonkeyPatch()\n"
        "    monkeypatch.setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    "module-mocker": (
        "def test_patch(module_mocker):\n    module_mocker.patch('{mod}.{attr}', {new})\n"
        "    assert {call}\n"
    ),
    "module-level-with": (
        "with patch('{mod}.{attr}', {new}):\n    KEPT = {call}\n\n\n"
        "def test_patch():\n    assert KEPT\n"
    ),
    "builtin-setattr": (
        "def test_patch():\n    setattr({mod}, '{attr}', {new})\n    assert {call}\n"
    ),
    # A store for good beside an undone one of the same name, by the same
    # code: still a store.
    "store-beside-patch": (
        "def test_patch(monkeypatch):\n    monkeypatch.setattr({mod}, '{attr}', {new})\n"
        "    {mod}.{attr} = {new}\n    assert {call}\n"
    ),
}


def _patch_module(form: str, *, imp: str, mod: str, attr: str, new: str, call: str) -> str:
    body = {**UNDONE, **KEPT}[form]
    fields = {"mod": mod, "attr": attr, "new": new, "call": call}
    return HEADER.format(imp=imp) + body.format(**fields)


# ------------------------------------------- an external module (``datetime``)


def _external_plan(repo, form: str):
    """``pkg.fields.make`` looks a name nothing bounds up on ``datetime``;
    tests/test_patch.py stores ``NEW`` there by ``form``. An unrelated test
    changes."""
    files = {
        "pkg/__init__.py": "",
        "pkg/fields.py": (
            "import datetime as dt\n\n\ndef make(name):\n    return getattr(dt, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/test_fields.py": (
            "import os\n\nfrom pkg.fields import make\n\n\n"
            "def test_make():\n    assert make(os.environ.get('W26', 'date'))\n"
        ),
        "tests/test_patch.py": _patch_module(
            form,
            imp="import datetime\n\nfrom pkg.fields import make\n\n\ndef NEW():\n    return 1",
            mod="datetime",
            attr="datetime",
            new="NEW",
            call="make('datetime')",
        ),
        "tests/test_other.py": "def test_other():\n    assert True\n",
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": (
            "import os\n\nfrom pkg.fields import make\n\n\n"
            "def time_make():\n    make(os.environ.get('W26', 'date'))\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit({"tests/test_other.py": "def test_other():\n    assert 1\n"})
    targets = [asv_target("bench.time_make", "benchmarks.bench.time_make")]
    return repo.plan(base, head, targets, discover_runners=["pytest"])


def _outside_patch_module(plan) -> set[str]:
    return {s for s in selected(plan) if not s.startswith("tests/test_patch.py")}


@pytest.mark.parametrize("form", sorted(UNDONE))
def test_w26_a_patch_undone_after_the_test_writes_no_external_module(repo, form):
    """Only ``test_patch`` sees ``NEW`` on ``datetime``, and it depends on
    ``NEW``: the lookup in ``make`` is bounded by what ``datetime``
    defines, so a change to another test selects only that test."""
    plan = _external_plan(repo, form)
    assert _outside_patch_module(plan) == {"tests/test_other.py::test_other"}
    assert not selected(plan) & {"tests/test_patch.py::test_patch"}


@pytest.mark.parametrize("form", sorted(KEPT))
def test_w26_a_store_that_outlives_the_test_writes_the_external_module(repo, form):
    """``NEW`` may stay on ``datetime`` for code that runs later: the
    lookup in ``make`` may find it, so every lookup sees any change."""
    plan = _external_plan(repo, form)
    assert _outside_patch_module(plan) == {
        "tests/test_other.py::test_other",
        "tests/test_fields.py::test_make",
        "bench.time_make",
    }


# ------------------------------------------- an in-scope module (W24's closure)


def _module_plan(repo, form: str):
    """``pkg.lib.get`` looks a name nothing bounds up on ``pkg.registry``;
    tests/test_patch.py stores ``tests.fake_registry.VALUE`` there by
    ``form``. ``VALUE`` changes."""
    files = {
        "pkg/__init__.py": "",
        "pkg/registry.py": "VALUE = 0\n",
        "pkg/lib.py": (
            "def get(name):\n    from pkg import registry\n\n    return getattr(registry, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/fake_registry.py": "VALUE = 1\n",
        "tests/test_b.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def test_b():\n    assert get(os.environ.get('W26', 'VALUE')) == 1\n"
        ),
        "tests/test_c.py": "def test_c():\n    pass\n",
        "tests/test_patch.py": _patch_module(
            form,
            imp=(
                "import pkg.registry\nfrom pkg.lib import get\n"
                "from tests.fake_registry import VALUE as NEW"
            ),
            mod="pkg.registry",
            attr="VALUE",
            new="NEW",
            call="get('VALUE')",
        ),
        "benchmarks/__init__.py": "",
        "benchmarks/bench.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def time_get():\n    get(os.environ.get('W26', 'VALUE'))\n"
        ),
    }
    base = repo.commit(files)
    head = repo.commit({"tests/fake_registry.py": "VALUE = 2\n"})
    targets = [asv_target("bench.time_get", "benchmarks.bench.time_get")]
    return repo.plan(base, head, targets, discover_runners=["pytest"])


@pytest.mark.parametrize("form", sorted(UNDONE))
def test_w26_a_patch_undone_after_the_test_puts_nothing_on_a_module(repo, form):
    """Only ``test_patch`` (which imports ``tests.fake_registry``) sees the
    new ``VALUE`` on ``pkg.registry``: the other lookups do not follow it."""
    plan = _module_plan(repo, form)
    assert _outside_patch_module(plan) == set()
    assert selected(plan)  # test_patch imports tests.fake_registry


@pytest.mark.parametrize("form", sorted(KEPT))
def test_w26_a_store_that_outlives_the_test_puts_objects_on_a_module(repo, form):
    """The stored ``VALUE`` may stay on ``pkg.registry`` for the tests that
    run later, whose lookups then find ``tests.fake_registry``'s object:
    they follow the writer's module and its imports."""
    plan = _module_plan(repo, form)
    assert {"tests/test_b.py::test_b", "bench.time_get"} <= selected(plan)
    assert "tests/test_c.py::test_c" not in selected(plan)


# ------------------------------------------- ``sys.modules`` entries


SWAPPED = {
    "monkeypatch": "def test_patch(monkeypatch):\n    {put}\n",
    "with-dict": (
        "def test_patch():\n    with patch.dict(sys.modules, {{'pkg.registry': fake}}):\n"
        "        pass\n"
    ),
}
INSTALLED = {
    "monkeypatch-object": "def test_patch():\n    mp = pytest.MonkeyPatch()\n    {put}\n",
    "dict-start": (
        "def test_patch():\n    patch.dict(sys.modules, {{'pkg.registry': fake}}).start()\n"
    ),
    "receiver": "def test_patch(mp):\n    {put}\n",
}


def _modules_plan(repo, body: str, receiver: str):
    put = f"{receiver}.setitem(sys.modules, 'pkg.registry', fake)"
    files = {
        "pkg/__init__.py": "",
        "pkg/registry.py": "VALUE = 0\n",
        "pkg/lib.py": (
            "def get(name):\n    from pkg import registry\n\n    return getattr(registry, name)\n"
        ),
        "tests/__init__.py": "",
        "tests/fake_registry.py": "VALUE = 1\n",
        "tests/conftest.py": (
            "import pytest\n\n\n@pytest.fixture\ndef mp():\n    return pytest.MonkeyPatch()\n"
        ),
        "tests/test_b.py": (
            "import os\n\nfrom pkg.lib import get\n\n\n"
            "def test_b():\n    assert get(os.environ.get('W26', 'VALUE')) == 1\n"
        ),
        "tests/test_c.py": "def test_c():\n    pass\n",
        "tests/test_patch.py": (
            "import sys\nfrom unittest.mock import patch\n\nimport pytest\n\n"
            "import tests.fake_registry as fake\n\n\n" + body.format(put=put)
        ),
    }
    base = repo.commit(files)
    head = repo.commit({"tests/fake_registry.py": "VALUE = 2\n"})
    return repo.plan(base, head, [], discover_runners=["pytest"])


@pytest.mark.parametrize("form", sorted(SWAPPED))
def test_w26_a_module_swapped_for_one_test(repo, form):
    """``sys.modules['pkg.registry']`` replaced for ``test_patch`` alone:
    no later import gets ``tests.fake_registry``."""
    plan = _modules_plan(repo, SWAPPED[form], "monkeypatch")
    assert "tests/test_b.py::test_b" not in selected(plan)


@pytest.mark.parametrize("form", sorted(INSTALLED))
def test_w26_a_module_installed_beyond_the_test(repo, form):
    """Installed by a ``MonkeyPatch`` nothing undoes (or a ``patch.dict``
    started and kept): a later ``from pkg import registry`` may get
    ``tests.fake_registry``, so ``test_b`` follows it."""
    plan = _modules_plan(repo, INSTALLED[form], "mp")
    assert "tests/test_b.py::test_b" in selected(plan)
    assert "tests/test_c.py::test_c" not in selected(plan)


# ------------------------------------------- process state put back by a ``with``

ENV = (
    "import os\nimport warnings\nfrom unittest import mock\n\n\n"
    "def restored():\n    with mock.patch.dict(os.environ, {}):\n"
    "        os.environ['MODE'] = 'x'\n    with warnings.catch_warnings():\n"
    "        warnings.simplefilter('ignore')\n\n\n"
    "def left():\n    with mock.patch.dict(os.environ, {}):\n"
    "        os.environ['MODE'] = 'x'\n        yield\n\n\n"
    "def filters_left():\n    with warnings.catch_warnings():\n"
    "        warnings.simplefilter('ignore')\n        yield\n"
)


def test_w26_process_state_a_with_puts_back_after_a_yield_is_written(repo):
    """``with mock.patch.dict(os.environ)`` and ``with
    warnings.catch_warnings()`` put the state back when the statement ends;
    a body that yields leaves it changed while it is suspended, for
    whatever runs meanwhile."""
    base = repo.commit({"pkg/__init__.py": "", "pkg/env.py": ENV})
    index = build_index(read_snapshot(repo.path, base, source_roots=["."]))
    writers = {symbol for symbol, _ in index.process_writes}
    assert writers == {"pkg.env.left", "pkg.env.filters_left"}

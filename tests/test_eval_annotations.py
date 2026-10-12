"""Regression scenarios for annotations evaluated outside every test (E7).

Python 3.14 evaluates a definition's annotations lazily, in a code object of
their own (``__annotate__``) that starts on the ``def`` line, and pytest
calls it while collecting (``inspect.signature``). The recorder credited it
to the function, as code run outside every test, so an edit to the body of
any annotated test or fixture selected every test. A body or docstring
change leaves ``__annotate__`` as it was; any other change, and code of the
symbol's own that ran there, is taken as before.

On Python 3.12 and 3.13 annotations are evaluated with the ``def``, inside
the module's import, so the narrow plans hold there without the rule; the
tests that failed before the fix fail on 3.14.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from diffcone import evidence as evidence_module
from diffcone.evidence import load_store, write_store
from diffcone.testing import selected

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

OPS = "def add(a, b):\n    return a + b\n"
TYPED = """\
from pathlib import Path

from pkg.ops import add


def test_typed(tmp_path: Path) -> None:
    assert add(1, 2) == 3


def test_plain():
    assert add(2, 2) == 4
"""
OTHER = "from pkg.ops import add\n\n\ndef test_other():\n    assert add(0, 0) == 0\n"
FILES = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/ops.py": OPS,
    "tests/__init__.py": "",
    "tests/test_ops.py": TYPED,
    "tests/test_other.py": OTHER,
}
EVERY = {
    "tests/test_ops.py::test_typed",
    "tests/test_ops.py::test_plain",
    "tests/test_other.py::test_other",
}


def _plan(repo, base, head, evidence):
    return repo.plan(base, head, [], discover_runners=["pytest"], evidence=evidence)


@needs_monitoring
def test_e7_a_body_change_of_an_annotated_test_stays_in_its_file(repo):
    """The other tests of the edited file are selected too: each test's run
    stats its own file (``touched_file``), on every Python version."""
    base = repo.commit(FILES)
    ev = repo.collect()
    head = repo.commit({"tests/test_ops.py": TYPED.replace("add(1, 2) == 3", "add(2, 1) == 3")})
    chosen = selected(_plan(repo, base, head, ev))
    assert "tests/test_ops.py::test_typed" in chosen
    assert "tests/test_other.py::test_other" not in chosen


CONFTEST = (
    "from pathlib import Path\n\nimport pytest\n\n\n@pytest.fixture\n"
    "def data(tmp_path: Path) -> dict:\n    return {'n': 1}\n"
)
USES = "def test_uses(data):\n    assert data['n'] == 1\n"
WITH_FIXTURE = {**FILES, "tests/conftest.py": CONFTEST, "tests/test_uses.py": USES}


@needs_monitoring
def test_e7_a_body_change_of_an_annotated_fixture_selects_its_tests(repo):
    base = repo.commit(WITH_FIXTURE)
    ev = repo.collect()
    head = repo.commit({"tests/conftest.py": CONFTEST.replace("{'n': 1}", "dict(n=1)")})
    assert selected(_plan(repo, base, head, ev)) == {"tests/test_uses.py::test_uses"}


@needs_monitoring
def test_e7_an_annotation_change_is_not_narrowed(repo):
    """What ``__annotate__`` evaluates changed: on 3.14 pytest ran it while
    collecting, so every test is selected (code run in a hook); before 3.14
    a fixture's definition change reaches every test in its scope."""
    base = repo.commit(WITH_FIXTURE)
    ev = repo.collect()
    head = repo.commit({"tests/conftest.py": CONFTEST.replace("-> dict", "-> object")})
    assert selected(_plan(repo, base, head, ev)) == EVERY | {"tests/test_uses.py::test_uses"}


@needs_monitoring
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
    assert selected(_plan(repo, base, head, ev)) == EVERY


@needs_monitoring
def test_e7_the_store_keeps_what_ran_only_through_annotations(repo, tmp_path):
    repo.commit(FILES)
    ev = repo.collect()
    again = load_store(write_store(ev, tmp_path / "stores"))
    assert again.annotations_only == ev.annotations_only
    if sys.version_info >= (3, 14):
        assert "tests.test_ops.test_typed" in ev.annotations_only


def test_e7_a_symbol_is_annotations_only_only_if_every_source_says_so():
    """Advancing a store merges recordings: a symbol whose own code ran
    outside the tests in any of them is not annotations-only."""

    def source(only, hook):
        return SimpleNamespace(
            annotations_only=set(only), import_phase=set(hook), hook_phase=set(hook), import_by={}
        )

    merged = evidence_module._annotations_only(
        [source({"a.f", "a.g"}, {"a.f", "a.g"}), source({"a.h"}, {"a.g", "a.h"})]
    )
    assert merged == {"a.f", "a.h"}


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


@needs_monitoring
def test_e7_a_body_change_of_a_function_whose_signature_an_import_read(repo):
    """``route`` evaluated ``handler``'s annotations while ``pkg.routes`` was
    imported; its body ran only in ``test_handler``. Before: the module was
    escalated and ``test_version`` selected too."""
    base = repo.commit({**FILES, "pkg/routes.py": ROUTES, **ROUTE_TESTS})
    ev = repo.collect()
    head = repo.commit({"pkg/routes.py": ROUTES.replace("x + 1", "1 + x")})
    assert selected(_plan(repo, base, head, ev)) == {"tests/test_handler.py::test_handler"}


@needs_monitoring
def test_e7_a_decorator_that_runs_the_body_at_import_still_escalates(repo):
    """``route`` calls ``handler`` at import: its body ran there, so the
    module's import-time state may differ for every importer."""
    calls = ROUTES.replace("inspect.signature(f)  #", "f(1)  #")
    base = repo.commit({**FILES, "pkg/routes.py": calls, **ROUTE_TESTS})
    ev = repo.collect()
    head = repo.commit({"pkg/routes.py": calls.replace("x + 1", "x + 2")})
    assert {"tests/test_handler.py::test_handler", "tests/test_version.py::test_version"} <= (
        selected(_plan(repo, base, head, ev))
    )


API = "def endpoint(x: int) -> int:\n    return x * 2\n"
SIGNATURE_HOOK = (
    "import inspect\n\nfrom pkg.api import endpoint\n\n\n"
    "def pytest_configure(config):\n    config.endpoint_signature = inspect.signature(endpoint)\n"
)


@needs_monitoring
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
    assert selected(_plan(repo, base, head, ev)) == {"tests/test_endpoint.py::test_endpoint"}


# A fixture in project code (a plugin module): pytest reads its signature
# while collecting, and it reads ``FACTOR`` when it runs.
PLUGIN = """\
import pytest

FACTOR = 2


@pytest.fixture
def factor(tmp_path: object) -> int:
    return FACTOR
"""
PLUGIN_FILES = {
    **FILES,
    "pkg/plugin.py": PLUGIN,
    "tests/conftest.py": 'pytest_plugins = ["pkg.plugin"]\n',
    "tests/test_factor.py": "def test_factor(factor):\n    assert factor == 2\n",
}


@needs_monitoring
def test_e7_a_reader_whose_annotations_do_not_name_the_change(repo):
    """``factor`` reads ``FACTOR`` in its body, which ran only for its test;
    what ran during collection was its annotations (``object``, ``int``).
    Before: a reader that ran during collection, so every test."""
    base = repo.commit(PLUGIN_FILES)
    ev = repo.collect()
    head = repo.commit({"pkg/plugin.py": PLUGIN.replace("FACTOR = 2", "FACTOR = 3")})
    assert selected(_plan(repo, base, head, ev)) == {"tests/test_factor.py::test_factor"}


@needs_monitoring
def test_e7_a_reader_whose_annotations_name_the_change(repo):
    """``Annotated[object, FACTOR]``: evaluating the annotations during
    collection read ``FACTOR``, so collection saw the change: every test."""
    annotated = "from typing import Annotated\n\n" + PLUGIN.replace(
        "tmp_path: object", "tmp_path: Annotated[object, FACTOR]"
    )
    base = repo.commit({**PLUGIN_FILES, "pkg/plugin.py": annotated})
    ev = repo.collect()
    head = repo.commit({"pkg/plugin.py": annotated.replace("FACTOR = 2", "FACTOR = 3")})
    plan = selected(_plan(repo, base, head, ev))
    if sys.version_info >= (3, 14):
        assert plan == EVERY | {"tests/test_factor.py::test_factor"}
    else:
        assert "tests/test_factor.py::test_factor" in plan

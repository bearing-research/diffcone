"""Regression scenarios for misses found by evaluating discovery on real
repositories (scripts/fixture_check.py and the corpus).

Each test failed before its fix. Every scenario plans with a benchmark
target beside the discovered tests, to keep the engine runner-independent.
"""

from __future__ import annotations

import pytest

from diffcone.discovery import DiscoveryOptions, discover
from diffcone.indexer import build_index
from diffcone.snapshot import read_snapshot
from diffcone.testing import asv_target, selected

BENCH = {
    "asv.conf.json": '{"version": 1, "benchmark_dir": "benchmarks"}',
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}
BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")


def _plan(repo, base, head):
    return repo.plan(base, head, [BENCH_TARGET], discover_runners=["pytest"])


def _discovered(repo, rev):
    snap = read_snapshot(repo.path, rev, ["."], with_config=True)
    return discover("pytest", snap, build_index(snap), DiscoveryOptions())


def _deps(result, nodeid: str) -> tuple[str, ...]:
    return next(t for t in result.targets if t.runner_id == nodeid).lifecycle_dependencies


# ------------------------------------------- fixtures under a plugin's decorator

# scrapy's tests/test_http2_client_protocol.py: pytest-twisted's
# ``async_yield_fixture`` was not recognised as a fixture decorator, so the
# fixtures ``client`` requested (``site``) were no dependency; and since
# ``client`` is also pytest-django's, the name was assumed to be that
# plugin's fixture.


def _twisted(site_value: str, name: str) -> dict[str, str]:
    return {
        **BENCH,
        "tests/test_h2.py": (
            "import pytest\n"
            "from pytest_twisted import async_yield_fixture\n\n\n"
            "class TestH2:\n"
            "    @pytest.fixture\n"
            "    def site(self):\n"
            f"        return {site_value}\n\n"
            "    @async_yield_fixture\n"
            f"    async def {name}(self, site):\n"
            "        yield site\n\n"
            f"    def test_uses(self, {name}):\n"
            f"        assert {name}\n\n"
            "    def test_other(self):\n"
            "        pass\n"
        ),
    }


@pytest.mark.parametrize("name", ["client", "conn"])
def test_async_yield_fixture_requests_are_dependencies(repo, name):
    base = repo.commit(_twisted("1", name))
    head = repo.commit(_twisted("2", name))  # only TestH2.site's body changes
    plan = _plan(repo, base, head)
    assert selected(plan) == {"tests/test_h2.py::TestH2::test_uses"}
    deps = _deps(_discovered(repo, head), "tests/test_h2.py::TestH2::test_uses")
    assert f"tests.test_h2.TestH2.{name}" in deps
    assert "tests.test_h2.TestH2.site" in deps


@pytest.mark.parametrize(
    "decorator",
    [
        "pytest_asyncio.fixture",
        "pytest_asyncio.fixture(scope='module')",
        "async_fixture",
        "pytest_twisted.async_fixture()",
        "pytest_trio.trio_fixture",
    ],
)
def test_fixture_decorators_ending_in_fixture(repo, decorator):
    """Any decorator whose name ends in ``fixture`` makes a fixture, and its
    ``name=`` and ``autouse=`` keywords are pytest's."""

    def files(value: str) -> dict[str, str]:
        named = f"{decorator.split('(')[0]}(name='client')"
        auto = f"{decorator.split('(')[0]}(autouse=True)"
        return {
            **BENCH,
            "tests/conftest.py": (
                "import pytest\n"
                "import pytest_asyncio\n"
                "import pytest_trio\n"
                "import pytest_twisted\n"
                "from pytest_twisted import async_fixture\n\n\n"
                "@pytest.fixture\n"
                f"def site():\n    return {value}\n\n\n"
                "@pytest.fixture\n"
                "def other_site():\n    return 1\n\n\n"
                f"@{decorator}\n"
                "async def conn(site):\n    return site\n\n\n"
                f"@{named}\n"
                "async def _client(site):\n    return site\n\n\n"
                f"@{auto}\n"
                "async def everywhere(other_site):\n    return other_site\n"
            ),
            "tests/test_x.py": (
                "def test_conn(conn):\n    pass\n\n\n"
                "def test_client(client):\n    pass\n\n\n"
                "def test_plain():\n    pass\n"
            ),
        }

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))  # only site's body changes
    assert selected(_plan(repo, base, head)) == {
        "tests/test_x.py::test_conn",
        "tests/test_x.py::test_client",
    }
    plain = _deps(_discovered(repo, head), "tests/test_x.py::test_plain")
    assert {"tests.conftest.everywhere", "tests.conftest.other_site"} <= set(plain)


# ------------------------------- a well-known name bound to what cannot be read

UNREAD = {
    "class method": (
        "tests/test_h2.py",
        "class TestH2:\n"
        "    @some_lib.thing\n"
        "    def client(self, site):\n"
        "        return site\n\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "module function": (
        "tests/test_h2.py",
        "@some_lib.thing(scope='module')\n"
        "def client(site):\n"
        "    return site\n\n\n"
        "class TestH2:\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "module call": (
        "tests/test_h2.py",
        "client = some_lib.make(lambda site: site)\n\n\n"
        "class TestH2:\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "class attribute": (
        "tests/test_h2.py",
        "class TestH2:\n"
        "    client = some_lib.make(lambda self, site: site)\n\n"
        "    def test_uses(self, client):\n"
        "        pass\n\n"
        "    def test_other(self):\n"
        "        pass\n",
    ),
    "conftest import": (
        "tests/conftest.py",
        "from tests.helpers import client  # noqa: F401\n",
    ),
}


@pytest.mark.parametrize("case", sorted(UNREAD))
def test_well_known_name_bound_in_scope_is_unknown(repo, case):
    """``client`` is pytest-django's, but a definition in scope discovery
    cannot read (an unknown decorator, a call's result) may be the fixture
    pytest finds, requesting what nobody can see: it is ``fixture:client``,
    whose users are selected on every change, and not the plugin's."""
    path, text = UNREAD[case]

    def files(value: str) -> dict[str, str]:
        out = {
            **BENCH,
            "tests/__init__.py": "",
            "tests/conftest.py": (
                f"import pytest\n\n\n@pytest.fixture\ndef site():\n    return {value}\n"
            ),
            "tests/helpers.py": (
                "import some_lib\n\n\n@some_lib.thing\ndef client(site):\n    return site\n"
            ),
            "tests/test_h2.py": (
                "class TestH2:\n"
                "    def test_uses(self, client):\n"
                "        pass\n\n"
                "    def test_other(self):\n"
                "        pass\n"
            ),
        }
        prefix = "import some_lib\n\n\n" if path == "tests/test_h2.py" else ""
        out[path] = prefix + text + (out[path] if path == "tests/conftest.py" else "")
        return out

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))  # only the conftest's site changes
    assert selected(_plan(repo, base, head)) == {"tests/test_h2.py::TestH2::test_uses"}
    result = _discovered(repo, head)
    assert "fixture:client" in _deps(result, "tests/test_h2.py::TestH2::test_uses")
    notes = {(n.kind, n.detail.split("'")[1]) for n in result.notes if "'" in n.detail}
    assert ("unresolved_fixture", "client") in notes
    assert not any(n.kind == "external_fixture" for n in result.notes)


def test_well_known_name_nothing_in_scope_binds_stays_the_plugins(repo):
    """The table still holds for a name nothing in scope binds, or binds to
    data or to an import from outside the source roots (outside code, as
    the plugin's fixture is)."""

    def files(value: str) -> dict[str, str]:
        return {
            **BENCH,
            "tests/conftest.py": (
                "import pytest\n"
                "from django.conf import settings  # noqa: F401\n\n"
                "client = None\n\n\n"
                f"@pytest.fixture\ndef site():\n    return {value}\n"
            ),
            "tests/test_x.py": (
                "def test_django(client, settings, mocker):\n    pass\n\n\n"
                "def test_site(site):\n    pass\n"
            ),
        }

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))
    assert selected(_plan(repo, base, head)) == {"tests/test_x.py::test_site"}
    result = _discovered(repo, head)
    assumed = {n.detail.split("'")[1] for n in result.notes if n.kind == "external_fixture"}
    assert assumed == {"client", "settings", "mocker"}
    assert not any(n.kind == "unresolved_fixture" for n in result.notes)


def test_maybe_indirect_argname_bound_in_scope_is_unknown(repo):
    """An argname maybe handed to a fixture (``indirect=FLAG``) that names
    no fixture discovery reads is a parameter, unless something in scope
    binds it to what may be one."""

    def files(value: str) -> dict[str, str]:
        return {
            **BENCH,
            "tests/conftest.py": (
                f"import pytest\n\n\n@pytest.fixture\ndef site():\n    return {value}\n"
            ),
            "tests/test_x.py": (
                "import pytest\nimport some_lib\n\nFLAG = True\n\n\n"
                "@some_lib.thing\ndef engine(request, site):\n    return site\n\n\n"
                "@pytest.mark.parametrize('engine', [1], indirect=FLAG)\n"
                "def test_engine(engine):\n    pass\n\n\n"
                "@pytest.mark.parametrize('other', [1], indirect=FLAG)\n"
                "def test_other(other):\n    pass\n"
            ),
        }

    base = repo.commit(files("1"))
    head = repo.commit(files("2"))
    assert selected(_plan(repo, base, head)) == {"tests/test_x.py::test_engine"}

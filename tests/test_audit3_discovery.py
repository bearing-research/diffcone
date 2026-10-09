"""Regression scenarios for round 3 of the pre-release audit: static
discovery (internal/audit.md, "Discovery (DSC)", PLN-5 and EVP-5's static
part).

Each test names the finding it guards, and each failed before its fix. The
pytest behaviour each one models was checked against pytest 9's own
``--collect-only`` (and asv_runner's discovery for DSC-2).
"""

from __future__ import annotations

import json

import pytest

from diffcone.cli import main
from diffcone.discovery import (
    DiscoveryOptions,
    discover,
    environment_options,
    pytest_command_arguments,
    relative_arguments,
)
from diffcone.indexer import build_index
from diffcone.report import to_text
from diffcone.snapshot import read_snapshot
from diffcone.testing import asv_target, py_target, rules, selected, unselected

ASV_CONF = '{"version": 1, "benchmark_dir": "benchmarks"}'
BENCH = {
    "asv.conf.json": ASV_CONF,
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "def time_noop():\n    pass\n",
}
BENCH_TARGET = asv_target("bench.time_noop", "benchmarks.bench.time_noop")
T = "def test_{0}():\n    pass\n"


def _plan(repo, base, head, targets=(), **options):
    kwargs = {"discovery_options": DiscoveryOptions(**options)} if options else {}
    return repo.plan(base, head, list(targets), discover_runners=["pytest"], **kwargs)


def _targets(plan) -> set[str]:
    return {t.runner_id for t in plan.targets}


def _discovered(repo, rev, roots=None, **options):
    snap = read_snapshot(repo.path, rev, roots or ["."], with_config=True)
    return discover("pytest", snap, build_index(snap), DiscoveryOptions(**options))


# --------------------------------------------------------------------- DSC-1

AUTO = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef auto_env():\n    {}\n"


@pytest.mark.parametrize(
    "files",
    [
        # by name, by star, as a module attribute, into the test module
        {"conftest.py": "from helpers.fx import auto_env  # noqa\n"},
        {"conftest.py": "from helpers.fx import *  # noqa\n"},
        {"conftest.py": "import helpers.fx\n\nauto_env = helpers.fx.auto_env\n"},
        {"conftest.py": "import helpers.fx as h\n\nrenamed = h.auto_env\n"},
        {"tests/test_a.py": "from helpers.fx import auto_env  # noqa\n\n\n" + T.format("a")},
        # through a re-exporting module, by name and by a two-level star chain
        {"conftest.py": "from helpers.again import auto_env  # noqa\n"},
        {"conftest.py": "from helpers.star import *  # noqa\n"},
        # a pytest_plugins package re-exporting two levels deep
        {"conftest.py": "pytest_plugins = ['helpers.plugin']\n"},
    ],
    ids=["name", "star", "attr", "attr_alias", "test_module", "reexport", "star2", "plugin"],
)
def test_dsc1_an_imported_autouse_fixture_is_a_dependency(repo, files):
    """Nobody requests an autouse fixture by name, so a binding of one was
    never followed: a change to its body selected nothing."""
    base = repo.commit(
        {
            **BENCH,
            "helpers/__init__.py": "",
            "helpers/fx.py": AUTO.format("pass"),
            "helpers/again.py": "from helpers.fx import auto_env  # noqa\n",
            "helpers/star.py": "from helpers.star2 import *  # noqa\n",
            "helpers/star2.py": "from .fx import *  # noqa\n",
            "helpers/plugin/__init__.py": "from .db import *  # noqa\n",
            "helpers/plugin/db.py": "from helpers.star2 import *  # noqa\n",
            "tests/__init__.py": "",
            "tests/test_a.py": T.format("a"),
            "tests/test_b.py": T.format("b"),
            **files,
        }
    )
    head = repo.commit({"helpers/fx.py": AUTO.format("raise RuntimeError('broken')")})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    expected = (
        {"tests/test_a.py::test_a"}
        if "tests/test_a.py" in files
        else {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    )
    assert selected(plan) == expected
    assert "bench.time_noop" in unselected(plan)
    assert not plan.incomplete_discovery


def test_dsc1_an_imported_fixture_that_is_not_autouse_is_still_not_a_dependency(repo):
    """The other side: following bindings for autouse fixtures adds no
    fixture a test does not use."""
    plain = AUTO.replace("autouse=True", "").replace("(\n", "\n")
    base = repo.commit(
        {
            "helpers/__init__.py": "",
            "helpers/fx.py": plain.format("pass"),
            "conftest.py": "from helpers.fx import *  # noqa\n",
            "tests/__init__.py": "",
            "tests/test_a.py": T.format("a"),
            "tests/test_b.py": "def test_b(auto_env):\n    pass\n",
        }
    )
    head = repo.commit({"helpers/fx.py": plain.format("raise RuntimeError('broken')")})
    assert selected(_plan(repo, base, head)) == {"tests/test_b.py::test_b"}


def test_dsc1_an_imported_fixture_with_its_own_name_is_found_under_it(repo):
    """The same gap for ``name=``: the binding's name is not the one tests
    request, and the module's fixture hides the conftest's."""
    named = "import pytest\n\n\n@pytest.fixture(name='db')\ndef real_db():\n    return {}\n"
    base = repo.commit(
        {
            **BENCH,
            "helpers/__init__.py": "",
            "helpers/fx.py": named.format(2),
            "conftest.py": "import pytest\n\n\n@pytest.fixture\ndef db():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_a.py": (
                "from helpers.fx import real_db  # noqa\n\n\ndef test_a(db):\n    assert db == 2\n"
            ),
            "tests/test_b.py": "def test_b(db):\n    assert db == 1\n",
        }
    )
    head = repo.commit({"helpers/fx.py": named.format(3)})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert unselected(plan) == {"tests/test_b.py::test_b", "bench.time_noop"}


# --------------------------------------------------------------------- DSC-2

ASV_CASES = (
    "class Suite:\n"
    "    def {cls}(self):\n"
    "        self.data = list(range({n}))\n\n"
    "    def time_sum(self):\n"
    "        sum(self.data)\n\n\n"
    "def {mod}():\n    pass\n\n\n"
    "def time_env():\n    pass\n"
)


@pytest.mark.parametrize("cls, mod", [("setUp", "SetUp"), ("TearDown", "teardown")])
def test_dsc2_asv_setup_and_teardown_in_any_case_are_dependencies(repo, cls, mod):
    """asv_runner finds ``setup``/``teardown`` ignoring case (``key.lower()
    == name.lower()`` over ``dir(source)``)."""
    test = py_target("tests/test_x.py::test_x", "tests.test_x.test_x")
    files = {
        "asv.conf.json": ASV_CONF,
        "benchmarks/__init__.py": "",
        "tests/__init__.py": "",
        "tests/test_x.py": T.format("x"),
    }
    base = repo.commit({**files, "benchmarks/bench.py": ASV_CASES.format(cls=cls, mod=mod, n=10)})
    head = repo.commit({"benchmarks/bench.py": ASV_CASES.format(cls=cls, mod=mod, n=10**7)})
    plan = repo.plan(base, head, [test], discover_runners=["asv"])
    assert selected(plan) == {"bench.Suite.time_sum"}
    assert unselected(plan) == {"bench.time_env", "tests/test_x.py::test_x"}
    edited = ASV_CASES.format(cls=cls, mod=mod, n=10**7).replace(
        f"def {mod}():\n    pass", f"def {mod}():\n    print()"
    )
    module = repo.commit({"benchmarks/bench.py": edited})
    plan = repo.plan(head, module, [test], discover_runners=["asv"])
    assert selected(plan) == {"bench.Suite.time_sum", "bench.time_env"}


def test_dsc2_asv_builds_the_class_for_every_benchmark(repo):
    """``klass()`` runs ``__init__`` (inherited too) before each benchmark."""
    test = py_target("tests/test_x.py::test_x", "tests.test_x.test_x")
    base_init = "class Base:\n    def __init__(self):\n        self.n = {}\n"
    base = repo.commit(
        {
            "asv.conf.json": ASV_CONF,
            "benchmarks/__init__.py": "",
            "benchmarks/common.py": base_init.format(10),
            "benchmarks/bench.py": (
                "from .common import Base\n\n\nclass Suite(Base):\n"
                "    def time_loop(self):\n        for _ in range(self.n):\n            pass\n\n\n"
                "def time_free():\n    pass\n"
            ),
            "tests/__init__.py": "",
            "tests/test_x.py": T.format("x"),
        }
    )
    head = repo.commit({"benchmarks/common.py": base_init.format(10**7)})
    plan = repo.plan(base, head, [test], discover_runners=["asv"])
    assert selected(plan) == {"bench.Suite.time_loop"}
    assert unselected(plan) == {"bench.time_free", "tests/test_x.py::test_x"}


# --------------------------------------------------------------------- DSC-3

HOOK = "def pytest_collection_modifyitems(config, items):\n    {}\n"
SKIP_ALL = "import pytest\n    for i in items:\n        i.add_marker(pytest.mark.skip)"


@pytest.mark.parametrize(
    "files, args",
    [
        ({}, ("--ignore=tests_legacy",)),
        ({}, ("--ignore", "tests_legacy")),
        ({"pyproject.toml": "[tool.pytest.ini_options]\nnorecursedirs = ['tests_legacy']\n"}, ()),
    ],
    ids=["ignore", "ignore_apart", "norecursedirs"],
)
def test_dsc3_a_test_dir_conftest_loads_at_startup_whatever_ignores_say(repo, files, args):
    """pytest imports ``<initial path>/test*/conftest.py`` before collecting
    (``_set_initial_conftests``), so its session hooks apply to every test."""
    base = repo.commit(
        {
            **BENCH,
            **files,
            "tests/test_a.py": T.format("a"),
            "tests_legacy/conftest.py": HOOK.format("pass"),
            "tests_legacy/test_l.py": T.format("l"),
        }
    )
    head = repo.commit({"tests_legacy/conftest.py": HOOK.format(SKIP_ALL)})
    plan = _plan(repo, base, head, [BENCH_TARGET], runner_args=args)
    assert _targets(plan) == {"tests/test_a.py::test_a", "bench.time_noop"}
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert not plan.incomplete_discovery


def test_dsc3_a_nested_dir_conftest_does_not_load_at_startup(repo):
    """Only ``test*`` directories directly in an initial path load at
    startup: an ignored ``legacy/`` (no ``test`` prefix) stays unloaded."""
    base = repo.commit(
        {
            "tests/test_a.py": T.format("a"),
            "legacy/conftest.py": HOOK.format("pass"),
            "legacy/test_l.py": T.format("l"),
        }
    )
    head = repo.commit({"legacy/conftest.py": HOOK.format(SKIP_ALL)})
    plan = _plan(repo, base, head, runner_args=("--ignore=legacy",))
    assert _targets(plan) == {"tests/test_a.py::test_a"}
    assert selected(plan) == set()


# --------------------------------------------------------------------- DSC-4

AUTO_ENV = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef env():\n    {}\n"


def test_dsc4_norecursedirs_never_applies_to_an_initial_path(repo):
    base = repo.commit(
        {
            **BENCH,
            "pyproject.toml": "[tool.pytest.ini_options]\nnorecursedirs = ['integration']\n",
            "tests/test_a.py": T.format("a"),
            "integration/conftest.py": AUTO_ENV.format("pass"),
            "integration/test_i.py": T.format("i"),
            "integration/integration/test_deeper.py": T.format("deeper"),
        }
    )
    head = repo.commit({"integration/conftest.py": AUTO_ENV.format("raise RuntimeError")})
    plan = _plan(repo, base, head, [BENCH_TARGET], runner_args=("integration",))
    # Below the initial path the rule applies again.
    assert _targets(plan) == {"integration/test_i.py::test_i", "bench.time_noop"}
    assert selected(plan) == {"integration/test_i.py::test_i"}


def test_dsc4_doctests_under_an_initial_path_in_norecursedirs(repo):
    rev = repo.commit(
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\nnorecursedirs = ['tools']\n"
                "addopts = '--doctest-modules'\n"
            ),
            "tools/__init__.py": "",
            "tools/helper.py": 'def f():\n    """\n    >>> f()\n    1\n    """\n    return 1\n',
        }
    )
    plan = _plan(repo, rev, rev, runner_args=("tools",))
    assert _targets(plan) == {"tools/helper.py::tools.helper.f"}
    plan = _plan(repo, rev, rev)
    assert _targets(plan) == set()


# --------------------------------------------------------------------- DSC-5

PLUGIN_FILES = {
    "plugins/__init__.py": "",
    "plugins/auto.py": AUTO_ENV.format("pass"),
    "tests/__init__.py": "",
    "tests/test_a.py": T.format("a"),
}


@pytest.mark.parametrize(
    "options",
    [
        {"env_plugins": ("plugins.auto",)},
        {"env_addopts": ("-p", "plugins.auto")},
        {"command_args": ("-p", "plugins.auto")},
        {"command_args": ("-pplugins.auto",)},
    ],
    ids=["PYTEST_PLUGINS", "PYTEST_ADDOPTS", "command", "command_attached"],
)
def test_dsc5_plugins_loaded_outside_the_configuration_are_dependencies(repo, options):
    base = repo.commit({**BENCH, **PLUGIN_FILES})
    head = repo.commit({"plugins/auto.py": AUTO_ENV.format("raise RuntimeError('broken')")})
    plan = _plan(repo, base, head, [BENCH_TARGET], **options)
    assert selected(plan) == {"tests/test_a.py::test_a"}
    assert "bench.time_noop" in unselected(plan)


def test_dsc5_pytest_addopts_comes_after_addopts_and_before_the_command(repo):
    """pytest's order: ini addopts, PYTEST_ADDOPTS, the command line; a
    path in any of them is an initial path."""
    rev = repo.commit(
        {
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "tests/test_a.py": T.format("a"),
            "other/test_o.py": T.format("o"),
            "more/test_m.py": T.format("m"),
        }
    )
    plan = _plan(repo, rev, rev, env_addopts=("other",), runner_args=("more",))
    assert _targets(plan) == {"other/test_o.py::test_o", "more/test_m.py::test_m"}
    config = plan.discovery[0].config
    assert config["run_arguments"] == {"PYTEST_ADDOPTS": ["other"], "after --": ["more"]}
    text = to_text(plan)
    assert "  read (PYTEST_ADDOPTS): other\n" in text and "  read (after --): more\n" in text


@pytest.mark.parametrize(
    "command, args, problem",
    [
        ("python -m pytest -x -p a.b", ("-x", "-p", "a.b"), ""),
        ("uv run --with pytest-x python -m pytest -q", ("-q",), ""),
        ("coverage run -m pytest tests", ("tests",), ""),
        ("uv run pytest -p a.b", ("-p", "a.b"), ""),
        (".venv/bin/pytest", (), ""),
        ("py.test --lf", ("--lf",), ""),
        ("tox -e py", (), "names no pytest program"),
        ("uv run --with pytest pytest", (), "names pytest more than once"),
    ],
)
def test_dsc5_the_commands_pytest_arguments(command, args, problem):
    found, why = pytest_command_arguments(command.split())
    assert found == args
    assert problem in why and bool(why) == bool(problem)


def test_dsc5_an_unrecognised_command_is_reported(repo):
    rev = repo.commit({"tests/test_a.py": T.format("a")})
    plan = _plan(repo, rev, rev, command_problem="the command 'tox -e py' names no pytest program")
    assert [n.kind for n in plan.incomplete_discovery] == ["unmodelled_runner_option"]


def test_dsc5_the_environment_and_command_reach_discovery_through_the_cli(
    repo, tmp_path, monkeypatch, capsys
):
    base = repo.commit({**PLUGIN_FILES, "plugins/other.py": AUTO_ENV.format("pass")})
    head = repo.commit({"plugins/auto.py": AUTO_ENV.format("raise RuntimeError('broken')")})
    common = ["plan", "--repo", str(repo.path), "--base", base, "--head", head]
    common += ["--discover", "pytest", "--no-cache"]
    for env, command in (
        ({"PYTEST_PLUGINS": "plugins.other, plugins.auto"}, None),
        ({"PYTEST_ADDOPTS": "-p plugins.auto"}, None),
        ({}, "python -m pytest -p plugins.auto"),
    ):
        monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        argv = common + (["--command", command] if command else [])
        assert main(argv) == 0
        data = json.loads(capsys.readouterr().out)
        assert [t["runner_id"] for t in data["selected_targets"]] == ["tests/test_a.py::test_a"]
        used = data["discovery"][0]["config"]["run_arguments"]
        assert used == (
            {"PYTEST_PLUGINS": ["plugins.other", "plugins.auto"]}
            if "PYTEST_PLUGINS" in env
            else {"PYTEST_ADDOPTS": ["-p", "plugins.auto"]}
            if env
            else {"command": ["-p", "plugins.auto"]}
        )


def test_dsc5_environment_options():
    assert environment_options({"PYTEST_ADDOPTS": "-p 'a b'", "PYTEST_PLUGINS": "x,,y "}) == {
        "env_addopts": ("-p", "a b"),
        "env_plugins": ("x", "y"),
    }
    assert environment_options({}) == {"env_addopts": (), "env_plugins": ()}


# --------------------------------------------------------------------- DSC-6

UNIGNORE = (
    "import sys\n\n\ndef pytest_ignore_collect(collection_path, config):\n"
    "    if sys.version_info < (3, 8) and 'py38' in str(collection_path):\n"
    "        return True\n"
    "    return {}\n"
)


@pytest.mark.parametrize(
    "files, args",
    [
        ({}, ("--ignore=tests/test_s3.py",)),
        (
            {
                "pyproject.toml": (
                    "[tool.pytest.ini_options]\naddopts = '--ignore-glob=tests/*_s3.py'\n"
                )
            },
            (),
        ),
    ],
    ids=["ignore", "ignore_glob"],
)
def test_dsc6_an_ignore_hook_returning_false_overrides_ignores(repo, files, args):
    """pytest_ignore_collect is firstresult and conftests come first: False
    collects what --ignore would skip."""
    base = repo.commit(
        {
            **BENCH,
            **files,
            "conftest.py": UNIGNORE.format("False"),
            "tests/test_a.py": T.format("a"),
            "tests/test_s3.py": "from lib import f\n\n\ndef test_s3():\n    assert f()\n",
            "lib.py": "def f():\n    return 1\n",
        }
    )
    head = repo.commit({"lib.py": "def f():\n    return 0\n"})
    plan = _plan(repo, base, head, [BENCH_TARGET], runner_args=args)
    assert selected(plan) == {"tests/test_s3.py::test_s3"}
    # A hook that only ever says True (or nothing) leaves the ignores alone.
    quiet = repo.commit({"conftest.py": UNIGNORE.format("None")})
    plan = _plan(repo, quiet, quiet, runner_args=args)
    assert _targets(plan) == {"tests/test_a.py::test_a"}


def test_dsc6_an_ignore_hook_applies_below_its_conftest_only(repo):
    rev = repo.commit(
        {
            "a/conftest.py": UNIGNORE.format("False"),
            "a/test_x.py": T.format("ax"),
            "b/test_x.py": T.format("bx"),
            "a/test_y.py": T.format("ay"),
            "b/test_y.py": T.format("by"),
        }
    )
    plan = _plan(repo, rev, rev, runner_args=("--ignore=a/test_y.py", "--ignore=b/test_y.py"))
    assert _targets(plan) == {
        "a/test_x.py::test_ax",
        "b/test_x.py::test_bx",
        "a/test_y.py::test_ay",
    }


def test_dsc6_an_ignore_hook_in_a_plugin_is_reported(repo):
    rev = repo.commit(
        {
            "plug.py": UNIGNORE.format("False"),
            "conftest.py": "pytest_plugins = ['plug']\n",
            "tests/test_a.py": T.format("a"),
        }
    )
    plan = _plan(repo, rev, rev)
    assert [(n.kind, n.path) for n in plan.incomplete_discovery] == [
        ("plugin_collects_files", "plug.py")
    ]


# --------------------------------------------------------------------- DSC-7


def test_dsc7_dot_is_an_initial_path(repo):
    base = repo.commit(
        {
            **BENCH,
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "tests/test_a.py": T.format("a"),
            "other/__init__.py": "",
            "other/lib.py": "def f():\n    return 1\n",
            "other/test_o.py": "from other.lib import f\n\n\ndef test_o():\n    assert f() == 1\n",
        }
    )
    head = repo.commit({"other/lib.py": "def f():\n    return 2\n"})
    for dot in (".", "./"):
        plan = _plan(repo, base, head, [BENCH_TARGET], runner_args=(dot,))
        assert selected(plan) == {"other/test_o.py::test_o"}, dot
        assert "tests/test_a.py::test_a" in unselected(plan)
        assert not plan.incomplete_discovery


# --------------------------------------------------------------------- DSC-8

DOC = "Example\n=======\n\n>>> 1 + 1\n2\n"


def test_dsc8_a_text_file_named_as_an_argument_is_a_doctest(repo):
    """pytest's ``_is_doctest``: a .txt/.rst initial path is a doctest
    whatever --doctest-glob says."""
    rev = repo.commit(
        {"tests/test_a.py": T.format("a"), "docs/guide.rst": DOC, "docs/other.rst": DOC}
    )
    plan = _plan(repo, rev, rev, runner_args=("tests", "docs/guide.rst"))
    assert _targets(plan) == {"tests/test_a.py::test_a", "docs/guide.rst::guide.rst"}
    assert "docs/guide.rst::guide.rst" in selected(plan)  # its content is not analysed


def test_dsc8_a_doctest_glob_with_a_directory_matches_the_path(repo):
    """``fnmatch_ex``: a glob with a ``/`` matches the path, not the name."""
    rev = repo.commit(
        {"tests/test_a.py": T.format("a"), "docs/guide.rst": DOC, "other/guide.rst": DOC}
    )
    plan = _plan(repo, rev, rev, runner_args=("--doctest-glob=docs/*.rst",))
    assert _targets(plan) == {"tests/test_a.py::test_a", "docs/guide.rst::guide.rst"}


# --------------------------------------------------------------------- DSC-9


@pytest.mark.parametrize(
    "files, args",
    [
        ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--ignore tests/slow'\n"}, ()),
        ({}, ("--ignore", "tests/slow")),
        ({}, ("--ignore-glob", "tests/slow/*")),
        ({}, ("--deselect", "tests/slow/test_b.py::test_b", "--ignore=tests/slow")),
        ({}, ("--doctest-glob", "tests/*.txt", "--ignore=tests/slow")),
    ],
)
def test_dsc9_an_option_value_written_apart_is_not_a_path(repo, files, args):
    """The value of an option that always takes one is no initial path, and
    no ambiguity: the plan is complete and run does not refuse."""
    rev = repo.commit(
        {
            **files,
            "tests/test_a.py": T.format("a"),
            "tests/slow/__init__.py": "",
            "tests/slow/test_b.py": T.format("b"),
        }
    )
    plan = _plan(repo, rev, rev, runner_args=args)
    assert _targets(plan) == {"tests/test_a.py::test_a"}
    assert not plan.incomplete_discovery


# --------------------------------------------------------------------- DSC-10


def test_dsc10_an_absolute_ignore_path_inside_the_repository(repo, capsys):
    rev = repo.commit(
        {
            "tests/test_a.py": T.format("a"),
            "tests/slow/__init__.py": "",
            "tests/slow/test_b.py": T.format("b"),
        }
    )
    slow = repo.path.resolve() / "tests" / "slow"
    for args in ([f"--ignore={slow}"], ["--ignore", str(slow)]):
        argv = ["plan", "--repo", str(repo.path), "--base", rev, "--head", rev]
        assert main([*argv, "--discover", "pytest", "--no-cache", "--", *args]) == 0
        data = json.loads(capsys.readouterr().out)
        ids = {t["runner_id"] for t in data["unselected_targets"] + data["selected_targets"]}
        assert ids == {"tests/test_a.py::test_a"}, args


def test_dsc10_relative_arguments(tmp_path):
    inside = tmp_path / "tests" / "x.py"
    assert relative_arguments(
        [f"--ignore={tmp_path / 'tests'}", str(inside), str(tmp_path), "/elsewhere", "-x"],
        tmp_path,
    ) == ("--ignore=tests", "tests/x.py", ".", "/elsewhere", "-x")


# ------------------------------------------------------- -o overrides (DSC-11)


@pytest.mark.parametrize(
    "files, args, expected",
    [
        # ``-o addopts=`` on the command line drops the configured addopts
        # (collection_check.py --clean-addopts collects so).
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--ignore=tests/slow'\n"},
            ("-o", "addopts="),
            {"tests/test_a.py::test_a", "tests/slow/test_b.py::test_b"},
        ),
        (
            {
                "pyproject.toml": (
                    "[tool.pytest.ini_options]\naddopts = '-o python_files=check_*.py'\n"
                )
            },
            (),
            {"tests/check_c.py::test_c"},
        ),
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests/slow']\n"},
            ("--override-ini=testpaths=tests",),
            {"tests/test_a.py::test_a", "tests/slow/test_b.py::test_b"},
        ),
        # A path in addopts is an initial path like one on the command line.
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = 'tests/check_c.py'\n"},
            (),
            {"tests/check_c.py::test_c"},
        ),
    ],
    ids=["clear_addopts", "python_files", "testpaths", "addopts_path"],
)
def test_dsc11_ini_overrides_and_addopts_paths_are_modelled(repo, files, args, expected):
    rev = repo.commit(
        {
            **files,
            "tests/test_a.py": T.format("a"),
            "tests/check_c.py": T.format("c"),
            "tests/slow/__init__.py": "",
            "tests/slow/test_b.py": T.format("b"),
        }
    )
    plan = _plan(repo, rev, rev, runner_args=args)
    assert _targets(plan) == expected
    assert not plan.incomplete_discovery


def test_dsc11_a_path_inside_another_initial_path_is_dropped(repo):
    """pytest keeps initial paths prefix-free: ``tests/legacy/test_l.py``
    inside ``tests`` is no initial path, so --ignore=tests/legacy applies."""
    rev = repo.commit(
        {
            "tests/test_a.py": T.format("a"),
            "tests/legacy/test_l.py": T.format("l"),
            "tests/legacy/test_m.py": T.format("m"),
        }
    )
    args = ("tests", "tests/legacy/test_l.py", "--ignore=tests/legacy")
    assert _targets(_plan(repo, rev, rev, runner_args=args)) == {"tests/test_a.py::test_a"}
    args = ("tests/legacy/test_l.py", "--ignore=tests/legacy")
    assert _targets(_plan(repo, rev, rev, runner_args=args)) == {"tests/legacy/test_l.py::test_l"}


# --------------------------------------------------------------------- PLN-5

OUTSIDE_PLUGIN = (
    "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _env(monkeypatch):\n"
    "    monkeypatch.setenv('X', '{}')\n"
)


@pytest.mark.parametrize(
    "files, options",
    [
        ({"tests/conftest.py": "pytest_plugins = ['support.plugin']\n"}, {}),
        ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-p support.plugin'\n"}, {}),
        ({}, {"env_plugins": ("support.plugin",)}),
    ],
    ids=["pytest_plugins", "addopts_p", "PYTEST_PLUGINS"],
)
def test_pln5_a_plugin_in_the_repository_outside_the_roots_is_unknown(repo, files, options):
    base = repo.commit(
        {
            **BENCH,
            "src/pkg/__init__.py": "",
            "src/pkg/ops.py": (
                "import os\n\n\ndef add(a, b):\n    return a + b + int(os.environ['X'])\n"
            ),
            "tests/test_ops.py": (
                "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
            ),
            "support/__init__.py": "",
            "support/plugin.py": OUTSIDE_PLUGIN.format(0),
            **files,
        }
    )
    head = repo.commit({"support/plugin.py": OUTSIDE_PLUGIN.format(1)})
    roots = ["src", "tests", "benchmarks=benchmarks"]
    plan = repo.plan(
        base,
        head,
        [BENCH_TARGET],
        source_roots=roots,
        discover_runners=["pytest"],
        discovery_options=DiscoveryOptions(**options),
    )
    assert selected(plan) == {"tests/test_ops.py::test_add"}
    assert "bench.time_noop" in unselected(plan)
    notes = [n for d in plan.discovery for n in d.notes if n.kind == "plugin_outside_roots"]
    assert [n.path for n in notes] == ["support/plugin.py"]


# --------------------------------------------------------------------- EVP-5

FLAG_PLUGIN = (
    "import pytest\n\nfrom pkg.flags import FLAGS\n\n\n@pytest.fixture(autouse=True)\n"
    "def _turn_on():\n    FLAGS['on'] = True\n    yield\n    FLAGS.clear()\n"
)
FLAG_FILES = {
    "pkg/__init__.py": "",
    "pkg/flags.py": "FLAGS = {}\n\n\ndef flag():\n    return FLAGS.get('on', False)\n",
    "tests/__init__.py": "",
    "tests/plugin.py": FLAG_PLUGIN,
    "tests/test_a.py": T.format("a"),
    "tests/test_b.py": "from pkg.flags import flag\n\n\ndef test_b():\n    assert not flag()\n",
}
REGISTERS = 'pytest_plugins = ["tests.plugin"]\n\n\n' + T.format("a")


@pytest.mark.parametrize("how", ["added", "edited", "removed"])
def test_evp5_registering_a_plugin_in_one_module_reaches_every_test(repo, how):
    """``pytest_plugins`` in a test module registers the plugin for the
    session: its autouse fixture now applies to test_b, whose lifecycle
    dependencies grow though nothing it depends on changed."""
    files = dict(FLAG_FILES)
    if how == "edited":
        files["tests/test_a.py"] = "pytest_plugins = []\n\n\n" + T.format("a")
    if how == "removed":
        files["tests/test_a.py"] = REGISTERS
    base = repo.commit({**BENCH, **files})
    head = repo.commit({"tests/test_a.py": T.format("a") if how == "removed" else REGISTERS})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert "lifecycle_changed" in rules(plan, "tests/test_b.py::test_b")
    assert "bench.time_noop" in unselected(plan)


def test_evp5_a_plugin_modules_import_time_code_reaches_every_test(repo):
    """A plugin module runs once per session, before any test: its
    import-time code reaches every test, even one with no fixture of it."""
    env = "import os\n\nos.environ['MODE'] = '{}'\n"
    base = repo.commit(
        {
            **BENCH,
            "tests/__init__.py": "",
            "tests/setenv.py": env.format("slow"),
            "tests/conftest.py": "pytest_plugins = ['tests.setenv']\n",
            "tests/test_a.py": T.format("a"),
            "tests/test_b.py": (
                "import os\n\n\ndef test_b():\n    assert os.environ['MODE'] == 'slow'\n"
            ),
        }
    )
    head = repo.commit({"tests/setenv.py": env.format("fast")})
    plan = _plan(repo, base, head, [BENCH_TARGET])
    assert selected(plan) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert "bench.time_noop" in unselected(plan)

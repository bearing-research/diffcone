# Static discovery

`--discover pytest` and `--discover asv` find targets by reading your
configuration and test files, without importing them. This page lists what
each one understands.

## pytest

**Configuration** is read from `pytest.toml`, `.pytest.toml`,
`pytest.ini`, `.pytest.ini`, `pyproject.toml` (`[tool.pytest.ini_options]`
or `[tool.pytest]`), `tox.ini` or `setup.cfg`, in pytest's order:
`testpaths`,
`python_files`, `python_classes`, `python_functions`, `norecursedirs`,
`usefixtures`, and the doctest options in `addopts`.

**Tests** found:

- test functions and methods, following your naming settings;
- test classes (without an `__init__`, unless they are
  `unittest.TestCase` subclasses), including nested classes and nested
  classes inherited from a base class;
- `unittest.TestCase` subclasses, whatever their names;
- tests inherited from base classes, including base classes in other
  modules;
- tests imported into a test module, by name or with `from module import *`,
  including tests the other module imported itself;
- tests bound to another name (`test_alias = test_orig`), `runTest` in a
  `unittest.TestCase` without `test*` methods, and functions marked
  `__test__ = True`;
- doctests, when `--doctest-modules` or `--doctest-glob` is set.

**What each test depends on**, besides its own code:

- its fixtures, requested by parameter, `usefixtures` (the mark or the
  configuration option) or `request.getfixturevalue` (every fixture it can
  see, when the name isn't written out), and the fixtures
  those use, resolved the way pytest does: class (including the fixtures
  it inherits from base classes in other modules), then module, then each
  `conftest.py` outward, then plugins listed in `pytest_plugins` (in any
  conftest or test module, and the plugins those list) and the pytest
  plugins of your project and of other packages in the repository. A
  fixture is found under every name a module
  binds it to, as in pytest: an alias (`box2 = box`) or an import (`from
  tests.helpers import engine`), unless the fixture sets its own `name=`.
  A name the test parametrizes is a parameter, not a fixture, for the test
  and for every fixture it uses;
- marks such as `usefixtures` and `parametrize` on its class and the
  class's base classes, including marks stored in a variable and applied
  by name (`needs_db = pytest.mark.usefixtures("db")`, then `@needs_db`);
- autouse fixtures that apply to it, including ones whose `autouse=` is a
  variable;
- xunit-style `setup_*` and `teardown_*` functions, and unittest's
  `setUpModule`, `setUp`, `asyncSetUp`, `setUpClass` and their teardowns;
- its module, and every `conftest.py` on its path with their `pytest_*`
  hooks, including hooks they import;
- hooks that run for the whole session, such as
  `pytest_collection_modifyitems` and `pytest_configure`, in any
  `conftest.py` and in your plugins.

A doctest depends on everything its module can reach, and on the fixtures
pytest gives it: autouse fixtures (such as one that fills
`doctest_namespace`) and the `usefixtures` option. A doctest in a text
file is always selected.

A `conftest.py` outside your source roots can't be read, so the tests under
it are always selected and the report says why; add a source root that
contains it. A change to your runner configuration or build script
(`pyproject.toml`, `tox.ini`, `setup.cfg`, `setup.py`, a `conftest.py`,
`asv.conf.json`) outside your source roots selects every target.

### Fixtures from installed plugins

A fixture that isn't defined in your source roots usually comes from an
installed plugin. diffcone recognises the fixtures of well-known plugins
(`mocker`, `httpx_mock`, `freezer`, `anyio_backend`, `benchmark` and
others), and lists each one it assumes in the report. Name fixtures from
other plugins with `--assume-external-fixture NAME`. A fixture diffcone
can't find otherwise is reported, and the tests that use it are selected
on every change, since diffcone can't tell what the fixture depends on.

Some fixtures are requested by pytest or a plugin itself rather than by
your tests: pytest's `tmp_path` uses `tmp_path_factory`, and anyio's plugin
uses `anyio_backend`. If your project overrides one, diffcone counts the
override as a dependency of the tests that use it: for pytest's fixtures,
the tests that use the fixture requesting it; for a plugin's, every test
that can see the override.

### Parametrized tests

Each test is a single target: diffcone selects or skips all of its
parameter cases together.

## ASV

`benchmark_dir` is read from `asv.conf.json`. Benchmarks are the
functions and methods whose names start with `time_`, `timeraw_`, `mem_`,
`peakmem_` or `track_` (or `Time`, `Timeraw`, `Mem`, `PeakMem`, `Track`
followed by a capital), including inherited ones and ones imported into a
benchmark module, in every module under `benchmark_dir` as ASV walks it,
named as ASV names them (a `benchmark_name` you set is used). Each depends
on its class's and module's `setup`, `setup_cache` and `teardown` (also
when imported from another module), and on its module. Class attributes
such as `params` count as part of the class. A `timeraw_` benchmark also
depends on the modules its code imports; if its code isn't a plain string,
it is always selected.

## When the target list may be short

Some things can't be known by reading the source:

- a pytest plugin that collects tests by its own rules, such as classes
  with a different naming pattern;
- a test base class, or an imported test, from outside your source roots;
- a test file, benchmark file or doctest module outside your source roots
  (with `--source-root src`, add `--source-root .` for tests in `tests/`);
- options in `addopts` that change what pytest collects (`-o`, `-c`,
  `--rootdir`, `--pyargs`, or a path to test);
- a test bound to something discovery can't follow
  (`TestMachine = Machine.TestCase`);
- a plugin your configuration turns on that collects files of its own
  (pytest-typing's `typing_checkers`, `--nbval`, `--markdown-docs`);
- a test file that can't be parsed.

Each one appears as a note in the report's `discovery` section, and the
plan exits with code `3`: running only the selected tests could skip
tests your runner would collect. You can:

- widen your source roots so the missing code is analysed;
- list the missing tests in a [manifest](manifest.md); or
- use [execution evidence](../guides/evidence.md): a recording captures
  what pytest really collected, which settles these notes when nothing was
  missed.

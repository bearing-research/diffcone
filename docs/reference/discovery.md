# Static discovery

`--discover pytest` and `--discover asv` find targets by reading your
configuration and test files, without importing them. This page lists what
each one understands.

## pytest

**Configuration** is read from `pytest.ini`, `pyproject.toml`
(`[tool.pytest.ini_options]`), `tox.ini` or `setup.cfg`: `testpaths`,
`python_files`, `python_classes`, `python_functions`, `norecursedirs`, and
the doctest options in `addopts`.

**Tests** found:

- test functions and methods, following your naming settings;
- test classes (without an `__init__`), including nested classes;
- `unittest.TestCase` subclasses, whatever their names;
- tests inherited from base classes, including base classes in other
  modules;
- tests imported into a test module with `from module import *`;
- doctests, when `--doctest-modules` or `--doctest-glob` is set.

**What each test depends on**, besides its own code:

- its fixtures, requested by parameter or `usefixtures`, and the fixtures
  those use, resolved the way pytest does: class (including the fixtures
  it inherits from base classes in other modules), then module, then each
  `conftest.py` outward, then plugins listed in `pytest_plugins` (and the
  plugins those list) and your project's own pytest plugins. A fixture is found under every name a module
  binds it to, as in pytest: an alias (`box2 = box`) or an import (`from
  tests.helpers import engine`), unless the fixture sets its own `name=`.
  A name the test parametrizes is a parameter, not a fixture, for the test
  and for every fixture it uses;
- marks such as `usefixtures` and `parametrize` on its class and the
  class's base classes, including marks stored in a variable and applied
  by name (`needs_db = pytest.mark.usefixtures("db")`, then `@needs_db`);
- autouse fixtures that apply to it, including ones whose `autouse=` is a
  variable;
- xunit-style `setup_*` and `teardown_*` functions;
- its module, and every `conftest.py` on its path with their `pytest_*`
  hooks.

A doctest depends on everything its module can reach; a doctest in a text
file is always selected.

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
`peakmem_` or `track_`, including inherited ones. Each depends on its
class's and module's `setup`, `setup_cache` and `teardown`, and on its
module. Class attributes such as `params` count as part of the class.

## When the target list may be short

Some things can't be known by reading the source:

- a pytest plugin that collects tests by its own rules, such as classes
  with a different naming pattern;
- a test base class, or an imported test, from outside your source roots;
- a test file that can't be parsed.

Each one appears as a note in the report's `discovery` section, and the
plan exits with code `3`: running only the selected tests could skip
tests your runner would collect. You can:

- widen your source roots so the missing code is analysed;
- list the missing tests in a [manifest](manifest.md); or
- use [execution evidence](../guides/evidence.md): a recording captures
  what pytest really collected, which settles these notes when nothing was
  missed.

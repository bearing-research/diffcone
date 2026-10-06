# Static discovery

Discovery turns the head snapshot into targets without importing or
running project code: it reproduces a documented subset of each runner's
collection rules from the source, and reports what it cannot resolve
instead of guessing. The full rules are in the design's
[pytest](../design.md#pytest) and [ASV](../design.md#asv) sections.

## pytest

- **Configuration** from `pytest.ini`, `pyproject.toml`
  (`[tool.pytest.ini_options]`), `tox.ini` or `setup.cfg`: `python_files`,
  `python_classes`, `python_functions`, `testpaths`, `norecursedirs`, and
  doctest options in `addopts`.
- **Tests:** test functions, `Test*` classes without `__init__`, nested
  classes, `unittest.TestCase` methods whatever the class is named, tests
  inherited from base classes (in other modules too), and test suites
  re-run through `from module import *`.
- **Lifecycle dependencies** of each test: its fixtures (by parameter, by
  `usefixtures`, transitively), resolved as pytest resolves them (class,
  module, nearest `conftest.py` outward, `pytest_plugins` modules and the
  project's own `pytest11` entry-point plugins in the source roots);
  autouse fixtures; xunit setup functions; its module; and every
  `conftest.py` on its path with their `pytest_*` hooks.
- **Fixtures it cannot find:** assumed to come from an installed plugin
  when a well-known plugin provides them (`mocker`, `httpx_mock`,
  `freezer`, `anyio_backend`, `benchmark`, ...; each assumption is listed
  in the report; `--no-well-known-fixtures` turns this off) or when named
  with `--assume-external-fixture NAME`. Any other becomes the dependency
  `fixture:<name>`, which selects the test conservatively.
- **Doctests**, as pytest collects them (`--doctest-modules`,
  `--doctest-glob`): a docstring's examples depend on everything its
  module's globals can reach, and a text-file doctest is always selected.

Parameter cases (`parametrize`, `pytest_generate_tests`) are not separate
targets: a test is selected as a whole.

## ASV

`benchmark_dir` from `asv.conf.json`; `time_`, `timeraw_`, `mem_`,
`peakmem_` and `track_` functions and methods, inherited ones included.
Their lifecycle dependencies are the class's and module's `setup`,
`setup_cache` and `teardown` and the module itself; class attributes such
as `params` reach the benchmarks through the class body.

## When the target list may be short

Some things discovery cannot know: what a pytest plugin collects by its own
rules (SQLAlchemy's testing plugin collects classes named `<Name>Test`), a
base class or imported test outside the source roots, a file that cannot be
parsed. Each is a note in the report, and together they make the plan exit
`3`, since running only the selected targets could skip tests the runner
collects. With [execution evidence](../guides/evidence.md), a recording of
the real collection can settle a note.

To compare discovery with what pytest really collects in your project, run
`scripts/collection_check.py --repo DIR --command CMD` from a checkout of
diffcone.

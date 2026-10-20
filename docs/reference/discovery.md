# Static discovery

`--discover pytest` and `--discover asv` find targets by reading your
configuration and test files, without importing them. This page lists what
each one understands.

## pytest

**Configuration** is read from `pytest.toml`, `.pytest.toml`,
`pytest.ini`, `.pytest.ini`, `pyproject.toml` (`[tool.pytest.ini_options]`
or `[tool.pytest]`), `tox.ini` or `setup.cfg` at the root of the
repository, in pytest's order: `testpaths`, `python_files`,
`python_classes`, `python_functions`, `norecursedirs`, `usefixtures`, and
the options in `addopts`. pytest reads options from three more places, and
so does discovery, in pytest's order: the `PYTEST_ADDOPTS` environment
variable, the pytest arguments written into `--command` (`uv run pytest -p
tests.plugin`), and the arguments you pass after `--`. Plugins named in
`PYTEST_PLUGINS` are loaded too.

Planning reads the environment when it runs, so plan in the environment
the tests run in. The report's `discovery` section lists what it read
(`run_arguments`), and pytest's `rootdir`, `configfile` and
`--junit-prefix` (`junit_prefix`) as they follow from the configuration
and arguments; `diffcone check` uses those to match JUnit names to tests.

Among those options:

- test files or directories (`.` included) replace `testpaths`, as they do
  for pytest; a `.txt` or `.rst` file named there is a doctest;
- `--ignore` and `--ignore-glob` leave paths out, and `norecursedirs` skips
  directories, below the paths pytest starts from, never those paths
  themselves; a `pytest_ignore_collect` hook in a `conftest.py` that can
  return `False` overrides both under its directory, so they are not
  applied there;
- `-o name=value` overrides the configuration (`-o addopts=` drops the
  configured `addopts`);
- an absolute path inside the repository counts as the relative one.

**Tests** found:

- test functions and methods, following your naming settings;
- test classes, including nested classes and nested classes inherited
  from a base class. As in pytest, a class with a constructor is not
  collected unless it is a `unittest.TestCase` subclass: its own or an
  inherited `__init__` or `__new__`, or one a `@dataclass` or attrs
  decorator generates;
- `unittest.TestCase` subclasses, whatever their names;
- tests inherited from base classes, including base classes in other
  modules;
- tests imported into a test module, by name or with `from module import *`,
  including tests the other module imported itself or defines inside an
  `if` or `try` block;
- tests bound to another name (`test_alias = test_orig`, also inside an
  `if` block), `runTest` in a `unittest.TestCase` without `test*` methods,
  and functions marked `__test__ = True`;
- doctests: text files matching `--doctest-glob` (by default
  `test*.txt`), and the docstrings in your modules when
  `--doctest-modules` is set.

**What each test depends on**, besides its own code:

- its fixtures, and the fixtures those use:
    - A fixture is requested by parameter, by `usefixtures` (the mark,
      the configuration option, or a hook that adds to either:
      `config.addinivalue_line("usefixtures", ...)`,
      `item.fixturenames.append(...)`), by `request.getfixturevalue`
      (also through an alias or `getattr`, and in a helper the test or
      fixture hands `request` to), or by pytest-lazy-fixtures' `lf()` and
      pytest-lazy-fixture's `lazy_fixture()` in parametrize values. When
      the name isn't written out, or `request` goes to code diffcone
      can't read, every fixture the test can see counts.
    - It is looked up the way pytest does: the class (including fixtures
      inherited from base classes in other modules), the module, each
      `conftest.py` outward, then plugins. Plugins are those listed in
      `pytest_plugins` (in any conftest or test module, and the plugins
      those list) and the pytest plugins of your project and of other
      packages in the repository. A `pytest_plugins` list may be built
      with `+`, `+=`, `.append` or `.extend`, or imported from another
      module.
    - A fixture is found under every name a module binds it to, as in
      pytest: an alias (`box2 = box`), an import (`from tests.helpers
      import engine`, `from tests.helpers import *`, through any number of
      modules) or a module attribute (`engine = tests.helpers.engine`),
      unless the fixture sets its own `name=`. When two plugins define a
      fixture of the same name, both count.
    - A name the test parametrizes is a parameter, not a fixture, for the
      test and for every fixture it uses, unless `indirect` hands it to
      the fixture. When `indirect` is a variable rather than `True`,
      `False` or a list of names, every name that has a fixture counts as
      handed over.
- for a test that uses `pytester` or `testdir`, every fixture of the
  session's plugins (entry points, `-p`, `PYTEST_PLUGINS`,
  `pytest_plugins`): the pytest session it runs loads them, and its inline
  test code may request any of them. What that inline code imports is
  followed too;
- marks such as `usefixtures` and `parametrize` on its class and the
  class's base classes, including marks stored in a variable and applied
  by name (`needs_db = pytest.mark.usefixtures("db")`, then `@needs_db`),
  and `pytestmark` lists built with `+`, `+=`, `.append` or `.extend`. A
  `usefixtures` whose names aren't written out (`usefixtures(NAME)`), or
  one applied by a decorator, mark factory or function whose code mentions
  `usefixtures` (`@needs_db` defined as a function, `pytestmark =
  marks()`), counts as every fixture the test can see;
- autouse fixtures that apply to it, including ones whose `autouse=` is a
  variable and ones a `conftest.py`, test module or plugin imports;
- xunit-style `setup_*` and `teardown_*` functions, and unittest's
  `setUpModule`, `setUp`, `asyncSetUp`, `setUpClass` and their teardowns;
- its module, and every `conftest.py` on its path with their `pytest_*`
  hooks, including hooks they import;
- hooks that run for the whole session, such as
  `pytest_collection_modifyitems` and `pytest_configure`, in any
  `conftest.py` pytest loads (including the one in a `test*` directory of a
  path pytest starts from, which it loads even when that directory is
  ignored) and in your plugins;
- your plugins' modules, which load for the whole session;
- the `pytest_*` methods of any class in your source roots, since pytest
  calls them for every test once the class is registered as a plugin
  (`config.pluginmanager.register(MyPlugin())`), and the hooks of a
  module registered by name (`config.pluginmanager.import_plugin(
  "tests.plugin")`);
- code pytest runs while it collects, before any test, that changes the
  process for every test after it: a `conftest.py`, a test module or a
  module they import that sets an environment variable or extends
  `sys.path` when imported, or a `pytest_generate_tests` in a
  sub-directory's `conftest.py` that does (see below for what counts).

Changes to the process that diffcone recognises:

- writes to `os.environ`, `sys.path` and `sys.modules`;
- attributes set on third-party modules;
- common configuration calls such as `warnings.filterwarnings`,
  `logging.basicConfig`, `locale.setlocale`, `numpy.random.seed` and
  hypothesis's `settings.load_profile`.

They count whether made directly or through a patch that is not undone
when a test ends (`mock.patch.dict(os.environ, ...).start()`,
`pytest.MonkeyPatch().setenv(...)`). State a library keeps behind its own
configuration function is not recognised; declare such a dependency in
[`diffcone.toml`](declarations.md) if a test relies on it.

A test is also selected when what runs around it changes although its own
code did not: for example, a test module adds a plugin to
`pytest_plugins`, whose autouse fixture then applies to every test.

A doctest depends on everything its module can reach, and on the fixtures
pytest gives it: autouse fixtures (such as one that fills
`doctest_namespace`) and the `usefixtures` option. A module such a fixture
stores into `doctest_namespace` (`doctest_namespace["np"] = numpy`, or
with `.update` or `.setdefault`) counts too, with everything it imports. A
doctest in a text file is always selected.

A docstring set when the module is imported is read when diffcone can
work out its text: a string, a module-level string built with `+`, `%`,
f-strings or methods such as `.format` and `.replace`, or another
function's docstring (`b.__doc__ = a.__doc__`). A docstring diffcone
can't work out (one a decorator of yours rewrites, or `f.__doc__ = DOC`
with `DOC` imported from elsewhere) makes its doctest always selected when
the docstring written in the code has examples; when it has none, whether
pytest finds a doctest there is unknown, so it is reported.

A `conftest.py` outside your source roots can't be read, so the tests under
it are always selected and the report says why; add a source root that
contains it. The same goes for a plugin in your repository that your source
roots don't hold (`pytest_plugins = ["support.plugin"]` with `support/`
outside them): every test is always selected. And when diffcone can't read
which plugins `pytest_plugins` names (it is built by a function call or a
comprehension, or imported from outside your source roots), every test is
always selected too; write the list out as strings to avoid that.

Some files can change what any test does, so a change to one selects every
target. Outside your source roots, these are:

- your runner configuration or build script: `pyproject.toml`, `tox.ini`,
  `setup.cfg`, `setup.py`, `noxfile.py`, `hatch.toml`, a `conftest.py`,
  `asv.conf.json` or `asv.conf.jsonc`;
- your dependencies: a lock file such as `uv.lock`, any file or directory
  named for requirements, constraints or deps (`dev-requirements.txt`,
  `ci/deps/py311.yaml`), a conda `environment*.yml`, and installed package
  metadata (a `*.dist-info` or `*.egg-info` directory);
- your CI configuration (`.github/workflows/`, `ci/`);
- a `.env` or `.pth` file, or `sitecustomize.py`;
- a type checker or linter configuration that a pytest plugin may run
  (`mypy.ini`, `ruff.toml`);
- compiled sources.

Inside your source roots, a change to any file that isn't a `.py` file
selects every target: a data file, a `README.md`, a configuration file. So
does a change to a file's mode (a script made executable), and a change to
a build script or `sitecustomize.py`, wherever it sits. `diffcone.toml` and
`.diffcone/` are the exceptions. With the source root `.`, every file in
the repository is under a root (see
[limitations](../limitations.md#what-a-change-reaches)).

Other files outside the roots are assumed not to affect the tests: if your
tests read one, put it under a source root.

### Fixtures from installed plugins

A fixture that isn't defined in your source roots usually comes from an
installed plugin. diffcone recognises the fixtures of well-known plugins
(`mocker`, `httpx_mock`, `freezer`, `anyio_backend`, `benchmark` and
others), and lists each one it assumes in the report. Name fixtures from
other plugins with `--assume-external-fixture NAME`. A fixture diffcone
can't find otherwise is reported, and the tests that use it are selected
on every change, since diffcone can't tell what the fixture depends on.

A fixture is a function under a decorator whose name ends in `fixture`:
`pytest.fixture`, pytest-asyncio's `fixture`, pytest-twisted's
`async_fixture` and `async_yield_fixture`, pytest-trio's `trio_fixture`,
also under another name the module gives it (`from pytest import fixture
as fx`, `fx = pytest.fixture`).
A plugin's fixture name is assumed only when nothing your tests can see
defines it. If a test class, test module, conftest or plugin module defines
that name under some other decorator, or binds it to something diffcone
can't read (`client = make_fixture(...)`), diffcone treats it as an unknown
fixture instead, and selects the tests that use it on every change.

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

- **Where.** `benchmark_dir` is read from `asv.conf.json` or
  `asv.conf.jsonc`: the one nearest the root of the repository, up to
  three levels down. Comments and trailing commas are allowed. The
  directory is relative to the one holding the file (`"."` is that
  directory itself), and defaults to `benchmarks`.
- **Benchmarks.** Functions and methods whose names start with `time_`,
  `timeraw_`, `mem_`, `peakmem_` or `track_` (or `Time`, `Timeraw`, `Mem`,
  `PeakMem`, `Track` followed by a capital or an underscore), in every
  module under `benchmark_dir` as ASV walks it. Inherited ones and ones
  imported into a benchmark module count. They are named as ASV names
  them; a `benchmark_name` you set is used, also for the function
  imported elsewhere or inherited by a subclass.
- **What each depends on.** Its module and class (class attributes such
  as `params` count as part of the class), and:
    - its class's and module's `setup`, `setup_cache` and `teardown`,
      also when imported from another module (`setup` and `teardown` in
      any case, such as `setUp`, as ASV finds them);
    - the `__init__`, `__new__`, `__getattr__` and `__getattribute__` of
      its class, which ASV runs for every benchmark, also when inherited.
- **`timeraw_` benchmarks** (by the name ASV matches: `timeraw_import =
  _impl` is one) also depend on the modules their code imports. If the
  code isn't a plain string, the benchmark is always selected.

## When the target list may be short

Some things can't be known by reading the source:

- a pytest plugin that collects tests by its own rules, such as classes
  with a different naming pattern;
- a test base class, or an imported test, from outside your source roots
  (or from a file `[index] exclude` keeps out);
- an imported test that its module binds to something discovery can't
  follow (`test_x = factory.make`);
- a test file, benchmark file or doctest module outside your source roots
  or kept out by `[index] exclude` (with `--source-root src`, add `--source-root .` for tests in `tests/`),
  or a `benchmark_dir` outside the repository;
- options that change what pytest collects in ways discovery doesn't
  follow (`-c`/`--config-file`, `--rootdir`, `--pyargs`), or a
  configuration file below the repository root that pytest will use (its
  rootdir is then that directory, the monorepo shape): discovery reads
  only the configuration at the root, so what the nested file sets is not
  known;
- a `--command` discovery can't find pytest's arguments in (it looks for
  `-m pytest` or a `pytest` program), or one that names pytest more than
  once;
- a path given to pytest that doesn't exist in the repository;
- a plugin with a `pytest_ignore_collect` hook that can return `False`;
- a `conftest.py` or plugin that turns files or objects into tests itself
  (`pytest_collect_file`, `pytest_collect_directory`,
  `pytest_pycollect_makeitem`), whether it defines the hook or imports it,
  and one that star-imports (`from module import *`) from outside your
  source roots, since that may bring such a hook in;
- a `--doctest-glob` that matches files other than `.txt`, `.rst` or
  `.md`;
- a test given by node id (`tests/test_x.py::test_one`), or a path right
  after an option that may take it as its value (`--cov src`), wherever
  pytest's arguments come from: `addopts`, `PYTEST_ADDOPTS`, `--command`
  or after `--`;
- a test bound to something discovery can't follow
  (`TestMachine = Machine.TestCase`, `test_one = make_test(1)`,
  `test_a, test_b = make()`), or bound dynamically: stored into
  `globals()`, `locals()` or `vars()`, set on a class (`setattr(TestA,
  name, check)`, `TestA.test_x = check`), or made by pytest-bdd's
  `scenarios(...)`;
- a docstring set when its module is imported that discovery can't read,
  under `--doctest-modules`;
- a plugin your configuration turns on that collects files of its own
  (pytest-typing's `typing_checkers`, `--nbval`, `--markdown-docs`);
- a test file that can't be parsed;
- a runner configuration file that can't be parsed (`pytest.ini`,
  `pyproject.toml`, `tox.ini`, `setup.cfg`, `asv.conf.json` and the
  like): what it sets is unknown, so the defaults are used.

Each one appears as a note in the report's `discovery` section, and the
plan exits with code `3`: running only the selected tests could skip
tests your runner would collect. You can:

- widen your source roots so the missing code is analysed;
- list your tests in a [manifest](manifest.md) and plan from it alone,
  without `--discover` (beside discovery, a manifest adds targets but
  doesn't clear its notes);
- run anyway with `diffcone run --allow-incomplete-discovery`: pytest then
  also runs every test it collects that isn't a target; or
- use [execution evidence](../guides/evidence.md): a recording captures
  what pytest really collected, which can settle a note.

A recording settles a pytest note when pytest collected nothing at the
recorded commit that wasn't a target, the same note stood there too, and
the file it names is unchanged since. Notes about options, the command
line or the configuration (an unparsable configuration file, a plugin the
configuration turns on, an option or path discovery doesn't model) name
no file, so a recording never settles them; nor does it settle ASV notes.

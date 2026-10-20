# Changelog

All notable changes to diffcone. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/), and before 1.0 a minor version
may change the report schema, the cache and evidence store formats, or
selection rules.

## [Unreleased]

### Planning

- `diffcone.toml` takes `[index] exclude`, patterns for `.py` files to
  leave out of the analysis (deliberately invalid test data, as in a
  formatter's or a linter's test suite). A matching file is read as a data
  file: plans no longer degrade, `collect` records the checkout, and a
  change to one selects everything in a plan without a recording and the
  tests that read it or ran its code in one with. What an excluded file
  runs is not read, so code that imports or runs one (an import,
  `import_module`, `runpy`, a subprocess, a file loader) is selected on any
  change, with a reason naming the file; with a recording, a change to an
  excluded file a test imported selects every test. Discovery's notes on
  an excluded plugin, base class or benchmark file name the exclusion. The
  report lists each snapshot's patterns (`analysis.<side>.excluded`).

### Discovery

- Under `--doctest-modules`, a docstring set when the module is imported
  (`f.__doc__ = ...`, a string built from parts) is read when its text can
  be worked out. Otherwise (a decorator of yours rewrites it, or the value
  is imported or computed) a function whose written docstring has
  examples has its doctest always selected, and one without is reported
  (exit 3).
- Tests bound by a call (`test_one = make(1)`, `functools.partial`), a
  lambda, `globals()` or `setattr`, and pytest-bdd's `scenarios()`, are
  reported (exit 3). Module data that merely has a test's name is not.
- A `usefixtures` mark discovery cannot read (a non-literal argument, a
  decorator or factory that applies it, `pytestmark = marks()`) counts as
  every visible fixture.
- Test classes pytest does not collect (an inherited `__init__` or
  `__new__`, a dataclass or attrs class) are no longer targets: `run`
  reported them as selected tests pytest did not collect.

### Report

- `schema_version` 4: selected and unselected target records no longer
  list `lifecycle_dependencies`. Each target repeated every fixture,
  hook and module it depends on, nine tenths of a large suite's plan
  (850 MB for one of 25 000 tests, now 40 MB). Discovery's output still lists them, and
  the ones that selected a target are in its explanation.

### Execution evidence

- `collect` warns when none of the tests it recorded is among the tests
  discovered at that commit: pytest's rootdir lies outside the repository
  (a configuration file in a directory above it), so plans would find no
  recording for any test and select them all.

### Running and checking

- `diffcone run --allow-incomplete-discovery` starts pytest even when
  nothing is selected, with every target deselected, so the tests
  discovery does not know about still run (`--dry-run` says so).
- `diffcone run` no longer refuses to run because of its own results
  (`diffcone-results/`) in a flat layout, or a generated, git-ignored
  `_version.py` that only binds constants.
- `diffcone check` (and the `check` action) says when it compared the plan
  only, without diffcone's own run.
- `diffcone report` counts a pull-request run that failed after a good
  plan as a problem.

### Fixed

- Planning a large suite took far more memory than it needed: every test
  carried its own copy of the dependencies all tests share (code run
  while pytest collects), in the dependency graph and in the cached
  discovery results. On a suite of 25 000 tests a plan peaked at 21 GB,
  more than a standard CI runner has, and took up to 90 minutes; it now
  peaks at 5.5 GB and takes under two minutes, and a cached discovery
  result is 9 MB instead of 680 MB. Plans are unchanged. Discovery
  cached by an earlier version is redone once.
- With a recording, a module cache filled by a function the cache's
  caller calls (`_put(key, compute(key))`, a method, a chain of helpers,
  an `append`, or a write under a branch on the result) was not followed:
  a test that read what an earlier test stored there, without running
  `compute`, was not selected when `compute` changed. Every in-place write
  of the code that code calls is now followed.
- With a recording, a cache kept on an object, a class, a closure or a
  function attribute counted a store only when the stored value named the
  changed code directly. A value computed through a variable, a table or
  `super()`, read off a changed constant, or the writer's own literal could
  leave a later test that read the cache unselected. Every store counts
  now, as for module variables, including stores through `__dict__` and
  `vars()`. What `__init__` sets on the object it is building does not
  count; calling `__init__` again on an object in use does.
- With a recording, a module the analysis does not read (outside the
  source roots) that a test imported was credited to that test alone,
  though later tests use it without running any of it: a change to it now
  selects every test. A recording made by an earlier version does not say
  which such files a test imported, so any `.py` file a test touched
  counts.
- A write to state held on a class (`Config.mode = m`,
  `Config.items.append(x)`, `setattr(Config, ...)`, `cls._instance = ...`,
  `type(self)._cache[k] = v`) did not make the writer a dependency of the
  class's readers; nor did a write to a module variable under another
  name: a local alias (`cache = _CACHE`), an item of it, a bound method,
  `globals()["_CACHE"]`, or what one of your functions returns
  (`registry()[k] = v`). Both are followed now, with and without a
  recording. Copies (`dict(_CACHE)`, `.copy()`) and an instance's own
  attributes are not shared state.
- A write into state a class holds, made through a module-level instance
  of it (`STORE._cache[k] = v`, or `STORE.cache()[k] = v` where `cache`
  returns the class-level `_cache`), did not make the writer a dependency
  of the code that reads that state through the class. It does now, with
  and without a recording.
- The same held for an instance a function makes itself: a change in
  place through it (`s = Store(); s._items.append(x)`, an instance made
  with `cls.__new__(cls)` or returned by a factory, or one handed to a
  function that changes it) did not reach the code that reads the
  class-level state, when the class or one of its bases holds some. It
  does now. Setting an attribute on a new instance (`new =
  cls.__new__(cls); new._key = None`, as copy and replace methods do) is
  not a change to the class, unless the class's own `__new__` may hand
  out an instance in use (a singleton). In `__new__`, `__init_subclass__`
  and `__class_getitem__`, `cls` is the class.
- A module constant that cannot change in place (a string, a number, a
  tuple of such, `re.compile(...)`, `textwrap.dedent(...)`) and that no
  code binds again gained a writer from every function that passed it to
  a call or used what its methods return (SQL handed to
  `conn.executescript`, `PATTERN.match(s)`). Such false writers made every
  reader of the constant depend on unrelated code. Binding the name again
  (`global`, `module.NAME = ...`, `setattr`) still counts.
- The same holds for a class attribute your class body binds once to
  such a value (`DAYS = "days"`, `_MONTHS = 12`) and that no code binds
  again: handing `TimeDelta.DAYS` to a function, or `m = min(m,
  self._MONTHS)`, is no longer a change to the class, and neither is
  calling a method on an item of a class-level table of strings and
  numbers that nothing changes (`self.timeframes[k].format(n)`). When
  such a table can change, a method called on its items counts as a
  change.
- An object handed to a constructor of your class (`Tokenizer(source,
  rules=RULES)`) is part of the new instance: changing the instance in
  place (`tok.rules["x"] = 1`, a method changing what `__init__` stored)
  makes that code a dependency of the readers of `RULES`. Setting or
  counting up the instance's own attributes (`tok.position += 1`) does
  not, unless the class can run code when they are set (`__setattr__`,
  a property, `__new__`, a decorator, a metaclass or a library base).
- A write onto the class of an object whose type is not known
  (`type(obj).x = v`, `obj.__class__.items.append(v)`, `setattr(type(obj),
  name, v)`, or `type(obj)` handed to a function that sets `c.x`) only
  counted as a change to the object. It now makes the writer a dependency
  of the readers of every class that may have that attribute: the classes
  that define or set it, classes deriving from a library class, and their
  subclasses (every class, for a special attribute, a computed name or an
  attribute no class sets). A mock's class is its own.
- `pytest.importorskip("pkg.mod")`, `importlib.util.find_spec`,
  `pkgutil.resolve_name`, `pydoc.locate` and `pkgutil.iter_modules` or
  `walk_packages` over a package were not imports: a change that made the
  module fail to import, or changed what it does when imported, did not
  select the test. They are now.
- Discovery missed fixtures a test requests outside its own signature:
  `request` handed to a helper, an alias of `request.getfixturevalue`,
  `lf()`/`lazy_fixture()` in a parametrization, a hook adding to
  `item.fixturenames` or to the `usefixtures` option; an autouse fixture
  under another name for the decorator (`from pytest import fixture as
  fx`); and a module stored into `doctest_namespace` by subscript
  (`doctest_namespace["pkg"] = pkg`). They are dependencies now.
- The `run` action planned only the last commit (`HEAD^1`): a push of
  several commits, a merge-queue group or a pull request checked out at
  its head planned part of the change. It plans the event's own change
  now (a pull request on top of its target branch, every commit of a
  push, the whole merge-queue group), and a branch name, tag or `HEAD~n`
  as `base` works in a shallow checkout. With no base (a new branch, a
  manual run) or an event base that cannot be fetched, it runs every test
  and says why.
- The `record` action's check counted any failed plan as an unusable
  recording and passed; it fails now when the plan fails without the
  recording too.
- The `run` and `record` actions deepen a shallow checkout with an
  absolute depth. They used `git fetch --deepen`, which GitHub serves only
  from a commit a branch or pull request still points at: a re-run, or a
  run whose pull request had moved on, checked out a merge commit since
  replaced, the base never arrived and the job failed with no plan (and
  blamed the recording). A base still missing is no longer reported as an
  unusable recording.
- `diffcone report` (and the `report` action) kept every plan it read
  whole. A plan made from a recording explains each target, tens of
  megabytes for a large suite, so a day of such plans could exhaust the
  runner's memory and stop the report. It keeps only what it reads.
- On Windows, the recorder no longer treats a system query (`locale -a`,
  `git rev-parse` and the other read-only `git` queries, `ps`, `uname`) as
  a process it cannot follow. It did, so a recording of a suite that runs
  one outside every test (locale tests that query at import, pytest-benchmark
  in every run) planned every change as select-everything there. POSIX
  was not affected.

## [0.4.0] - 2026-10-14

Upgrading from 0.3.0: make your execution-evidence recordings again. The
store format is now 6 (0.3.0 wrote 1): `--evidence auto` skips an older
recording, naming one is an error, and the `run` action plans from the
code until `record` makes a new one. Expect plans to select more tests
than 0.3.0 did: most of the changes below close cases where diffcone
missed an affected test. The JSON report keeps schema version 3 and gains
fields: `always_run` and `analysis.counts.always_run`;
`environment_checked` and `preloaded_modules` in `analysis.evidence`;
`rootdir`, `configfile`, `junit_prefix` and `run_arguments` in pytest's
`discovery[].config`; and `evidence_not_used` in the plan `run -o` writes.
`check --format json` gains `mapping.rootdir` and `mapping.junit_prefix`.
New discovery notes: `unparsable_config` (discovery incomplete),
`unreadable_plugins` and `plugin_outside_roots`.

### Planning

- `diffcone.toml` takes `[[always_run]]` entries: the tests (or
  benchmarks) whose id matches a pattern are selected in every plan, with
  or without a recording, and the report gives the entry as the reason and
  counts the targets each entry matched; the text report flags an entry
  that matches nothing. A malformed entry, or one naming an unknown
  runner, is an analysis error, so the plan selects everything rather than
  quietly leaving those tests out. `diffcone report` computes a job's
  selected share without them.
- A test is selected when the fixtures, hooks or plugins around it change
  though its own code did not (rule `lifecycle_changed`), for example when
  another test module adds a plugin to `pytest_plugins`; with a recording,
  compared with what ran around it when it was recorded. pytest plugin
  modules are dependencies of every test, since their import-time code
  runs for the whole session.
- diffcone now assumes a test does not depend on what an earlier test left
  behind. A change to module state made by code that runs only inside one
  test counts for that test only, not for the other tests and benchmarks
  reading the same state: the test itself, a function-scoped fixture,
  unittest's `setUp`/`tearDown`, and a helper in a test module or
  conftest that only such code calls. A patch undone when the test ends
  counts the same way, also for lookups on a library module it patches:
  `monkeypatch` and `mocker` methods on pytest's fixtures, a `with
  mock.patch(...)` block, a `@mock.patch(...)` decorator,
  `MonkeyPatch.context()`, and a test or fixture that saves a module
  attribute and puts it back. Still counted for every reader: a test
  module's top-level code, wider-scoped fixtures, hooks, all project code
  (a cache a test fills through it), a doctest's function, a manifest
  entry on project code, a test or `setUp` that module-level code, a
  wider-scoped fixture or a hook calls, and a store through a
  `monkeypatch` that a wider-scoped fixture requests or the test module
  overrides (one overridden in a parent conftest is still taken as
  undone). If a test relies on an earlier test's state,
  declare the dependency in `diffcone.toml`.
- A fixture or function that only empties or fills a module-level dict,
  list or set (`REG.clear()`), or resets another module's variable
  (`mod.X = None`), does not count as reading it: an autouse reset fixture
  no longer selects every test when the code filling the registry
  changes.
- An attribute read on an object of unknown type is matched to
  module-level functions, classes and variables only in modules such an
  object can be: modules passed around or bound to another name, obtained
  by name or named in a string, and test modules and conftests, which
  pytest hands out. A name bound only to modules (`m = ops`, then
  `m.value()`) is followed like the import itself. Methods still match
  whatever the receiver.
- A text a test writes or runs (`pytester.makepyfile`, `exec`, `-c` code)
  whose template field the test's own parameter fills, when
  `@pytest.mark.parametrize` on the test gives that parameter literal
  values, is read once per value instead of counting as text built at run
  time, which any change reached. A value that is not a literal, an
  `indirect` parameter, another decorator on the test, or a parametrize
  on its class or in `pytestmark` keeps the old rule.
- Indexing a very long function is no longer quadratic in its calls.
- Editing the docstring of a method under `@classmethod`, `@staticmethod`
  or `@property` no longer counts as code run at import (which selected
  every test importing the module): these builtins only copy the
  docstring. Code that reads the docstring is still selected, and so is a
  project decorator with one of those names.

### Fixed

#### Static analysis

- A change to a dependency, build or CI file outside the source roots
  selected nothing when the roots don't hold the repository root. These
  now select every target: lock files (`uv.lock`, `poetry.lock`, any
  `*.lock`, `pylock.toml`, `conda-lock.yml`), `requirements*` files (also
  `dev-requirements.txt`, `Requirements.txt`, `requirements.pip`),
  `environment*` files, a `.txt`/`.yml`/`.yaml` file in a directory named
  `deps` or `dependencies`, a workspace member's `pyproject.toml`,
  `uv.toml`, `hatch.toml`, `poetry.toml`, `pdm.toml`, `pytest.toml`,
  `.env` and `.pth` files, `sitecustomize.py`, `noxfile.py`,
  `.github/workflows/`, checker configurations that pytest plugins run as
  tests (`mypy.ini`, `ruff.toml`), compiled sources, anything under a
  `*.dist-info`, `*.egg-info` or `*.egg` directory, a build script
  anywhere under a root (a nested `setup.py`), and a lock-file change
  between `INDEX` and `WORKTREE`. Evidence mode treats them the same way.
- A name taken from a dict, list or set written in the code
  (`getattr(handlers, NAMES[key])`, a lazy-export table) is bounded by it
  only while every use of the table is a read: a table changed through an
  alias, a helper it is passed to, its module (`core.TABLE[k] = v`),
  `globals()`, `vars()`, `sys.modules`, `monkeypatch` or `mock.patch` (an
  object or a dotted string) is no longer read as its literal. A
  parameter or class-body name that shares its name with such a table is
  no longer bounded by it.
- A lookup on a standard-library or third-party module
  (`getattr(logging, name)`) counts writes that reach the module
  indirectly: an alias, a helper it is passed to, a loop, `__setattr__`,
  `vars()`, a module found at run time, a submodule handed on
  (`register(os.path)`). Handing a library object such as
  `datetime.timezone.utc` to a call does not make every lookup on that
  library depend on the caller.
- Modules whose name is known only at run time: once code can reach one,
  no literal table is bounded. A name that starts with fixed text
  (`import_module(f"plugins.{name}")`) stands for the modules with that
  prefix; a new `types.ModuleType` is no project module unless it is
  installed in `sys.modules` under one's name; `exec(code, namespace)`
  does not reach the calling module; `m.__dict__["X"] = v` changes only
  `X`. `runpy.run_module(name)`, `runpy.run_path(path)` and `python -m
  <name>` with a name nothing bounds count as an import of any module, as
  `importlib.import_module(name)` does.
- An object put in `sys.modules` (assignment, `setdefault`, `update`,
  `monkeypatch.setitem`, `mock.patch.dict`) was ignored. It is now the
  module of that name: lookups on that module see it, and while it stays
  installed, the module's importers depend on the code that installed it.
  Without a recording, code that installs an object under a name computed
  at run time makes every test importing a project module depend on it.
- A change to a module-level variable made through a module object the
  analysis did not resolve to the variable's module is a dependency of
  the variable's readers: `importlib.import_module("pkg.reg").TABLE["x"]
  = f`, `sys.modules[...]`, a function-local import, a star import, or a
  module passed to a helper that writes into it. A function's
  `__globals__`, a frame's `f_globals`, `pickle.loads`,
  `pkgutil.resolve_name`, `pydoc.locate`, `gc.get_objects()` and
  `importlib.util.module_from_spec` copies are recognised as ways to
  change a module's tables, and a lookup by a computed name on a module
  sees objects other code puts on that module. Star-imported names
  (`from sys import *`, then `modules[...]`) are seen too.
- State changed through an argument or a receiver (`register(REGISTRY)`,
  `registry.add(x)`) did not reach its readers, in either mode; nor did a
  change to code that may call such a writer differently, or a write
  through a module attribute and an item (`store._CACHE["k"].append(x)`,
  `del store._CACHE[k]`). A method called on an instance attribute
  (`self._client.get()`) counts as that class's method only when every
  assignment of the attribute creates an instance of one class (or of a
  third-party class), and a module-level object made by a third-party
  call is changed only by methods of classes deriving from a third-party
  class; calls on such attributes are still matched by name. A function
  a module-level variable's initialiser calls runs at import, so a change
  to it reaches the module's importers.
- Changing the arguments of a call made at import time (`X =
  set_mode("slow")`, a class attribute, a decorator's or default's
  argument) did not select the tests that read the state the callee
  changes. An import added to or removed from a module that starts or
  stops running another module's import-time code reaches the readers of
  what that code writes (a registry filled at import), in both modes;
  added imports stay non-structural otherwise.
- Moving a module's or a class's statements around (a constant moved
  below the decorator that reads it, two `@register` functions swapped, a
  `def` moved below a `try`/`except` import of the same name) was no
  change at all: the order of statements both sides have is now part of
  the definition, so a reorder reaches importers. Only swapping
  neighbouring plain `def`s (no decorator that does anything, no
  annotation evaluated at import) still selects nothing.
- A decorator diffcone can't show leaves the function alone (one from an
  installed package, or a project decorator that calls or registers it)
  makes the decorated function count as code that runs on import, and its
  parameters are no longer bounded by its call sites. A name imported in
  a `try`/`except` or `if`/`else`, rebound by a star import, or redefined
  in the module is no longer taken for `typing.final`, a quiet decorator
  or a structural base; attribute uses of a name bound several ways
  depend on every binding.
- A local name, parameter or function-local import was taken for a
  module-level import or builtin of the same name, and a call of an
  unresolved function named `rebinds`, `registers` or `mutated_by` for one
  of diffcone's own kinds of dependency.
- A dynamic import, script path or `getattr` receiver bounded by its call
  sites is unbounded when a caller may be unseen (a call through an object
  of unknown type, an unseen construction).
- Reading members by name or wholesale is a dependency: an attribute a
  class does not define (`C.__doc__`, `C.__type_params__`, one a decorator
  sets), `C.__dict__[name]` and `obj.__dict__[name]`, `mod.__dict__[name]`
  and `inspect.getmembers(mod)` (a test parametrized over a module's
  functions), `match` class patterns, `operator.attrgetter`/
  `methodcaller` and `str.format` fields (`'{0.attr}'`); a non-literal
  name is a dynamic reference. `C.__subclasses__()`, `C.__mro__` and
  `C.__bases__` reach the classes they return.
- Code that passes a module to other code (`read(ops, name)`) depends on
  that module's functions and on what its imports bind (`api.core.TABLE`);
  a module from a literal `import_module("pkg.sub")` or `__import__` that
  is handed on counts as handed on. An instance handed to code outside the
  project (`json.dump(x, w)`, `print(file=w)`, `dict(obj)`,
  `csv.writer(w)`) depends on every method of its class, not only its
  dunder methods.
- A class obtained by a name nothing resolves (`m.Alt()` on an object of
  unknown type, a lazy `__getattr__` export) reaches its `__init__` and
  `__new__`. A class with an `enum.Enum`-family base or an in-scope
  metaclass depends on its `__init__`/`__new__`, which run when the class
  is created (`Color.RED.code` reaches `Color.__init__`); `dir(mod)`
  depends on `mod.__dir__`, and `dir`, `hasattr` and `vars` of a library
  module your code writes to are lookup sites.
- A docstring-only change selects the tests that read that docstring, in
  both modes, also through a parameter (`inspect.getdoc(fn)` in a
  helper), `getattr(obj, "__doc__")` and a module's `__doc__`.
- A change to PEP 695/696 type parameters (bounds, defaults, their number)
  was no change at all; a bound or default naming project code is now a
  dependency. A subscript in an evaluated annotation (`X: Registry["k"] =
  1`) counts as code that runs on import.
- Code a test runs in a new process: a test running a script path, `-m
  module` or `-c` code named in the code did not depend on it. A module's
  file is an import of it; a `.py` file no module name maps to
  (`scripts/gen-data.py`) is read as `-c` code is (what it imports counts,
  and a change to the file reaches the test; if it imports by a name it
  computes, any change does); a Python command whose program is
  built at run time is affected by any change. A child whose interpreter
  is not the first word (`env X=1 python -m ...`, `uv run python ...`,
  `timeout 30 python ...`, `asyncio.create_subprocess_exec(sys.executable,
  ...)`, `os.execl`) is followed. `python -c` code that changes
  `sys.path`, `sys.meta_path` or `sys.modules` by any assignment, deletion
  or augmented assignment, or calls `os.chdir`, can import anything.
- Text a test writes out and runs (`exec`, `eval`, `compile`, `timeit`,
  `pytester.makepyfile`) is read as the code it is: its imports and calls
  count. Text built at run time from pieces that are not written out can
  import anything.
- A test file given to a test runner (`python -m pytest path`,
  `pytest.main([...])`, `python -m unittest module`, `unittest.main
  (module=...)`, `loadTestsFromName`) counted as an import; it runs whole,
  conftests and fixtures included. `unittest` discovery,
  `doctest.testfile`, `DocFileSuite` and `DocFileTest`, a directory or a
  non-literal argument can run anything, so any change selects the test.
- Functions obtained from `pickle.load`/`loads` (and cloudpickle, dill,
  joblib) or from a module loaded by a computed file path were invisible
  without a recording; such code is now affected by any change. A round
  trip (`loads(dumps(x))`, a temporary file), pickle bytes written in the
  code and a path in the module's own directory are bounded by what they
  load.
- Patches: one that may outlast its test (`patch(...).start()`, a patcher
  kept for later, a `MonkeyPatch()` of your own, a `with mock.patch(...)`
  around a fixture's `yield`, `setattr` on any other receiver) is a store
  on the module it patches, so lookups on that module follow it, and set
  in `sys.modules` that way it counts as installed for good. A `with
  mock.patch.dict(os.environ)` or `warnings.catch_warnings()` block whose
  body yields does not put the process state back. `mock.patch.multiple
  ("pkg.mod", NAME=...)` depends on those names and the module; any dotted
  patch target counts as an import of its module, also through a patcher
  called on a value (`pytest.MonkeyPatch().setattr("pkg.mod.NAME",
  ...)`).
- A write to process-global state (`os.environ`, warning filters,
  `sys.path`) is recognised whichever import binds the name: `from os
  import *`, an import in `try` with another in `except`, or a
  function-local one. A `with` statement counts as putting state back only
  when every binding of its name is the restoring one.
- Snapshots: `WORKTREE` did not see edits to files flagged
  assume-unchanged or skip-worktree. It now reads git-ignored `.py` files
  in directories git does not ignore as a whole (a generated
  `_version.py`, `local_settings.py`); ignored directories are still
  skipped. File mode changes (`chmod +x`) under the source roots are
  changes, in commits and the working tree. A submodule under the roots
  no longer makes every `WORKTREE` plan select everything (one checked out
  at another commit, or with uncommitted edits, still counts as a
  change); a path containing a line break, or a tracked file replaced by
  a directory, no longer fails the plan.
- A file nested too deeply to analyse is an analysis error that selects
  everything (a test, conftest or benchmark module: a file that did not
  parse, which makes discovery incomplete), instead of a crash. A
  `diffcone.toml` that is present but can't be read (left unmerged by a
  conflicted merge) is an analysis error too; it was read as no file. A
  manifest that is not valid JSON says so.

#### Discovery

- A fixture defined with a decorator whose name ends in `fixture`
  (pytest-twisted's `async_fixture` and `async_yield_fixture`,
  pytest-trio's `trio_fixture`) was not seen; when its name was also a
  well-known plugin's (pytest-django's `client`), it was taken for that
  plugin's fixture and what it requested reached nothing. Such decorators
  now make fixtures, and a plugin's fixture name is no longer assumed when
  the test's class, module, conftests or plugins define it in a way
  diffcone can't read: the name is an unknown fixture, selected on every
  change.
- An autouse fixture imported into a conftest, a test module or a plugin
  (by name, through star imports at any depth, or as a module attribute)
  was not a dependency of the tests it applies to; nor was a fixture with
  `name=` imported under its function name.
- Conftests pytest loads at startup (in the `test*` directories of a path
  it starts from) count even when ignored or under `norecursedirs`;
  `--ignore`, `--ignore-glob` and `norecursedirs` apply only below the
  paths pytest starts from, and not under a conftest whose
  `pytest_ignore_collect` can return False.
- `pytest_plugins` is read however it is built (`+`, `+=`, `.append`,
  `.extend`, a comma-separated string, a variable) or imported from
  another module; one that can't be read keeps every test selected
  (`unreadable_plugins`), as does a plugin module in the repository
  outside the source roots (`plugin_outside_roots`). The `pytest_*`
  methods of an object registered as a plugin
  (`config.pluginmanager.register(...)`), and plugins registered with
  `import_plugin("name")`, apply to every test.
- `pytestmark` lists built with `+`, `+=`, `.append` or `.extend`, or
  imported, apply their `usefixtures`; `indirect=` given as a variable, as
  the third positional argument or through `**kwargs` no longer drops the
  fixture from the test's dependencies.
- Tests defined, aliased or marked `__test__ = True` inside `if`/`try`
  blocks, and tests imported from such modules, are found; an imported
  test discovery can't follow is reported, not dropped.
- Code pytest runs while collecting that changes the process (a conftest,
  a test module or a module they import that sets an environment variable
  or extends `sys.path` at import, a `pytest_generate_tests` that does,
  also through a patch nothing undoes: `mock.patch.dict(os.environ,
  ...).start()`, `pytest.MonkeyPatch().setenv(...)`, `syspath_prepend`)
  is a dependency of every test.
- A collection hook imported into a conftest (by a star import, or from
  outside the source roots), or a star import from outside the roots in a
  conftest or plugin, makes discovery possibly incomplete.
- A `pytester`/`testdir` test depends on the project's registered plugins
  (`pytest11` entry points, `pytest_plugins`, `-p`) and on what its inline
  files import.
- ASV: `setUp` and `TearDown` in any case, and the benchmark class's
  `__init__`, are dependencies of its benchmarks; a `timeraw_` benchmark
  bound under another name, or renamed with `benchmark_name`, depends on
  what its code imports; `benchmark_name` applies wherever the function is
  bound, inherited methods included; `benchmark_dir: "."` finds
  benchmarks.

#### Execution evidence

- A module stored into pytest's `doctest_namespace` (pandas' autouse
  `add_doctest_imports` does `doctest_namespace["pd"] = pd`) no longer
  counts as handed to unknown code. When that module could reach `gc`,
  every test that ran the fixture counted as able to look up anything, so
  a docstring edit selected every test.
- A module variable filled once by one function and then only read (a
  lazily computed constant such as pandas' `_pkg_dir` in
  `find_stack_level`) no longer counts as state that function writes for
  later tests: the value is the same whoever computes it first, so a
  change that only makes the function run earlier or later reaches nothing
  through it. A change to the function itself, or a reader outside it,
  still counts.
- Code that ran while pytest collected a test file (building the ids of
  parametrized tests, an `ids=` function, `pytest_generate_tests` and what
  it calls) now selects that file's tests when it changes, and the tests
  reading what it stored, instead of every test; so does a change to a
  file read then. It still selects every test when what ran it can set
  something for the whole process (an environment variable) or reaches
  pytest's configuration or session (`.config`, `.session`, `.stash`,
  `.pluginmanager`, another hook). Code run by session hooks
  (`pytest_configure`, `pytest_collection_modifyitems`) selects every test
  as before.
- A test that failed or errored while it was recorded is always selected:
  its recording stops where it failed, so where it passes (a flaky failure,
  a different environment) it can run code the recording never saw.
  `VIRTUAL_ENV` is now part of the environment a recording is checked
  against: a tool run from its own environment passes its `VIRTUAL_ENV` to
  the suite, and IPython warns on one that is not the interpreter's.
- A test module listing the machine's locales while pytest collected it
  (`locale -a`, as pandas' locale tests do) made every change select every
  test. Listing locales runs no Python, and no longer counts as a process
  the recording can't follow. Nor does `ninja`, which an editable
  meson-python install (pandas) runs on import to rebuild compiled code.
- Before Python 3.14, a test that had a library evaluate string
  annotations (`from __future__ import annotations`, forward references)
  through `typing.get_type_hints`, as pydantic and FastAPI do, recorded
  nothing of them, so a change to those annotations, or to a class they
  name, did not select it. The recorder now credits the test with the
  annotations' owner and what their names resolve to, through `typing`,
  `annotationlib` and `inspect` alike; `inspect`'s evaluation no longer
  counts as running text code, which selected the test on every change.
- A recording no longer disables pytest's cache plugin: a configured
  `--failed-first` made recording a usage error. The plugin starts from
  an empty cache of its own, so `--lf`/`--ff` neither narrow nor reorder
  the recorded run, and the repository's `.pytest_cache` is left alone.
- Editing one test selected every test of its file: pytest looks up each
  test's location as the test starts, which read the file inside the
  test's record. That lookup is no longer recorded; a test that reads its
  own file still is.
- On Python 3.14, editing the body of a function whose annotations were
  read outside the tests selected every test, or every test importing its
  module. Python 3.14 evaluates annotations in code of their own, which
  pytest runs while collecting (reading a signature) and frameworks run at
  import (FastAPI, dataclasses, pydantic). That code no longer counts as
  the function running there, nor as reading a change its annotations
  don't name; a change to the annotations still counts.
- Editing a test file selected every test when the suite was recorded
  with a virtual environment inside the checkout (`uv run pytest` runs
  `.venv/bin/pytest`): pytest reading files during collection looked like
  the project reading them outside any test. An environment in the
  checkout, or inside a source directory (where it hid that directory's
  code or gave a false "installed copy" error), is now installed code.
- A session hook that lists installed distributions
  (`importlib.metadata.distributions()`) made adding any file anywhere
  select every test; those scans are no longer recorded.
- The recorder starts before pytest loads any `-p` plugin (`addopts`,
  `PYTEST_ADDOPTS`, the command) and before a project `sitecustomize.py`,
  so what they import is recorded. Modules imported before it could start
  are listed (`preloaded_modules`), `collect` warns, and any change in
  them is planned from the code.
- Files the recording missed are recorded: files a library reads while it
  is imported (matplotlib's `matplotlibrc`), files reached through a
  directory descriptor (`dir_fd=`, `os.listdir(fd)`, `os.fstat`), entries
  of an `os.scandir` listing, `os.readlink`, `sqlite3` `file:` URIs,
  symlinked paths, and on macOS and Windows a file opened under another
  capitalisation. A descriptor the recorder cannot trace flags the test.
- A process started while a `conftest.py` is imported or in a hook was not
  credited to anything; what it runs now counts as part of that import or
  hook, and one the recording cannot follow makes every change plan from
  the code (pytest-xdist workers are not affected). A test using a
  `multiprocessing` pool, forkserver or forked process that an earlier
  test started is always selected while that process runs.
- Tests that run code compiled from text no file holds (doctests,
  `--doctest-modules`, `timeit`, a notebook kernel) are always selected
  (rule `text_code`). A string `skipif` condition naming nothing of the
  project and `autospec=True` mocks do not count as such text.
- A change to code that runs at import, or that ran during an import,
  reaches the tests reading what it writes into another module, Cython
  code included. Code that can obtain a test module without importing it
  by name sees changes to it: library code importing one by a computed
  name, an unpickler, a loader of a computed path. A lazy-export
  `__getattr__` whose table only test-time code can change does not count
  as such code, nor do `gc.get_stats()`, `gc.collect()` and
  `gc.callbacks`, and `hasattr(obj, "name")` notices only `name`.
  `pytest_plugins` in a test module selects every test when it changes.
- A function added, deleted or redefined with an annotation that calls
  code (`def f(x: set_mode("fast"))`), or a module-level variable whose
  annotation does, reaches what that code writes: before Python 3.14
  annotations run with the definition. A definition whose annotations call
  nothing is not escalated.
- Caches: a module-level cache filled by an earlier test hid the function
  that filled it from later tests' records, in every order; a change to
  that function now reaches the tests reading the cache. So does a cache
  kept on an object, a class, a closure, a function attribute or in a
  memoising decorator diffcone knows (`functools.cached_property`,
  `cachetools`, Django's and Werkzeug's `cached_property`); a decorator
  from an installed package it doesn't know is taken to keep nothing. A
  store made by a test's own code counts for that test only.
- A non-literal `getattr` on a standard or third-party module sees
  changes anywhere in the project, not only in its own imports, and a
  test module's import-time change reaches lookups in library code.
- Code a hook ran that changed, or that reads a change, selects every
  test.
- Cython: edits to blank lines or `#` lines inside a string in a function
  body were ignored.
- A recording stored under a path containing `#`, `?` or `%` was skipped
  as if there were none.
- When `run` couldn't use a recording because only `-O` or `PYTHONPATH`
  differed, it listed no differences; every difference is now named.

#### Running, check and validate

- `validate` and `corpus` could not run the suites of several projects.
  A checkout of a commit lacks a git-ignored, build-generated module such
  as the `_version.py` setuptools-scm or hatch-vcs writes, so the package
  could not be imported; the repository's copy of each git-ignored module
  inside a package is now copied into every checkout that lacks it
  (not a conftest or test file; `--setup-command` runs after it). pytest's cache plugin is no longer
  disabled, which made a configured `--failed-first` or `--lf` a usage
  error; it starts from an empty cache of its own instead, so those options
  neither narrow nor reorder the run, and the repository's `.pytest_cache`
  is left alone (a run whose cache could not be moved is refused). With `--coverage`, a suite that did not run is reported
  as such instead of as a missing coverage database; pytest-cov is loaded
  with `-p` when plugin autoloading is disabled; the database's place is
  set in diffcone's coverage config instead of `COVERAGE_FILE`, which the
  tests' own processes inherited; and parallel parts beside it are read
  too. Errors name the snapshot (`base <commit>` or `head`) whose suite
  failed.
- `validate` and `corpus` reported tests outside the target list as
  misses even when the plan said discovery was incomplete (it exits 3,
  and `run` refuses it). They are now listed as "outside the target list"
  and don't fail the check, and coverage recall is measured over the
  targets. When discovery claims to be complete, such a test is still a
  miss, labelled `MISSED (not a target)`. The JSON gains
  `discovery_incomplete`, `outside_targets` and a `target` flag on each
  record; `counts.targets` now counts targets only.
- `diffcone check` decided "already failing at the baseline" per test
  function, so a parametrized case that failed at the baseline excused a
  new failure of another case: a miss reported as clean. Cases are now
  compared one by one.
- `check` and `validate` work when pytest's rootdir lies below the
  repository (an ini file in a subdirectory): the plan records pytest's
  rootdir, configuration file and `--junit-prefix`, `check --rootdir` and
  `--junit-prefix` override them, and a directory that fails to collect
  counts as a collection failure of every test under it. Before, every
  failure was an unknown miss, which the `record` action confirmed.
- `validate`, `corpus` and `collect` run the whole suite whatever `-x` or
  `--maxfail` is configured, and `validate` and `corpus` stop with exit
  code 2 when the suite reported fewer outcomes than it collected: a miss
  can no longer hide behind an early stop. `validate` runs pytest without
  `-v` and reports the command line it ran.
- `run` refuses a working tree that differs from the planned snapshot in
  any file the plan would have selected on (data and configuration files
  under the roots, `diffcone.toml`, runner configuration, build inputs,
  conftests, ignored `.py` files, file modes, dirty submodules, files
  flagged assume-unchanged or skip-worktree, `.py` files with non-ASCII
  names) and names them.
- `run`'s "did not collect" message names `--lf`, `--ff`, `--sw`, `-k`,
  `-m` or `--deselect` among the effective pytest arguments and where
  they are set.
- `collect` and `run --collect` no longer refuse a clean checkout because
  untracked bytecode or ASV output has a non-ASCII name; a renamed file
  counts as both names.
- `run --dry-run --evidence` prints a command you can run, without
  diffcone's internal recorder plugin.
- `discover` honours `--no-cache` and `--cache-dir`: it uses the same
  cache as `plan`, and its output is unchanged.
- On Windows, a relative `--command` resolved for `validate` and `corpus`
  was quoted for a POSIX shell, and one written with forward slashes
  (`.venv/Scripts/python.exe`) was not resolved.
- An unexpected error in any command exits 2; `check`, `report`,
  `collect` and `prune` exited 1, which `check` uses for a miss.

#### CI actions

- The `record` action with `check: true` could pass a push that had a
  miss: a re-run of the job restored the newest recording instead of its
  own commit's (so the kept verdict was lost and the push was re-checked
  against a later baseline), and a check that crashed, or a re-run whose
  JUnit could not be read, was recorded as checked with no miss. A
  recording that is not from before the pushed commit is no longer
  checked against. A re-run's other failures (when the job's pytest
  arguments name a path after an option) no longer count as misses, and
  the re-run of a missed test runs a `command` with quotes or spaces as
  written.
- `diffcone report` ignores recording artifacts uploaded by pull-request
  runs (a pull request from a fork runs its own code), counts an artifact
  it cannot read as a problem instead of failing, reports pull-request
  runs whose selected tests pytest did not collect, keeps a test id with
  backticks inside its code span, and escapes `|` in "not checked"
  reasons.
- The `report` action failed whenever a job of a reported run had been
  re-run (two artifacts of the same name); it reads the latest attempt's.
  Its window starts at the last report that posted, so a run with only a
  job summary (a manual run with posting off) no longer hides the runs
  before it.
- The `record` action's re-run of a new unselected failure stopped at the
  first failure when `pytest-args` or `addopts` had `-x` or `--maxfail`,
  and the misses it never reached were reported as flaky. It now re-runs
  with `--maxfail=0`, and a missed test the re-run did not run still
  counts as a miss.


### Discovery

- pytest options in `PYTEST_ADDOPTS`, plugins in `PYTEST_PLUGINS` and
  pytest arguments written into the run's command are read: `plan` and
  `discover` take `--command`, the CI actions pass it, and the report
  lists what was read (`run_arguments`). A command whose pytest arguments
  cannot be found is reported.
- Understood now: `.` as an argument, a `.txt` or `.rst` file named as an
  argument (always a doctest), `--doctest-glob` patterns with a directory,
  option values written as separate arguments (`--ignore tests/slow`),
  absolute paths inside the repository, `-o name=value` overrides and
  paths in `addopts`.
- A pytest configuration file below the repository root that pytest
  would use (one nearer the paths it starts from than the root's) is not
  read by discovery, so it makes discovery incomplete
  (`unmodelled_runner_option`, exit code 3; `run` refuses without
  `--allow-incomplete-discovery`).
- A pytest configuration file (`pytest.toml`, `pytest.ini`,
  `pyproject.toml`, `tox.ini`, `setup.cfg`) or an `asv.conf.json` that
  can't be parsed made discovery quietly use the defaults; it now makes
  discovery incomplete (`unparsable_config`, exit code 3), as does a
  `benchmark_dir` outside the repository.
- `asv.conf.jsonc` is read like `asv.conf.json`, and trailing commas in
  either are accepted, as asv allows.

### Execution evidence

- A Python process a test starts with `subprocess` (directly, through
  `asyncio`, or through `uv run`) records itself on Linux and macOS, and
  the test is credited with what it ran instead of being selected for
  every change; so is every test that runs while the process is still
  running (a server or a warm worker another test started). A `python -c`
  probe that runs project code is caught by its own recording, and a
  project's own `sitecustomize.py` does not stop subprocesses from
  recording themselves. A process started another way (`os.system`, a
  shell, `multiprocessing`, anything on Windows), one that cannot record
  itself (a replaced environment, `python -S`, Python before 3.12), one
  whose recording was cut short (a full disk), and one running project
  code from a `-c` snippet or a script outside the source roots still
  select their test for every change.
- A test run that changes its own environment (a test that installs or
  upgrades a package with the test interpreter) is named: `collect` warns
  with the packages, the recording keeps the environment it started in,
  and `run` says when it meets that environment. The recording is still
  keyed by the environment at the end, so such a run plans from the code.
- `plan --evidence` says the environment was not checked
  (`environment_checked: false`): only `run` can check it, and its `-o`
  report says `true` when the environment matched.
- `run --collect` on a recording made with `--reverse-check` runs the
  selected tests again in reverse order, so the advanced records are
  order-checked too.
- A method or attribute added to (or changed or deleted on) a fake class
  in a test module selects only the tests that ran library code calling a
  method of that name (`x.read()`) and also ran code able to hand that
  code a fake: code naming the class or a subclass, or finding it without
  naming it (a base's `__subclasses__()`, `gc`, a frame's globals). A
  fake built at import, in a decorator or during collection, or one whose
  class or bases sit outside test code, still selects every such test.
- A lookup on a standard or third-party module that tests patch
  (`dir(builtins)` or `hasattr(os, name)` in library code, with
  `monkeypatch.setattr(builtins, ...)` in a test) no longer makes every
  change anywhere select every test that ran the lookup: the tests that
  patch the module are selected through their own code. A module written
  at import or in a hook is treated as before, and a change to code that
  runs at import still reaches the lookup when it can run a writer.
- Editing an ordinary module named `build.py` (not at the repository root
  or beside `pyproject.toml`/`setup.cfg`) selects the tests that ran it,
  as static planning does, instead of every test.
- Recordings no longer select every test for every change when pytest
  plugins or test helpers ask the machine or the repository something
  outside the tests: `git` reporting the commit, branch, remote or
  configuration (pytest-benchmark does as it starts) and `ps`
  (shellingham's shell detection) join `uname` and `locale`. A `git`
  command that prints a file or writes still counts.
- Running the suite under coverage (`--cov` in `addopts`) no longer makes
  every source file look read by the module importing it: coverage
  resolving a file it is about to measure is nobody's read.
- Tests that build URLs with werkzeug (every Flask app) no longer count as
  running code compiled from text, which selected them for every change.
- A change to a module no test imports (a Sphinx `docs/conf.py`) no
  longer reaches code that looks names up on whatever it is handed (a
  frame's globals, a function's `__code__`, `sys.modules`), which selected
  every test importing that code. A module imported by any test, even
  inside one, or by an import added in the same change, still does.

### CI

- `record` describes every recording for `diffcone report`, also without
  `check` (a nightly recording) and when the recording failed: its
  `results` output always holds `context.json`.
- `report` takes `repository` (report on another repository's runs; a
  public one needs no extra access), `issues-repository` and
  `issues-token` (post the report and miss issues elsewhere, each source
  repository with its own report issue), and `comment: on-problem`
  (comment only on a miss, a failed check or an incomplete report). Miss
  issues outside diffcone's tracker link to a pre-filled diffcone issue.
- `run -o` adds `evidence_not_used` (why the recording was not used, and
  what differed) to the plan it writes, and `diffcone report` shows those
  differences per job.

## [0.3.0] - 2026-10-08

### CI

- `diffcone report --dir DIR` reports on many CI runs of the actions: per
  job, the share pull requests selected and why some were planned from the
  code, and for pushes whether each was checked, its new failures, flaky
  re-runs and misses. It exits 1 on a miss or a check that failed.
- A new `report` action runs it on a schedule over the recent runs'
  artifacts, comments the report on an issue, and opens an issue for each
  miss.
- `run` and `record` write `context.json` (what they did, and why a plan
  was made from the code or a push was not checked) into the results they
  upload; `run` also writes `ran.json`, the plan that ran.
- The verdict `record` keeps for a re-run of the same commit
  (`.diffcone/check.json`) also says whether the push was checked and
  whether the check itself failed, so a re-run reports a failed check as
  one rather than as a miss.

### Execution evidence

- On Windows, `uv python list` run through the path `shutil.which` gives
  (`...\uv.EXE`) is recognised as an inert query, as it is elsewhere; it
  made a third of a Windows suite always selected.

## [0.2.0] - 2026-10-08

### Platforms

- Windows is tested in CI (Python 3.11-3.14): commands are split without
  POSIX escapes, the recorder plugin is copied where a symlink is not
  allowed, the recorder reads Windows command lines and existence checks and
  matches short (8.3) path names, and the actions find Python and the
  venv's `Scripts` directory.

### CI

- `record` takes `check: true` for a default branch whose every push runs
  the full suite: it plans the push's own change from the previous
  recording, records, and fails the job on a new failure the plan did not
  select that fails again when re-run (a flaky one is only reported).
  `fail-on-test-failure: true` fails the job on failing tests, as a plain
  test run would. Pull requests then run only the selection.

### Planning

- A dynamic import through a literal table of tuples is bounded by the
  table: a PEP 562 lazy-export `__getattr__` (`target =
  _LAZY.get(name)`, then `import_module(target[0])`) no longer reaches every
  module. `D.get(key)` reads a literal dict like `D[key]`.
- A lookup by a run-time name on a third-party or standard-library module
  (`getattr(logging, level)`, `dir(builtins)`) no longer selects every test
  that reaches it whenever anything in its import closure changes, unless
  project code stores something on that module.

### Execution evidence

- A test that runs a `python -c` snippet that can't run project code (an
  interpreter version check importing only built-in modules, with nothing
  after the code and no `PYTHONPATH`-like environment) or `uv python list`
  is no longer always selected.
- Editing a module that looks at its own file at import
  (`Path(__file__).resolve()`) no longer replans all its importers from
  the code: that stat records only that the file exists. Any other stat of
  a source file during a test counts as reading it.
- A test that leaves a thread running is no longer always selected;
  instead every test is credited with the code background threads are in
  the middle of while it runs, which also covers a later test running
  beside a thread that loops in one function.

### Discovery

- Test files and directories named after `--` replace `testpaths`, as in
  pytest; a node id, or a path after an option that may take it as its
  value, is reported instead of guessed.
- `--ignore` and `--ignore-glob` (in `addopts` or after `--`) are honoured,
  below the paths pytest starts from, as pytest applies them:
  ignored tests are no longer targets, which `run` reported as selected
  but not collected.
- Importing a library's test helper whose name looks like a test class
  (`from fastapi.testclient import TestClient`, aiohttp's `TestServer`) no
  longer reports an incomplete target list.
- A conftest pytest does not load (a sibling package's tests, outside
  `testpaths`) is no longer read or reported.

## [0.1.0] - 2026-10-07

The first release.

### Planning

- `diffcone plan` compares two snapshots (git revisions, the staged
  `INDEX`, or the `WORKTREE`) and reports which tests and benchmarks a
  change can affect, each with the dependency path or the fallback rule
  that selected it. Both revisions are analysed; analysis never runs
  project code. JSON (`schema_version` 3) and text reports; exit codes
  distinguish a complete plan (0), a degraded one that selects everything
  (1), no plan (2) and a target list that may be short of what the runner
  collects (3).
- Static discovery of pytest tests (configuration, collection rules,
  inheritance, star-imported suites, the fixture chain with fixtures bound
  by alias or import, fixtures and marks inherited from base classes in
  other modules, marks stored in variables, parametrized names supplied to
  fixtures, plugins declared by plugins, and overrides of fixtures that
  pytest and installed plugins request, tests bound by assignment,
  `runTest`, pytest 9's `pytest.toml`, doctests with their fixtures,
  session-wide hooks from any conftest, the `usefixtures` option, plugins
  of sibling packages) and ASV benchmarks (`benchmark_name`, imported
  `setup`, the code a `timeraw_` benchmark runs), without importing
  project code; what it cannot see it reports. A conftest outside the
  source roots keeps its tests always selected, and a changed runner
  configuration or build file outside them selects everything.
  `diffcone discover` writes the targets as a manifest.
- Dependencies the analysis cannot see can be declared in `diffcone.toml`.
- A target depends on everything it depended on at the base too, and on
  its module's import even when a hand-written manifest does not list the
  module; an added import selects the code that now resolves through it;
  a docstring edit selects only where code runs or reads the docstring.
  Unknown manifest keys are errors.
- Indexes, discovery and per-module results are cached under
  `.diffcone/cache/`; a cached plan is identical to an uncached one.
  `diffcone prune --keep REV` shrinks the cache to what planning at given
  commits reads.

### Running and checking

- `diffcone run` plans and runs only the selected targets with pytest or
  ASV; `diffcone validate` runs the whole suite at both snapshots and
  checks the plan against outcome changes (and, with `--coverage`, against
  what each test executed); `diffcone corpus` does so over a commit range.
- `diffcone check` compares a plan with the JUnit XML of a full run, and of
  other selective runs (pytest-testmon's, say), reporting every failure the
  plan missed; Markdown output for CI job summaries.

### Execution evidence (opt-in, Python 3.12+ in the project)

- `diffcone collect` records what each test executed, at a commit;
  `plan --evidence` selects the tests whose record meets the change, and
  plans the rest statically or selects everything, saying which.
  `run --collect` advances the record to the new commit. `--env-var`
  records project variables that change what tests do. Tests that start a
  subprocess or subinterpreter, or leave a thread running, are always
  selected; a shared fixture computed once by one xdist worker is credited
  to every test using it.
- Cython: with a `profile=True` build recorded on Python 3.13+, edits to
  function bodies and to the names a file binds outside functions select
  the tests that executed the code concerned.
- A recording settles what static discovery cannot know (whether a plugin
  collects a class pytest's rules skip), so such plans no longer stop at
  exit 3.

### CI

- Composite GitHub Actions (`actions/record`, `actions/run`,
  `actions/check`) to record nightly on the default branch and plan, run
  and check each pull request against it; see `docs/ci.md`.

[0.4.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.4.0
[0.3.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.3.0
[0.2.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.2.0
[0.1.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.1.0

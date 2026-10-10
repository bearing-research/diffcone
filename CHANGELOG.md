# Changelog

All notable changes to diffcone. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/), and before 1.0 a minor version
may change the report schema, the cache and evidence store formats, or
selection rules.

## [Unreleased]

### Planning

- `diffcone.toml` takes `[[always_run]]` entries: tests (or benchmarks)
  matching a pattern on their id are selected in every plan, with or
  without a recording, and the report gives the entry as the reason and
  how many targets each entry matched. A malformed entry is an analysis
  error, so the plan selects everything rather than quietly planning those
  tests. `diffcone report` computes a job's selected share without them.
- A test is selected when the fixtures, hooks or plugins around it change
  though its own code did not (rule `lifecycle_changed`), for example when
  another test module adds a plugin to `pytest_plugins`. pytest plugin
  modules are dependencies of every test, since their import-time code
  runs for the whole session; a plugin in the repository outside the
  source roots keeps every test always selected.
- An `always_run` entry naming an unknown runner is an analysis error; one
  whose pattern matches nothing in a plan is flagged in the text report.
- `plan --evidence` says the environment was not checked
  (`environment_checked: false`): only `run` can check it.
- A module imported by a name that starts with fixed text
  (`import_module(f"plugins.{name}")`) is one of the modules with that
  prefix, not any module. A new `types.ModuleType` is no project module
  unless it is installed in `sys.modules` under one's name,
  `exec(code, namespace)` no longer reaches the calling module, and
  `m.__dict__["X"] = v` changes only `X`.
- A fixture or function that only empties or fills a module-level dict,
  list or set (`REG.clear()`), or resets another module's variable
  (`mod.X = None`), no longer counts as reading it: an autouse reset
  fixture no longer selects every test when the code filling the registry
  changes.
- An attribute read on an object of unknown type is matched to
  module-level functions, classes and variables only in modules such an
  object can be: modules passed around, obtained by name or named in a
  string, and test modules and conftests, which pytest hands out. Methods
  still match whatever the receiver.
- A method called on an instance attribute (`self._client.get()`) is
  resolved to one class's method when every assignment of the attribute
  creates an instance of that class or of a third-party class; a
  module-level object made by a third-party call is changed only by
  methods of classes deriving from a third-party class.

### Fixed

- A pull request that changes only dependencies selected no tests when the
  source roots don't hold the repository root: a lock file (`uv.lock`,
  `poetry.lock`, `pylock.toml`), a `requirements*` or `environment*` file,
  or a workspace member's `pyproject.toml` outside the roots was ignored.
  Such a change, and a compiled source outside the roots, now selects every
  target, as it already did under a root and in evidence mode. Evidence
  mode now also treats `pylock.toml`, `conda-lock.yml`, `uv.toml` and
  `pytest.toml` this way.
- Under execution evidence, editing a test file selected every test when
  the suite was recorded with a virtual environment inside the checkout
  (`uv run pytest` runs `.venv/bin/pytest`): the recorder took the
  environment's own scripts for project code, so pytest reading files
  during collection looked like the project reading them outside any
  test. The environment is now treated as installed code.
- Under execution evidence, a session hook that lists installed
  distributions (`importlib.metadata.distributions()`) made adding any
  file anywhere select every test. Those scans are no longer recorded,
  and a change under a `*.dist-info`, `*.egg-info` or `*.egg` directory
  now selects every target, in both modes.

- `diffcone check` decided "already failing at the baseline" per test
  function, so a parametrized case that failed at the baseline excused a
  new failure of another case of the same test: a miss reported as clean.
  Cases are now compared one by one.
- The `record` action with `check: true` could pass a push that had a
  miss: a re-run of the job restored the newest recording instead of its
  own commit's (so the kept verdict was lost and the push was re-checked
  against a later baseline), and a check that crashed, or a re-run whose
  JUnit could not be read, was recorded as checked with no miss. A
  re-run's other failures (when the job's pytest arguments name a path
  after an option) no longer count as misses. A recording that is not
  from before the pushed commit is no longer checked against.
- An unexpected error in any command exits 2; `check`, `report`,
  `collect` and `prune` exited 1, which `check` uses for a miss.
- `diffcone report` ignores recording artifacts uploaded by pull-request
  runs (a pull request from a fork runs its own code), counts an artifact
  it cannot read as a problem instead of failing, reports pull-request
  runs whose selected tests pytest did not collect, and keeps a test id
  with backticks inside its code span. The `report` action starts its
  window at the last report that posted, so a run with only a job summary
  (a manual run with posting off) no longer hides the runs before it.
- On Windows, a relative `--command` resolved for `validate` and `corpus`
  was quoted for a POSIX shell.

- Holes in the narrowings of 0.2.0, each a miss: a name taken from a
  dict, list or set written in the code (`getattr(handlers, NAMES[key])`,
  a lazy-export table) is bounded by it only while every use of the table
  is a read, so a table changed through an alias, a helper it is passed
  to, its module (`core.TABLE[k] = v`), `globals()`, `vars()`,
  `sys.modules`, `monkeypatch` or `mock.patch` (an object or a dotted
  string) is no longer read as its literal; and a lookup on a
  standard-library or third-party module (`getattr(logging, name)`) counts
  writes that reach the module indirectly (an alias, a helper it is passed
  to, a loop, `__setattr__`, `vars()`, a module found at run time). Once
  code can reach a module whose name is known only at run time, no table
  is bounded.
- A class obtained by a name nothing resolves (`m.Alt()` on an object of
  unknown type, a lazy `__getattr__` export) reaches its `__init__` and
  `__new__`: a constructor change selected nothing.
- Reading a module's members wholesale (`mod.__dict__[name]`,
  `inspect.getmembers(mod)`, e.g. parametrizing a test over a module's
  functions) is a dependency.
- A change to a dependency, build or CI file outside the source roots
  selected nothing: `dev-requirements.txt`, `Requirements.txt`,
  `requirements.pip`, pandas' `ci/deps/*.yaml`, `hatch.toml`,
  `poetry.toml`, `pdm.toml`, `.env` and `.pth` files, `sitecustomize.py`,
  `noxfile.py`, `.github/workflows/`, and checker configurations pytest
  plugins run as tests (`mypy.ini`, `ruff.toml`). These now select every
  target, as does a build script anywhere under a root (a nested
  `setup.py`) and a lock-file change between `INDEX` and `WORKTREE`.
- Changing the arguments of a call made at import time (`X =
  set_mode("slow")`, a class attribute, a decorator's or default's
  argument) did not select the tests that read the state the callee
  changes.
- A change to PEP 695/696 type parameters (bounds, defaults, their number)
  was no change at all; a bound or default naming project code is now a
  dependency.
- `WORKTREE` did not see edits to files flagged assume-unchanged or
  skip-worktree, and neither did `run`'s check that the working tree
  matches the plan; an edited `.py` file with a non-ASCII name also passed
  that check.
- A submodule under the source roots made every `WORKTREE` plan select
  everything; one checked out at another commit, or with uncommitted
  edits, still counts as a change. A path containing a line break no
  longer fails the plan.

- A test that runs project code in a new process (a script path, `-m
  module` or `-c` code named in the code) did not depend on it; a script
  no module name maps to is now read too, and a Python command whose
  program is built at run time is affected by any change.
- State changed through an argument or a receiver (`register(REGISTRY)`,
  `registry.add(x)`) did not reach its readers, in either mode; nor did a
  change to code that may call such a writer differently, or a write
  through a module attribute and an item (`store._CACHE["k"].append(x)`,
  `del store._CACHE[k]`). A function a module-level variable's
  initialiser calls runs at import, so a change to it reaches the
  module's importers.
- Reading an attribute a class does not define (`C.__doc__`,
  `C.__type_params__`, an attribute a decorator sets) did not depend on
  the class. `C.__subclasses__()`, `C.__mro__` and `C.__bases__` reach the
  classes they return, `C.__dict__[name]` and `obj.__dict__[name]` read
  members as `getattr` does, and `getattr(obj, "__doc__")` reads a
  docstring.
- A parameter or class-body name that shares its name with a module-level
  string table was bounded by that table, though the caller can pass
  anything.
- Code that passes a module to other code (`read(ops, name)`) did not
  depend on that module's functions, nor on what the module's imports
  bind (`api.core.TABLE`).
- An object put in `sys.modules` (assignment, `setdefault`, `update`,
  `monkeypatch.setitem`, `mock.patch.dict`) was ignored. It is now the
  module of that name: lookups on that module see it, and while it stays
  installed, the module's importers depend on the code that installed it.
  Without a recording, code that installs an object under a name computed
  at run time makes every test importing a project module depend on it.
- A function's `__globals__`, a frame's `f_globals`, `pickle.loads`,
  `pkgutil.resolve_name`, `pydoc.locate`, `gc.get_objects()` and
  `importlib.util.module_from_spec` copies are recognised as ways to
  change a module's tables. A lookup by a computed name on a module sees
  objects other code puts on that module.
- Functions obtained from `pickle.load`/`loads` (and cloudpickle, dill,
  joblib) or from a module loaded by a computed file path were invisible
  to planning without a recording; such code is now affected by any
  change. A round trip (`loads(dumps(x))`, a temporary file), pickle
  bytes written in the code and a path in the module's own directory are
  bounded by what they load.
- A local name, parameter or function-local import was taken for a
  module-level import or builtin of the same name.

### Discovery

- An autouse fixture imported into a conftest, a test module or a plugin
  (by name, through star imports at any depth, or as a module attribute)
  was not a dependency of the tests it applies to; nor was a fixture with
  `name=` imported under its function name.
- pytest options in `PYTEST_ADDOPTS`, plugins in `PYTEST_PLUGINS` and
  pytest arguments written into `--command` were not read. `plan` and
  `discover` take `--command`, the CI actions pass it, and the report lists
  what was read; a command whose pytest arguments cannot be found is
  reported.
- Conftests pytest loads at startup (in the `test*` directories of a path
  it starts from) count even when ignored or under `norecursedirs`;
  `--ignore`, `--ignore-glob` and `norecursedirs` apply only below the
  paths pytest starts from, and not under a conftest whose
  `pytest_ignore_collect` can return False.
- Understood now: `.` as an argument, a `.txt` or `.rst` file named as an
  argument (always a doctest), `--doctest-glob` patterns with a directory,
  option values written as separate arguments (`--ignore tests/slow`),
  absolute paths inside the repository, `-o name=value` overrides and paths
  in `addopts`.
- ASV: `setUp` and `TearDown` in any case, and the benchmark class's
  `__init__`, are dependencies of its benchmarks.
- The `pytest_*` methods of an object registered as a plugin
  (`config.pluginmanager.register(...)`), and plugins registered with
  `import_plugin("name")`, apply to every test.
- Code pytest runs while collecting that changes the process (a conftest,
  test module or module they import that sets an environment variable or
  extends `sys.path` at import, a `pytest_generate_tests` that does) is a
  dependency of every test.

### Execution evidence

- Recordings must be made again: the store format changed.
- A process started while a `conftest.py` is imported or in a hook was not
  credited to anything; what it runs now counts as part of that import or
  hook, and one the recording cannot follow makes every change plan from
  the code. pytest-xdist workers are not affected.
- A test using a `multiprocessing` pool, forkserver or forked process that
  an earlier test started is always selected while that process runs.
- Files a library reads while it is imported (matplotlib's `matplotlibrc`)
  were not recorded.
- A virtual environment created inside a source directory hid that
  directory's code, or gave a false "installed copy" error.
- Tests that run code compiled from text no file holds (doctests,
  `--doctest-modules`, `timeit`, a notebook kernel) are always selected
  (rule `text_code`).
- On macOS and Windows, a file opened under another capitalisation was not
  matched.
- A `python -c` probe that runs project code is caught by its own
  recording, and a project's own `sitecustomize.py` no longer stops
  subprocesses from recording themselves. A child recording cut short (a
  full disk) is treated as unreadable.
- A fake test class found without naming it (a base's `__subclasses__()`,
  `gc`, a frame's globals) counts as held by the code that found it.
- A change to code that runs at import, or that ran during an import,
  reaches the tests reading what it writes into another module, Cython
  code included; library code importing a test module by a computed name
  sees changes to it; `pytest_plugins` in a test module selects every test
  when it changes.
- `run --collect` on a recording made with `--reverse-check` runs the
  selected tests again in reverse order, so the advanced records are
  order-checked too.
- Cython: edits to blank lines or `#` lines inside a string in a function
  body were ignored.
- A Python process a test starts with `subprocess` (directly, through
  `asyncio`, or through `uv run`) records itself on Linux and macOS, and
  the test is credited with what it ran instead of being selected for
  every change. So is every test that runs while the process is still
  running (a server or a warm worker another test started). A process
  started another way (`os.system`, a shell, `multiprocessing`, anything
  on Windows), one that cannot record itself (a replaced environment,
  `python -S`, Python before 3.12), and one running project code from a
  `-c` snippet or a script outside the source roots still select their
  test for every change.
- A test run that changes its own environment (a test that installs or
  upgrades a package with the test interpreter) is named: `collect` warns
  with the packages, the recording keeps the environment it started in,
  and `run` says when it meets that environment. The recording is still
  keyed by the environment at the end, so such a run plans from the code.
- A method or attribute added to (or changed or deleted on) a fake class
  in a test module no longer selects every test that ran library code
  calling a method of that name (`x.read()`). It selects only the tests
  that also ran code able to hand that code a fake: code naming the class
  or a subclass. A fake built at import, in a decorator or during
  collection, or one whose class or bases sit outside test code, is
  treated as before.
- A lookup on a standard or third-party module that tests patch
  (`dir(builtins)` or `hasattr(os, name)` in library code, with
  `monkeypatch.setattr(builtins, ...)` in a test) no longer makes every
  change anywhere select every test that ran the lookup. The tests that
  patch the module are selected through their own code. A module written
  at import or in a hook is treated as before, and a change to code that
  runs at import still reaches the lookup when it can run a writer. A
  non-literal `getattr` on such a module now sees changes anywhere in the
  project, not only in its own imports, and a test module's import-time
  change now reaches lookups in library code.
- A test whose fixtures, hooks or plugins change though its own code did
  not is selected under a recording too (`lifecycle_changed`), and code a
  hook ran that changed, or that reads a change, selects every test.
- A module-level cache filled by an earlier test hid the function that
  filled it from later tests' records, in every order: a change to that
  function now reaches the tests reading the cache.
- A docstring-only change selects the tests that read that docstring.
- A lazy-export `__getattr__` whose table only test-time code can change
  no longer counts as importing test modules; `gc.get_stats()`,
  `gc.collect()` and `gc.callbacks` no longer count as walking the object
  graph; `hasattr(obj, "name")` notices only `name`.
- Unpicklers and loaders of a computed path count as code that can obtain
  a test module.

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
- The `report` action failed whenever a job of a reported run had been
  re-run, since the run then holds two artifacts of the same name; it now
  reads the latest attempt's.

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

[0.3.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.3.0
[0.2.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.2.0
[0.1.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.1.0

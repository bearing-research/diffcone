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

### Execution evidence

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

[0.3.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.3.0
[0.2.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.2.0
[0.1.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.1.0

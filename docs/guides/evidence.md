# Execution evidence

Reading the code works well when changes stay local. In a large library
where nearly every module imports a few central ones, a change to a
central module can reach nearly every test, so a plan selects nearly the
whole suite.

Execution evidence fixes this. You record once which functions each test
actually ran, and diffcone selects the tests whose recorded run touches the
change.

!!! info "Requirements"

    Recording needs Python 3.12 or later in your project's test
    environment. diffcone itself can run on another Python version.

## Record

From a clean checkout of a commit, run your suite under the recorder.
Arguments after `--` are passed to pytest. The examples use the default
source root; for a `src` layout, add `--source-root src --source-root .`
to every command here, `collect` included (a recording keeps its source
roots, and plans must use the same ones):

```bash
diffcone collect --command "uv run pytest" -- -n 8
```

This writes a recording to `.diffcone/evidence/`: for each test, the
functions it ran and the files it read. To record a different commit
without checking it out, add `--rev REV`. `diffcone evidence` lists your
recordings:

```bash
diffcone evidence
```

`collect` refuses to record a commit with a file it can't analyse, since
the code in it couldn't be matched to what the tests ran. If pytest stops
early (an interrupt, an internal error, a usage error), nothing is
written.

Plans find each test's recording by its pytest node ID. If your
repository has no pytest configuration and a directory above it has one,
pytest takes that directory as its rootdir and every node ID starts with
the path to your repository, so plans find no recording for any test.
`collect` warns when none of the recorded tests matches a test it
discovers; add `--rootdir=.` to the command, or give the repository a
pytest configuration of its own.

A recording made by an earlier version of diffcone may not be readable by
a later one. `diffcone evidence` and `--evidence auto` then skip it (so
`auto` fails with "no evidence recording" if it was the only one), and
naming its path is an error. Record again.

Some tests behave differently depending on what ran before them. Add
`--reverse-check` to run the suite a second time in reverse order; tests
whose recordings differ are marked unstable and always selected.

## Plan with a recording

```bash
diffcone plan --base main --head WORKTREE --discover pytest --evidence auto
```

`--evidence auto` uses the recording, made with the same source roots, from
the nearest ancestor of the head (or pass a recording's path). The
recording doesn't need to be at your base: diffcone also accounts for the
changes made since it was recorded.

`plan` doesn't run anything, so it can't check that your test environment
is the one the recording was made in: the report says
`environment NOT checked` (`environment_checked: false` in JSON). Its
selection holds only in the recorded environment. `diffcone run` checks it
before any test runs (see below).

A test is selected when, in its recorded run, it:

- ran a function that changed;
- read a value or definition that changed;
- looked up a name dynamically where something the lookup can see
  changed; or
- read a file that changed.

It is also selected when its own code, class or module changed, when the
fixtures, hooks or plugins around it changed, and when an
[`[[always_run]]`](../reference/declarations.md#tests-to-run-on-every-change)
entry names it.

Some changes can't be judged from a recording, and diffcone says so in the
report:

- Code that runs when a module is imported is planned from the code, as
  without evidence. The exception is a changed constant or class
  attribute, which selects the tests that read it.
- The recorder starts before pytest loads any plugin (`-p` in `addopts`,
  `PYTEST_ADDOPTS` or your command) and before your own
  `sitecustomize.py`, so what those import is recorded. If a module of
  your project was imported before that (by a `.pth` file, or under
  `python -S`), `collect` warns and lists it in `preloaded_modules`;
  changes to it are always planned from the code.
- If one test fills a cache and later tests read it, every test that
  reads it counts. This covers caches in module variables, on objects and
  classes, in closures, in function attributes and in `cached_property`,
  and a cache filled from a file that changed. `functools.cache` and
  `lru_cache` are cleared before each test instead, also over another
  wrapper (`singledispatch`, `staticmethod`, `partial`, a class).
  Caches kept by an unknown memoising decorator, or filled by code
  diffcone can't follow from the code that ran the change (a method of an
  object whose class the code doesn't tell, a table of callbacks), are
  not seen, nor
  is what the recorder can't watch: files opened by compiled code, state
  kept on disk between tests (see
  [limitations](../limitations.md#state-shared-between-tests)).
- A module your index doesn't read (outside the source roots, or kept out
  with `[index] exclude`) that a test imports: a later test may use it
  without running a line of it, so a change to it selects every test.
- Tests with no recording (new tests, for example), tests whose
  recording was unstable, and tests that failed or errored while they were
  recorded are always selected: a failing test's recording stops where it
  failed. So is a test only some of whose cases ran while it was recorded
  (the others deselected, or not reached): its recording covers only
  those cases.
- A recording describes the tests as they ran with the arguments it was
  made with. If you plan or run with pytest options that change what a
  test does (a conftest option such as `--runslow` or `--backend`), make
  the recording with the same options.
- Processes a test starts:
    - **Followed.** A Python process started with `subprocess` (directly,
      through `asyncio`, or through `uv run`) records itself on Linux and
      macOS. The test is credited with what it ran, and so is every test
      that runs while it is still running.
    - **Always selected.** A test that starts a process any other way
      (`os.system`, a shell, `multiprocessing`, or, on Windows, any other
      process),
      or whose child can't record itself. So is every test that runs
      while such a process is still running (a `multiprocessing` pool
      reused by a later test), and a test whose child runs your code from
      a `python -c` snippet or a script outside your source roots.
    - **Ignored.** A `python -c` snippet that can't run your code (a
      version check), package-manager commands (`uv sync`, `uv pip`,
      `uv python list`), and programs that only ask about the machine or
      the repository (`uname`, `ps`, `locale -a`, read-only `git` commands
      such as `git rev-parse`), on every platform.

    A project `sitecustomize.py` still runs in every child.
- A process started outside every test (by a `conftest.py` as it is
  imported, or in a hook) counts as part of that import or hook: a change
  to what it ran is planned as a change to code that ran there. One that
  can't be followed makes every change do so, which can select every
  test.
- A test that runs code from text rather than a file (doctests, including
  pytest's `--doctest-modules`, `timeit` statements, a notebook kernel) is
  always selected: that code can use any name of your code. Annotations
  evaluated from text (string annotations, which pydantic and FastAPI
  evaluate) are not: the test is credited with the function or class they
  belong to and the classes and functions they name.
- Code that background threads are running while a test runs is credited
  to that test.
- Code that ran while pytest collected a test file (building the ids of
  parametrized tests, an `ids=` function, a `pytest_generate_tests` hook
  and what it calls) decides that file's tests: a change to it, or to what
  it reads, selects the tests of every file it ran for, and the tests
  reading anything it stored. So does a change to a file read then. It
  counts as a hook's (below) when what ran it, or what that can run, sets
  something for the whole process, such as an environment variable, or
  reaches pytest's configuration or session.
- Code that ran outside every test in a pytest hook (`pytest_configure`,
  `pytest_collection_modifyitems`, the collection of a directory) can
  affect any test: a change to it, or to what it reads, selects every
  test, and so does a change to `pytest_plugins`. Reading a function's
  annotations there (as pytest does on Python 3.14) is not running the
  function: only a change to the annotations, or to what they name,
  counts.
- Changes to compiled sources (except [Cython](#cython) with a profiled
  recording), build files, dependencies and configuration select every
  test.
- On a file system that ignores case (macOS and Windows by default), a file
  a test opened under another spelling (`Data/Expected.TXT`) still counts
  as the file it is. A file read through a symlink, or a symlinked
  directory, counts as the file it really is as well as under the name it
  was opened by; a link a test reads with `os.readlink` is the link.
- Files are recorded however a test reaches them: opened, stat'ed, listed,
  through a `sqlite3` `file:` URI, through a directory descriptor
  (`os.listdir(fd)`, `dir_fd=`, `os.fstat`) or as entries of a directory
  it scanned with `os.scandir` (`os.walk`, `glob`, `pathlib`): an edit to
  any file of a scanned directory selects the test. A descriptor the
  recorder cannot trace to a path makes the test always selected.

ASV benchmarks are always planned from the code.

## Run with a recording

```bash
diffcone run --base main --head HEAD --discover pytest \
    --command "uv run pytest" --evidence auto -- -n 8
```

A recording is only valid in the environment it was made in. Before any
test runs, even when the plan selects nothing, `run` checks that these
match the recording:

- the Python implementation and version, and its `-O` setting;
- the platform and machine;
- the installed packages;
- your `PYTHONPATH`;
- a few environment variables (`TZ`, `LANG`, `LC_ALL`,
  `PYTHONWARNINGS`, `VIRTUAL_ENV`, `PYTEST_ADDOPTS`).

It sets `PYTHONHASHSEED` to the recorded value itself. With `--dry-run`,
nothing is checked.

If the environment doesn't match, or the check couldn't run, `run` falls
back to a plan from the code and tells you what differed. If that plan's
discovery may be incomplete, it runs the whole suite instead, unless you
pass `--allow-incomplete-discovery`.

A package installed in editable mode from outside your repository
(`pip install -e ../lib`) counts as installed: diffcone doesn't see edits
to its code, as with any other installed package. If your tests also
depend on your own environment variables, record them with `--env-var`:

```bash
diffcone collect --command "uv run pytest" --env-var MYAPP_MODE -- -n 8
```

The environment is the one at the end of the recording. If your tests
install or upgrade a package while they run (a test that runs `uv sync`
or `pip install` with your test interpreter, for example), `collect` warns
and names the packages: a fresh install from your lock file then never
matches the recording, and every run falls back to a plan from the code.
`run` says so too when it meets the environment the recording started in.
Stop the tests from changing their own environment to use the recording.

## Keep the recording current

Recording the whole suite for every commit would be slow. With `--collect`,
`run` records the tests it runs and writes a new recording for the head:
fresh records for the tests it ran, and the earlier ones for the tests it
didn't select, which behave the same at the head.

```bash
diffcone run --base main --head HEAD --discover pytest \
    --command "uv run pytest" --evidence auto --collect -- -n 8
```

This needs a clean checkout of the head, and the same `--command`, pytest
arguments and source roots the recording was made with. If the recording
was made with `--reverse-check`, `run --collect` runs the selected tests a
second time in reverse order, so the new records are checked the same way.

If the environment differs, or pytest is interrupted or ends with an error
of its own (not a test failure), no recording is written. Like `collect`,
`run --collect` adds `--maxfail=0`, so an `-x` or `--maxfail` in your
arguments or `addopts` doesn't stop it at a failure. If something else
stops it early (`--sw`, a plugin), the tests it didn't finish have no
record in the new recording, and every plan selects them until they are
recorded again. A test that failed when it was recorded keeps that mark,
and stays selected, until you record the full suite again.

A test has a record only if all of its cases ran. A case left out by `-k`,
`-m` or `--deselect` (on the command line, in `addopts` or in
`PYTEST_ADDOPTS`) or by a conftest leaves the whole test without a record,
so every plan selects it. `collect` says how many collected tests have no
record. `PYTEST_ADDOPTS` is part of the recorded environment: a run with a
different value runs the static plan instead.

In CI, a common setup records the full suite once a night on your default
branch and plans each pull request from it. See [CI](../ci.md).

## Cython

diffcone can also select tests for changes to Cython code (`.pyx`, `.pxd`,
`.pxi`) when the recording was made with a build compiled with Cython's
`profile=True` directive, on Python 3.13 or later. Your test runs can keep
using your normal build.

With such a recording:

- a change inside a Cython function selects the tests that ran it (or ran a
  function that calls it);
- a change outside functions (an import, a constant, a declaration, a new
  function) selects the tests that ran code using the changed name.

Changes diffcone can't attribute to a name, such as a compiler directive or
a C source file, select every test. diffcone doesn't build your project;
you provide the profiled build for recording.

## Shallow clones

Planning needs the recorded commit to be present in git. In a shallow clone
(common in CI), fetch it first; diffcone names the commit when it's
missing:

```bash
git fetch --depth=1 origin <commit>
```

In a shallow clone, `--evidence auto` can't tell which commits are
ancestors, so pass the recording's path instead.

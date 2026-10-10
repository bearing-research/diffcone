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
written. A recording made by an earlier version of diffcone may not be
readable by a later one; `diffcone evidence` then leaves it out, and you
need to record again.

Some tests behave differently depending on what ran before them. Add
`--reverse-check` to run the suite a second time in reverse order; tests
whose recordings differ are marked unstable and always selected.

## Plan with a recording

```bash
diffcone plan --base main --head WORKTREE --discover pytest --evidence auto
```

`--evidence auto` uses the recording, made with the same source roots, from
the nearest ancestor of the head (or pass a recording's path). The recording doesn't need to be at your base:
diffcone also accounts for the changes made since it was recorded.

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

- Code that runs on import is planned from the code instead, as without
  evidence.
- Tests with no recording (new tests, for example) and tests whose
  recording was unstable are always selected.
- A Python process a test starts with `subprocess` (directly, through
  `asyncio`, or through `uv run`) records itself on Linux and macOS, and the test is credited
  with what it ran, as is every test that runs while it is still running.
  A test that starts a process any other way (`os.system`, a shell,
  `multiprocessing`, or anything on Windows), or whose child can't record
  itself, is always selected, and so is every test that runs while such a
  process is still running (a `multiprocessing` pool reused by a later
  test). So is a test whose child runs your code from a `python -c`
  snippet or a script outside your source roots. A `python -c` snippet
  that can't run your code (a version check) or `uv python list` doesn't
  count. A project `sitecustomize.py` still runs in every child.
- A process started outside every test (by a `conftest.py` as it is
  imported, or in a hook) counts as part of that import or hook: a change
  to what it ran is planned as a change to code that ran there. One that
  can't be followed makes every change do so, which can select every
  test.
- A test that runs code from text rather than a file (doctests, including
  pytest's `--doctest-modules`, `timeit` statements, a notebook kernel) is
  always selected: that code can use any name of your code.
- Code that background threads are running while a test runs is credited
  to that test.
- Code that ran outside every test, in a pytest hook or while pytest
  collected, can affect any test: a change to it, or to what it reads,
  selects every test, and so does a change to `pytest_plugins`.
- Changes to compiled sources (except [Cython](#cython) with a profiled
  recording), build files, dependencies and configuration select every
  test.
- On a file system that ignores case (macOS and Windows by default), a file
  a test opened under another spelling (`Data/Expected.TXT`) still counts
  as the file it is.

ASV benchmarks are always planned from the code.

## Run with a recording

```bash
diffcone run --base main --head HEAD --discover pytest \
    --command "uv run pytest" --evidence auto -- -n 8
```

A recording is only valid in the environment it was made in. Before any
test runs, even when the plan selects nothing, `run` checks that the Python
implementation and version and its `-O` setting, the platform and machine,
the installed packages, your `PYTHONPATH` and a few environment variables
(`TZ`, `LANG`, `LC_ALL`, `PYTHONWARNINGS`) match the recording; it sets
`PYTHONHASHSEED` to the recorded value itself. If they don't match, it
falls back to a plan from the code and tells you what differed. If that
plan's discovery may be incomplete, it runs the whole suite instead. A package installed in
editable mode from outside your repository (`pip install -e ../lib`)
counts as installed: diffcone doesn't see edits to its code, as with any
other installed package. If your tests also depend on your own
environment variables, record them with `--env-var`:

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
If the environment differs or pytest stops early, no recording is written.

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

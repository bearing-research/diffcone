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
Arguments after `--` are passed to pytest:

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

Some tests behave differently depending on what ran before them. Add
`--reverse-check` to run the suite a second time in reverse order; tests
whose recordings differ are marked unstable and always selected.

## Plan with a recording

```bash
diffcone plan --base main --head WORKTREE --discover pytest --evidence auto
```

`--evidence auto` uses the recording from the closest earlier commit (or
pass a recording's path). The recording doesn't need to be at your base:
diffcone also accounts for the changes made since it was recorded.

A test is selected when, in its recorded run, it:

- ran a function that changed;
- read a value or definition that changed;
- looked up a name dynamically where a name was added or removed; or
- read a file that changed.

Some changes can't be judged from a recording, and diffcone says so in the
report:

- Code that runs on import is planned from the code instead, as without
  evidence.
- Tests with no recording (new tests, for example), tests whose recording
  was unstable, and tests that started a subprocess are always selected.
- Changes to compiled sources, build files and configuration select every
  test.

ASV benchmarks are always planned from the code.

## Run with a recording

```bash
diffcone run --base main --head HEAD --discover pytest \
    --command "uv run pytest" --evidence auto -- -n 8
```

A recording is only valid in the environment it was made in. Before any
test runs, `run` checks that the Python version, the installed packages and
a few environment variables (`PYTHONHASHSEED`, `TZ`, `LANG`, `LC_ALL`)
match the recording. If they don't, it falls back to a plan from the code
and tells you what differed. If your tests also depend on your own
environment variables, record them with `--env-var`:

```bash
diffcone collect --command "uv run pytest" --env-var MYAPP_MODE -- -n 8
```

## Keep the recording current

Recording the whole suite for every commit would be slow. With `--collect`,
`run` records the tests it runs and writes a new recording for the head:
fresh records for the tests it ran, and the earlier ones for the tests it
didn't select, which behave the same at the head.

```bash
diffcone run --base main --head HEAD --discover pytest \
    --command "uv run pytest" --evidence auto --collect -- -n 8
```

This needs a clean checkout of the head and the same pytest arguments the
recording was made with. If the environment differs or pytest stops early,
no recording is written.

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

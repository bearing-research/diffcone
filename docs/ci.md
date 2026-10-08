# CI with GitHub Actions

diffcone provides three composite actions for a common setup: record the
full test suite once a night on your default branch, then run only the
affected tests on each pull request, using that recording.

| Action | Where it runs | What it does |
|---|---|---|
| `bearing-research/diffcone/actions/record` | A nightly job on the default branch | Records the full suite with [execution evidence](guides/evidence.md) and saves it with `actions/cache`. |
| `bearing-research/diffcone/actions/run` | Each pull request | Restores the latest recording, plans the pull request, and runs the selected tests. |
| `bearing-research/diffcone/actions/check` | Optional, after a full run | Checks that every test that failed in a full run was selected, and writes a summary. |

Each action installs the diffcone version that matches its own tag, so
reference all three by the same release tag (or commit), never by a
branch.

## Record nightly

```yaml title=".github/workflows/diffcone-record.yml"
name: diffcone record

on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  record:
    runs-on: ubuntu-latest
    cache-mode: write
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10.2.0
      - run: uv sync --locked
      - uses: bearing-research/diffcone/actions/record@v0.1.0
        with:
          key-prefix: diffcone-ubuntu
          command: uv run python -m pytest
          pytest-args: -n auto
```

`key-prefix` names the recording; use one per test environment (operating
system, Python version) if you record several. For a `src` layout, add
`source-roots: src .` to both `record` and `run`. If the recording fails (a
collection error, for example), nothing is saved and pull requests keep
using the previous one.

## Run on pull requests

```yaml title=".github/workflows/tests.yml"
on: pull_request

jobs:
  affected-tests:
    runs-on: ubuntu-latest
    cache-mode: read
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v10.2.0
      - run: uv sync --locked
      - uses: bearing-research/diffcone/actions/run@v0.1.0
        with:
          key-prefix: diffcone-ubuntu
          command: uv run python -m pytest
          pytest-args: -n auto
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: diffcone-results
          path: diffcone-results
```

The job fails when a selected test fails, like any test job. The plan and
the run's JUnit XML are written to `diffcone-results/`; upload them with
`if: always()` so they're kept when tests fail.

!!! important "Match the recording"

    `command` and `pytest-args` must be the same in both workflows, and the
    environment must match: the same Python version and installed packages.
    Installing from a lock file makes this automatic. When the environment
    differs, or the pull request changes the lock file, diffcone runs a
    plan from the code instead of from the recording.

## Check against a full run (optional)

While you build trust in the selection, keep your existing full test job
and compare: did diffcone select every test that failed? Have the full job
write JUnit XML (`--junitxml=full.xml`) and upload it, then add:

```yaml
  diffcone-check:
    needs: [full-tests, affected-tests]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v8
        with:
          name: full-junit
          path: full
      - uses: actions/download-artifact@v8
        with:
          name: diffcone-results
          path: diffcone-results
      - uses: bearing-research/diffcone/actions/check@v0.1.0
        with:
          full: full/full.xml
```

The check writes its result to the job summary. A miss is a new failure
of the full run that the plan didn't select, or that diffcone's own run
didn't execute. It doesn't fail the workflow unless you set
`fail-on-miss: true`. Tests that already failed in
the nightly run are reported as already failing, not as misses.

## Or: record on every push to the default branch

If your default branch already runs the full suite on every push, that run
can be the recording, and pull requests run only the selected tests. Each
push then checks the plan for its own change against the full run, so a
test diffcone should have selected fails the default branch's workflow.

```yaml title=".github/workflows/tests.yml"
on:
  push:
    branches: [main]
  pull_request:

jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10.2.0
      - run: uv sync --locked
      - if: github.event_name == 'push'
        uses: bearing-research/diffcone/actions/record@v0.2.0
        with:
          key-prefix: diffcone-ubuntu
          command: uv run python -m pytest
          pytest-args: -n auto
          check: true
          fail-on-test-failure: true
      - if: github.event_name == 'pull_request'
        uses: bearing-research/diffcone/actions/run@v0.2.0
        with:
          key-prefix: diffcone-ubuntu
          command: uv run python -m pytest
          pytest-args: -n auto
```

On a push, `record` with `check: true`:

1. restores the newest recording and plans the commit's own change (its
   first parent to it) from it;
2. runs the full suite under the recorder and saves the new recording;
3. compares the plan with the full run: a new failure the plan didn't
   select is run again, and if it fails again it is a miss, which fails the
   job. One that passes the second time is reported as flaky. Tests that
   already failed in the previous push's run are not misses.

With `fail-on-test-failure: true` a failing test fails the job too, as a
plain test run would, after the recording is saved.

As with the nightly setup, only runs on the default branch can save the
recording; pull requests only restore it.

## How the cache is shared

Each recording is saved with `actions/cache` under its own key,
`<key-prefix>--<commit>-<run>`, and pull requests restore the newest one
with their prefix (`diffcone-ubuntu` never restores a recording of
`diffcone-ubuntu-py312`). If a recording's commit is no longer on the
branch (after a force push), the run plans without it and says so. A
shallow checkout is deepened until the base revision is there. GitHub lets pull requests,
including those from forks, read caches saved on the default branch but
never write them, so a pull request can't change the recording. The
`cache-mode` settings above give each job only the access it needs.

The saved directory also holds diffcone's analysis cache, pruned to the
recorded commit, so pull requests start planning without re-reading
unchanged files.

## Action inputs

`record` and `run` take:

| Input | Description |
|---|---|
| `key-prefix` | Required. The name of the recording in the cache. |
| `command` | Required. The pytest command, without arguments. |
| `pytest-args` | Arguments for pytest, as one shell-quoted string. |
| `source-roots` | Space-separated [source roots](guides/planning.md#source-roots) (default `.`). |
| `diffcone-ref` | Install diffcone from this git ref instead of the action's own version. |

`record` also takes `env-vars` (environment variables your tests depend
on, see [execution evidence](guides/evidence.md#run-with-a-recording)),
`junit` (where to keep the recording run's JUnit XML; default
`.diffcone/baseline.xml`), `check` (default `false`: check each push's
change against its full run, as above) and `fail-on-test-failure` (default
`false`). `run` also takes `base` (default `HEAD^1`, the
target branch of the pull request's merge commit) and
`allow-incomplete-discovery` (default `false`; see
[when the target list may be short](reference/discovery.md#when-the-target-list-may-be-short)).

`check` takes `full` (required: the full run's JUnit XML), `results` (the
directory `run` wrote; default `diffcone-results`), `others` (more
selective runs to compare, as `NAME=JUNIT` pairs), and `fail-on-miss`.

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
system, Python version) if you record several. If the recording fails (a
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

## How the cache is shared

The recording is saved with `actions/cache` under `<key-prefix>-<commit>`,
and pull requests restore the newest one. GitHub lets pull requests,
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
on, see [execution evidence](guides/evidence.md#run-with-a-recording)) and
`junit` (where to keep the recording run's JUnit XML; default
`.diffcone/baseline.xml`). `run` also takes `base` (default `HEAD^1`, the
target branch of the pull request's merge commit) and
`allow-incomplete-discovery` (default `false`; see
[when the target list may be short](reference/discovery.md#when-the-target-list-may-be-short)).

`check` takes `full` (required: the full run's JUnit XML), `results` (the
directory `run` wrote; default `diffcone-results`), `others` (more
selective runs to compare, as `NAME=JUNIT` pairs), and `fail-on-miss`.

# CI with GitHub Actions

diffcone provides composite actions for a common setup: record the
full test suite once a night on your default branch, then run only the
affected tests on each pull request, using that recording.

| Action | Where it runs | What it does |
|---|---|---|
| `bearing-research/diffcone/actions/record` | A nightly job on the default branch | Records the full suite with [execution evidence](guides/evidence.md) and saves it with `actions/cache`. |
| `bearing-research/diffcone/actions/run` | Each pull request | Restores the latest recording, plans the pull request, and runs the selected tests. |
| `bearing-research/diffcone/actions/check` | Optional, after a full run | Checks that every test that failed in a full run was selected, and writes a summary. |
| `bearing-research/diffcone/actions/report` | Optional, on a schedule | Reports on the recent runs of `run` and `record`, and opens an issue for each miss. |

Each action installs the diffcone version that matches its own tag, so
reference them all by the same release tag (or commit), never by a
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
      - uses: bearing-research/diffcone/actions/record@v0.3.0
        id: record
        with:
          key-prefix: diffcone-ubuntu
          command: uv run python -m pytest
          pytest-args: -n auto
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: diffcone-ubuntu
          path: ${{ steps.record.outputs.results }}
```

The upload is optional: it keeps a description of the recording for
[a report on recent runs](#report-on-recent-runs).

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
      - uses: bearing-research/diffcone/actions/run@v0.3.0
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
    differs, diffcone runs a plan from the code instead of from the
    recording; when the pull request changes the lock file or another
    dependency, build or CI file diffcone recognises (see
    [discovery](reference/discovery.md)), it runs every test.

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
      - uses: bearing-research/diffcone/actions/check@v0.3.0
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
        id: record
        uses: bearing-research/diffcone/actions/record@v0.3.0
        with:
          key-prefix: diffcone-ubuntu
          command: uv run python -m pytest
          pytest-args: -n auto
          check: true
          fail-on-test-failure: true
      - if: github.event_name == 'pull_request'
        uses: bearing-research/diffcone/actions/run@v0.3.0
        with:
          key-prefix: diffcone-ubuntu
          command: uv run python -m pytest
          pytest-args: -n auto
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: diffcone-ubuntu
          path: ${{ steps.record.outputs.results || 'diffcone-results' }}
```

On a push, `record` with `check: true`:

1. restores the newest recording and plans the commit's change from it;
2. runs the full suite under the recorder;
3. compares the plan with the full run. A new failure the plan didn't
   select is run again, with the same pytest options. If it fails again,
   it is a miss, and the job fails. If it passes the second time, it is
   reported as flaky. Failures the recorded run already had are not
   misses;
4. saves the new recording, together with the verdict. Re-running a job
   that found a miss finds it again.

The check is skipped, with a note, when there is nothing to compare with:
no recording yet, a recording diffcone can't use (made with other source
roots, for example), or a commit with no parent. The recording is still
made and saved.

With `fail-on-test-failure: true` a failing test fails the job too, as a
plain test run would, after the recording is saved. A test file that can't
be collected stops the recording itself, which fails the job.

As with the nightly setup, only runs on the default branch can save the
recording; pull requests only restore it.

## Report on recent runs

Once every job runs through diffcone, a scheduled `report` job reads what
the jobs uploaded and sums it up: for pull requests, the share of tests
each job selected (leaving out the tests
[`[[always_run]]`](reference/declarations.md#tests-to-run-on-every-change)
names) and how often it planned without a recording (and what
differed); for recordings, whether each pushed commit was checked, its new
failures, flaky tests and misses. It adds the report as a comment on an
issue labelled `diffcone-report`, and opens one issue, labelled
`diffcone-miss`, for each test diffcone missed.

```yaml title=".github/workflows/diffcone-report.yml"
name: diffcone report

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

permissions:
  actions: read
  issues: write

jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: bearing-research/diffcone/actions/report@v0.3.0
        with:
          workflow: tests.yml
```

The report reads the artifacts whose names start with `diffcone-`, so
upload the results of `run` and `record` (as in the workflows above) under
a name that starts with `diffcone-` and differs per job, and per cell of a
matrix: `diffcone-${{ matrix.os }}-py${{ matrix.python-version }}`, for
example. The report has one row per name.

Each report covers the runs that completed since the last successful
report that posted started, so a run still going when one report starts is
in the next. A report with both `report-label` and `miss-label` empty
writes only the job summary and leaves its runs to the next report. If an
artifact can't be downloaded, the report says so and the job fails, and
the next report covers the same runs again. Artifacts that have expired
are not reported. The report leaves out recording artifacts uploaded by
pull-request runs (a pull request from a fork runs its own code), and
artifacts whose files it cannot read, which it counts as a problem.

`report` takes `hours` (the window for the first report: runs that
completed in this many hours before it; default `24`), `workflow` (only
that workflow's runs; default all), `artifact-prefix` (default
`diffcone-`), `report-label` and `miss-label` (set either to an empty
string to skip that issue), and `comment` (`always`, the default, or
`on-problem`: comment only when there is a miss, a failed check, a
pull request whose selected tests pytest did not collect, or an incomplete
report; the job summary always has the report). It reads what
`run` and `record` upload from the same release on, so give all the
actions the same tag.

Each miss issue has a link that opens a new issue on diffcone's tracker
with the test and the plan's reason filled in. Nothing is sent until you
review it and submit it.

### Report on another repository

`repository` reports on another repository's runs, and
`issues-repository` posts somewhere else: a team can watch several
projects from one place. A public repository's runs need no extra access;
posting to another repository needs a token that can write its issues
(`issues-token`, for example a fine-grained token saved as a secret).
Issues posted elsewhere name the repository the runs came from, and each
repository gets its own report issue.

```yaml
      - uses: bearing-research/diffcone/actions/report@v0.3.0
        with:
          repository: my-org/my-project
          workflow: tests.yml
          comment: on-problem
```

To read the same report locally, download the artifacts of some runs into
one directory per run and run `diffcone report`:

```bash
gh run download 123456 --pattern 'diffcone-*' --dir runs/123456
diffcone report --dir runs
```

## How the cache is shared

Each recording is saved with `actions/cache` under its own key,
`<key-prefix>--<commit>-<run id>-<attempt>`, and pull requests restore the newest one
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

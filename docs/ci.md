# diffcone in GitHub Actions

Roadmap item 9. The shape: record evidence once a night on the default
branch, and let every pull request opened the next day plan against it. A
recording at commit C plans any pair of later snapshots (diffcone plans
C -> base and C -> head), so one recording serves the whole day.

**Status: the actions below have been run locally, step by step, against a
small repository (record, a pull request that breaks a test, the selective
run, the check). They have not yet run on GitHub**, and the pandas example
has not run anywhere. "To check on the first run" lists what only a real
run can tell.

## The three actions

All three install diffcone into a virtual environment of the runner's own
Python, from the copy of this repository the runner downloaded for the
action (`GITHUB_ACTION_PATH`), so `actions/record@v0.1.0` runs diffcone
0.1.0; `diffcone-ref` installs another git ref instead. The project's tests
run in the project's environment through `command`. Reference the actions
by a release tag (or a commit), never by a branch: the recording and the
pull-request runs must use the same diffcone.

Caches follow GitHub's rules: a scheduled run on the default branch is a
trusted trigger and saves the recording; a pull request, a fork's
included, restores caches of the default branch read-only and can never
overwrite them. `cache-mode` (per workflow or job) grants each job only
what it needs: `write` for the recording, `read` for pull-request jobs.

* `bearing-research/diffcone/actions/record` (nightly, default branch):
  `diffcone collect` with the job's pytest command and arguments, plus
  `--junitxml` into `.diffcone/baseline.xml`; a plan of the commit against
  itself, so its index and discovery are cached; `diffcone prune --keep
  HEAD`; then `actions/cache/save` of `.diffcone/` under
  `<key-prefix>-<sha>`. A recording that fails (a collection error, a
  crashed worker, a dirty checkout) exits non-zero and nothing is saved, so
  pull requests keep the last good one. On pandas the saved directory is
  about 190 MB, 29 MB compressed.
* `bearing-research/diffcone/actions/run` (pull request): restores the
  newest `<key-prefix>-*` entry, fetches the recorded commit if the
  checkout lacks it, writes the plan to `diffcone-results/plan.json`, and
  runs the selected tests with `--junitxml=diffcone-results/diffcone.xml`.
  The base defaults to `HEAD^1`, the first parent of the merge commit
  `actions/checkout` makes for a pull request. Like any test step it fails
  when a selected test fails, so upload `diffcone-results/` with
  `if: always()`. With no entry to restore it plans statically.
* `bearing-research/diffcone/actions/check` (after both runs): `diffcone
  check` of the plan against the full run's JUnit, with the nightly run as
  the baseline and diffcone's own run (and any other selector's) beside
  it; the Markdown verdict goes to the job summary and `verdict.json` next
  to the results. It fails only with `fail-on-miss: true`.

The environment has to be the same in the recording and in the pull
request: the same interpreter, the same installed distributions, and the
variables named in `env-vars`. A pull request that changes the lock file
therefore uses no evidence, which is right: its tests may behave
differently anywhere. When the environment differs, `diffcone run` falls
back to the static plan, or to the whole suite when static discovery may
be short of what pytest collects.

## pandas, on a fork

Covers the `ubuntu-24.04` / `py313` job's `not single_cpu` step. pandas
installs from `pixi.lock`, so nightly and pull-request environments match
unless the lock changes, and its editable build is not part of the
fingerprint. `PANDAS_FUTURE`, `PYTHONDEVMODE` and `PYTHONWARNDEFAULTENCODING`
change what tests do, so they are recorded and checked. Cython edits select
everything: the build is the ordinary one, without `profile=True` (Cython
evidence also needs Python 3.13+, where a profiled build reports to
`sys.monitoring`).

### `.github/workflows/diffcone-record.yml` (new)

```yaml
name: diffcone record
on:
  schedule:
    - cron: "0 0 * * *"
  workflow_dispatch:
permissions:
  contents: read
jobs:
  record:
    runs-on: ubuntu-24.04
    cache-mode: write
    timeout-minutes: 240
    env:
      LANG: C.UTF-8
      PANDAS_FUTURE: default
      MESONPY_EDITABLE_VERBOSE: 1
      PYTHONDEVMODE: 1
      PYTHONWARNDEFAULTENCODING: 1
      QT_QPA_PLATFORM: offscreen
      PANDAS_MOTO_URL: "http://localhost:5000"
    # services: the same mysql, postgres and moto services as unit-tests.yml's ubuntu job
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: ./.github/actions/setup-pixi
        with:
          environment: py313
      - run: pixi run --environment py313 build-pandas --editable
      - uses: bearing-research/diffcone/actions/record@v0.1.0
        with:
          key-prefix: pandas-ubuntu-24.04-py313
          command: pixi run --environment py313 python -m pytest
          pytest-args: -r fE --numprocesses=auto --dist=worksteal -m "not single_cpu" pandas
          env-vars: PANDAS_FUTURE PYTHONDEVMODE PYTHONWARNDEFAULTENCODING
```

### Changes to `.github/workflows/unit-tests.yml`

1. The full run of the same step writes JUnit. pytest reads
   `PYTEST_ADDOPTS`, so neither `run-tests` nor the pixi task changes; on
   the `Test (not single_cpu)` step:

   ```yaml
   env:
     PANDAS_MOTO_URL: "http://localhost:5000"
     PYTEST_ADDOPTS: >-
       ${{ matrix.platform == 'ubuntu-24.04' && matrix.environment == 'py313'
           && !matrix.name && '--junitxml=full.xml' || '' }}
   ```

   and after it:

   ```yaml
   - uses: actions/upload-artifact@v7
     if: >-
       always() && matrix.platform == 'ubuntu-24.04'
       && matrix.environment == 'py313' && !matrix.name
     with:
       name: full-junit
       path: full.xml
   ```

2. A job beside `ubuntu`, with the record job's `env`, `services` and
   setup steps (checkout with `fetch-depth: 0`, setup-pixi, build):

   ```yaml
   diffcone:
     runs-on: ubuntu-24.04
     cache-mode: read
     # env, services, checkout, setup-pixi and build as in the record job
     steps:
       # ...
       - uses: bearing-research/diffcone/actions/run@v0.1.0
         with:
           key-prefix: pandas-ubuntu-24.04-py313
           command: pixi run --environment py313 python -m pytest
           pytest-args: -r fE --numprocesses=auto --dist=worksteal -m "not single_cpu" pandas
       - uses: actions/upload-artifact@v7
         if: always()
         with:
           name: diffcone-results
           path: diffcone-results
   ```

3. The verdict:

   ```yaml
   diffcone-check:
     needs: [ubuntu, diffcone]
     if: always()
     runs-on: ubuntu-24.04
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

The `pytest-args` must be identical in the record and run steps: the
selection is checked against what the recording covered.

### To check on the first run

* that `pixi run` passes `PYTHONPATH` through to Python (the recorder is
  loaded as `-p diffcone_collect` from a directory diffcone puts there);
* that the checkout is clean after the build, which `collect` requires
  (build directories and `.pixi/` must be ignored by git);
* how long the recording takes on a 4-core runner (the whole `not
  single_cpu` suite under the recorder) against `timeout-minutes`, and how
  long the plan takes from the restored cache;
* that nightly and pull-request jobs really record the same environment
  (`diffcone evidence` lists the store's; the run says what differed).

### Beside it: pytest-testmon

`check --run testmon=<junit>` compares any other selector's run on the same
failures. For testmon that is a nightly `pytest --testmon` whose
`.testmondata` is cached like `.diffcone/`, and a pull-request run with
`--testmon --junitxml=testmon.xml`, in jobs of their own: installing
pytest-testmon changes the environment the diffcone recording describes.
Still to find out: whether testmon works with pytest-xdist and with the
`--cov` run pandas already does (both use coverage.py's tracer), and
whether its data stays valid across the day's commits.

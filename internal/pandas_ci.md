# pandas, on a fork

The pandas trial of the GitHub Actions in `actions/` (user guide:
`docs/ci.md`; plan: `internal/roadmap.md`, item 9).

Covers the `ubuntu-24.04` / `py313` job's `not single_cpu` step. pandas
installs from `pixi.lock`, so nightly and pull-request environments match
unless the lock changes, and its editable build is not part of the
fingerprint. `PANDAS_FUTURE`, `PYTHONDEVMODE` and `PYTHONWARNDEFAULTENCODING`
change what tests do, so they are recorded and checked. Cython edits select
everything: the build is the ordinary one, without `profile=True` (Cython
evidence also needs Python 3.13+, where a profiled build reports to
`sys.monitoring`).

## `.github/workflows/diffcone-record.yml` (new)

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

## Changes to `.github/workflows/unit-tests.yml`

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

## To check on the first run

* that `pixi run` passes `PYTHONPATH` through to Python (the recorder is
  loaded as `-p diffcone_collect` from a directory diffcone puts there);
* that the checkout is clean after the build, which `collect` requires
  (build directories and `.pixi/` must be ignored by git);
* how long the recording takes on a 4-core runner (the whole `not
  single_cpu` suite under the recorder) against `timeout-minutes`, and how
  long the plan takes from the restored cache;
* that nightly and pull-request jobs really record the same environment
  (`diffcone evidence` lists the store's; the run says what differed).

## Beside it: pytest-testmon

`check --run testmon=<junit>` compares any other selector's run on the same
failures. For testmon that is a nightly `pytest --testmon` whose
`.testmondata` is cached like `.diffcone/`, and a pull-request run with
`--testmon --junitxml=testmon.xml`, in jobs of their own: installing
pytest-testmon changes the environment the diffcone recording describes.
Still to find out: whether testmon works with pytest-xdist and with the
`--cov` run pandas already does (both use coverage.py's tracer), and
whether its data stays valid across the day's commits.

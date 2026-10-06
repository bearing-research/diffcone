# Running and checking

Planning never executes project code. These commands run tests *after* a
plan exists, with a command line you give them.

## Run the selected tests

```bash
# arguments after -- go to pytest
diffcone run --base main --head WORKTREE --discover pytest \
    --command "uv run pytest" -- -x

# ASV: print the asv command (a --bench pattern) without running it
diffcone run --base main --head HEAD --discover asv --runner asv --dry-run
```

`run` builds the plan exactly like `plan` and runs the selected targets.
pytest collects from its own starting points as usual and a small plugin
keeps only the selected tests, so conftests load as in a full run.

`run` refuses to run (exit `2`) when the working tree differs, under the
source roots, from the snapshot the plan analysed: the plan describes the
code it read, not whatever is checked out. Plan with `--head WORKTREE`,
check the snapshot out, or pass `--allow-mismatched-worktree`. It also
refuses (exit `3`) a plan whose discovery may be incomplete, unless given
`--allow-incomplete-discovery`.

## Check a plan against a full run

The question a selector has to answer is "did it select every test that
fails?". `check` answers it from the JUnit XML of a full run, without
running anything, so it fits beside an existing CI job:

```bash
pytest --junitxml=full.xml                       # the full run, as CI already does
diffcone plan --base main --head HEAD --discover pytest -o plan.json
diffcone check --plan plan.json --full full.xml
```

It reports every test that failed or errored in the full run but was not
selected. A test the plan did not know at all, or a file that failed to
collect, counts as a miss too. The exit code is `1` on a miss.

- `--baseline nightly.xml`: a run without the change (the nightly run at
  the base, say). A failure it also had is reported as already failing,
  not missed: the change did not cause it.
- `--run NAME=JUNIT`: a selective run compared on the same failures:
  diffcone's own run of the plan (whose outcomes should agree with the full
  run's), or another selector's, such as pytest-testmon's.
- `--format markdown` writes a table for a CI job summary; `json` for
  tools.

## Validate a plan by running everything

`validate` runs the whole suite at both snapshots (commits in temporary
`git worktree`s, `WORKTREE` in place) and reports every test whose
pass/fail outcome changed but was not selected:

```bash
diffcone validate --base main --head HEAD --discover pytest \
    --source-root src --source-root tests --command "uv run pytest" --coverage
```

With `--coverage` it also runs the head suite under pytest-cov with
per-test contexts (pytest-cov must be installed where the suite runs) and
requires every test that *executed* a changed symbol to have been selected,
reporting recall and precision against that ground truth. It exits `1` on
any miss.

`corpus --range A..B` does the same for every commit in a range
(first-parent order; commits without `.py` changes are skipped unless
`--all-commits`) and aggregates misses, recall, precision and the share of
tests saved. Each commit's suite runs once; `--jobs N` validates pairs in
parallel.

Two things to get right for the runs:

- Pass the source roots that hold the package (`--source-root src
  --source-root tests` for a `src` layout): they go first on `PYTHONPATH`,
  so the checkout's code runs, not an installed copy.
- A relative interpreter in `--command` (`.venv/bin/python -m pytest`) is
  resolved against the current directory, then the repository, since the
  suites run in temporary worktrees. `--setup-command` runs in each
  temporary checkout first (to regenerate build files such as a
  setuptools-scm `_version.py`).

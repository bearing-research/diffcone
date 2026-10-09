# Running and checking

Planning never runs your code. These commands run tests after a plan
exists, using the command line you give them.

The examples use the default source root, the repository itself. For a
`src` layout, add `--source-root src --source-root .` to each command, as
in [planning](planning.md#source-roots).

## Run the selected tests

```bash
diffcone run --base main --head WORKTREE --discover pytest \
    --command "uv run pytest" -- -x
```

`run` builds the same plan as `diffcone plan`, then runs only the selected
tests. Everything after `--` is passed to pytest. pytest collects your
suite as usual and a small plugin keeps the selected tests, so conftests
and plugins load exactly as in a full run.

Discovery reads the same pytest options the run will use: those after
`--`, those written into `--command` (`uv run pytest -p tests.plugin`),
and the `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` environment variables. When
you plan separately with `diffcone plan`, pass it the same `--command`
and arguments, in the same environment. If discovery can't find pytest's
arguments in `--command` (it looks for `-m pytest` or a `pytest`
program, so a wrapper script is not recognised), the plan says so and
exits `3`.

For ASV, `--runner asv` builds the matching `asv run --bench` pattern. Add
`--dry-run` to print the command instead of running it:

```bash
diffcone run --base main --head HEAD --discover asv --runner asv --dry-run
```

`run` refuses to start in two situations, each with an option to override:

- **The checkout differs from what was planned.** If you plan a commit but
  have uncommitted changes to Python files, the plan doesn't describe the
  code that would run. Plan with `--head WORKTREE`, or pass
  `--allow-mismatched-worktree`.
- **Discovery may be incomplete** (exit code `3`). Pass
  `--allow-incomplete-discovery` to run the selection anyway.

## Check a plan against a full run

To trust a selection, you want to know: did it select every test that
failed? `diffcone check` answers that from the JUnit XML of a full test
run. It doesn't run anything, so you can add it next to an existing CI job:

```bash
pytest --junitxml=full.xml
diffcone plan --base main --head HEAD --discover pytest -o plan.json
diffcone check --plan plan.json --full full.xml
```

It lists every test that failed or errored in the full run but wasn't
selected, and exits with `1` if there is one. Options:

- `--baseline base.xml` takes a run from before the change. Tests that
  failed there too are reported as already failing, not as misses.
- `--run NAME=JUNIT` adds a selective run to compare on the same failures:
  diffcone's own run of the plan, or another test-selection tool's. Name
  diffcone's own run `diffcone` (`--run diffcone=diffcone.xml`): a new
  failure it didn't execute is a miss too, even if the plan selected it.
- `--format markdown` writes a summary table for a CI job summary, and
  `--format json` a report for scripts.

## Validate a plan by running everything

`diffcone validate` runs your whole suite on both snapshots and reports any
test whose result changed but wasn't selected:

```bash
diffcone validate --base main --head HEAD --discover pytest \
    --source-root src --source-root . --command "uv run pytest"
```

Commits are checked out into temporary worktrees; `WORKTREE` runs in
place. With `--coverage` (which needs pytest-cov installed where your tests
run), it also records which tests executed changed code, and reports any
such test that wasn't selected.

`diffcone corpus --range A..B` validates every commit in a range and
summarises the results: how many failures were missed and what share of
tests each plan would have skipped. Use `--jobs N` to validate several
commits in parallel.

Tips for both commands:

- Pass the source roots that contain your package, so the tests run
  against the checked-out code rather than an installed copy.
- If `--command` uses a relative path (`.venv/bin/python -m pytest`), it is
  resolved against the current directory, then the repository.
- `--setup-command` runs a shell command in each temporary checkout before
  its tests, for example to regenerate a `_version.py`.

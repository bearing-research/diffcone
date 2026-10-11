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
and plugins load exactly as in a full run. Tests pytest collects that the
plan doesn't know about (from a plugin's own collection, for example) are
kept too.

`run` exits with pytest's exit code, with two exceptions (see
[exit codes](../reference/cli.md#exit-codes)):

- if a selected test wasn't collected and pytest otherwise succeeded, it
  exits `3`, since the run may have skipped tests;
- if nothing ran because nothing was selected, it exits `0`.

Discovery reads the same pytest options the run will use: those after
`--`, those written into `--command` (`uv run pytest -p tests.plugin`),
and the `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` environment variables. When
you plan separately with `diffcone plan`, pass it the same `--command`
and arguments, in the same environment. If discovery can't find pytest's
arguments in `--command` (it looks for `-m pytest` or a `pytest`
program, so a wrapper script is not recognised), the plan says so and
exits `3`.

If pytest didn't collect a selected test, the message also names any
option among the effective pytest arguments that keeps tests out of a run
by the cache or by name (`--lf`, `--ff`, `--nf`, `--sw`, `-k`, `-m`,
`--deselect`) and where it is set, since that is the usual reason.

For ASV, `--runner asv` builds the matching `asv run --bench` pattern. Add
`--dry-run` to print the command instead of running it:

```bash
diffcone run --base main --head HEAD --discover asv --runner asv --dry-run
```

For pytest, the printed command names the selected tests as node ids. It
is close to, but not the same as, what `run` does:

- it leaves out tests pytest collects that the plan doesn't know, which
  `run` keeps;
- naming tests on the command line makes pytest load their `conftest.py`
  files earlier than a normal run does;
- with `--evidence`, a dry run doesn't check the environment against the
  recording.

`run` refuses to start in two situations, each with an option to override:

- **The checkout differs from what was planned.** If you planned a commit
  or `INDEX` and the working tree differs in a file the plan depends on,
  `run` refuses and names the files. That includes any file under your
  source roots, a git-ignored `.py` beside tracked code, any
  `conftest.py`, pytest or ASV configuration, `diffcone.toml`, dependency
  and lock files, and submodules. Plan with `--head WORKTREE`, or pass
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

- `--full` and `--baseline` can be repeated: several files (from a
  sharded or split run, say) count as one run.
- `--baseline base.xml` takes a run from before the change. Tests that
  failed there too are reported as already failing, not as misses. Each
  parametrized case counts on its own: a case that failed at the baseline
  doesn't excuse a new failure of another case.
- `--run NAME=JUNIT` adds a selective run to compare on the same failures:
  diffcone's own run of the plan, or another test-selection tool's. Name
  diffcone's own run `diffcone` (`--run diffcone=diffcone.xml`): a new
  failure it didn't execute is a miss too, even if the plan selected it.
- `--format markdown` writes a summary table for a CI job summary, and
  `--format json` a report for scripts.

JUnit names tests relative to pytest's rootdir, sometimes with a
`--junit-prefix`. The plan records both, so plan with the same `--command`
and arguments as the run. If they differ, pass `--rootdir DIR` (relative
to the repository) or `--junit-prefix PREFIX`. If no case matches a target
at all, the report says so and points at these options, rather than
reporting every failure as a miss.

A directory that fails to collect (its `conftest.py` doesn't import)
counts as a collection failure of every test under it. It is caught when
any of them was selected.

### The JSON report

`diffcone check --format json` writes:

| Field | Contents |
|---|---|
| `ok` | `true` when there is no miss and nothing in `not_run`. |
| `misses` | The new failures the plan didn't select. |
| `not_run` | New failures diffcone's own run (`--run diffcone=...`) didn't execute. |
| `plan` | `status`, `discovery_incomplete`, and how many `targets` there were and how many were `selected`. |
| `full_run` | `cases`, `test_time`, `selected_test_time` (the full run's time for the selected tests) and `unmatched_cases` (cases that match no target). |
| `mapping` | The `rootdir` and `junit_prefix` the names were mapped with, and a `note` when no case matched a target. |
| `failures` | Every failure of the full run: `test`, `outcome`, `kind`, `selected` and `already_failing` (it failed in the baseline too). `kind` is `test`, `collection` (a file that failed to collect) or `unknown` (it matches no target). |
| `runs` | Each `--run`: `name`, `cases`, `test_time`, `targets_run`, `misses` (new failures it didn't run) and `disagreements` (tests it ran that failed in one run but not the other). |

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

A miss comes in two kinds:

- `MISSED`: a target whose result changed (or that executed changed code)
  and that the plan did not select.
- A test that is not a target at all, because discovery did not find it.
  If the plan says its discovery is incomplete (it exits `3`, and
  `diffcone run` refuses it without `--allow-incomplete-discovery`), such
  tests are a gap the plan already reported, not a selection miss:
  `validate` lists them as "outside the target list (discovery
  incomplete: the plan exits 3)" and they do not fail the check. Coverage
  recall is then measured over the targets, with the other tests counted
  apart (`recall 100% of targets; 1589 test(s) outside the target list`).
  If the plan claims its discovery is complete, such a test is a discovery
  miss and is reported as `MISSED (not a target)`.

`validate` exits `0` when nothing was missed and `1` when something was. In
`--format json`, `missed` lists every miss (each with `"target": true` or
`false`), `outside_targets` lists the changed tests that are not targets,
and `discovery_incomplete` says whether those count as misses; `counts` and
`coverage.counts` carry `missed_targets` and `outside_targets` as well.

The whole suite must run on both sides for the comparison to mean
anything, so `validate`, `corpus` and `collect` add `--maxfail=0` after
your arguments, which overrides an `-x` or `--maxfail` in `addopts` or in
the command. A suite that still stops early (a `conftest.py` that sets
`maxfail`, a plugin that ends the session) reports outcomes for fewer
tests than it collected; `validate` then refuses with exit code `2`
instead of comparing.

`diffcone corpus --range A..B` validates every commit in a range and
summarises the results: how many failures were missed and what share of
tests each plan would have skipped, with tests outside an incomplete target
list counted apart, as `validate` does; it exits `1` when a plan missed something or a commit
could not be validated. It skips commits that change no `.py`
file unless you pass `--all-commits`. Use `--jobs N` to validate several
commits in parallel. Both commands take `--evidence` to validate plans made
from a recording.

Tips for both commands:

- Pass the source roots that contain your package, so the tests run
  against the checked-out code rather than an installed copy.
- If `--command` uses a relative path (`.venv/bin/python -m pytest`), it is
  resolved against the current directory, then the repository.
- A temporary checkout gets your repository's copy of each git-ignored
  Python module inside a package that the commit lacks, such as the
  `_version.py` that setuptools-scm or hatch-vcs generate, so the package
  can be imported.
- `--setup-command` runs a shell command in each temporary checkout before
  its tests, for whatever else a build generates (compiled extensions).
- pytest's cache plugin runs with an empty cache of its own: a configured
  `--lf` or `--ff` doesn't narrow or reorder the suite, and your
  `.pytest_cache` is left alone. Don't disable the plugin in `--command`
  (`-p no:cacheprovider`) when your configuration or plugins need it.

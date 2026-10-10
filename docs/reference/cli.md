# Commands

Every command and its options. `diffcone <command> --help` prints the same
information. `plan`, `run` and `discover` take a snapshot (`--base`,
`--head`, `--rev`) as a git revision, `INDEX` (your staged changes) or
`WORKTREE` (the files on disk); `validate` and `corpus` run your suite, so
they take a revision or `WORKTREE`; `collect --rev` takes a revision. A
revision the repository does not have is an error that names it (and says
so when the clone is shallow).

## Exit codes

`plan` and `discover`:

- `0`: plan produced, analysis complete.
- `1`: plan produced, but analysis errors (a file that does not parse, a
  source root with no Python file) forced selecting everything.
- `2`: no plan (bad arguments, an unreadable manifest, an unknown revision,
  or an internal error).
- `3`: plan produced, but discovery may be short of what the runner
  collects.

`1` and `3` are opposite failures: `1` selects too much, `3` means the
target list itself may be short, so running only the selected targets
could skip tests. `3` wins when both apply. A manifest written by
`discover` keeps its notes, so a plan made from it exits `3` as well.

`run` exits with the runner's own code (0 when nothing was selected, or
with `--dry-run`; pytest's `5`, no test ran, counts as `0`), and with `3`
when a selected test was not collected. It refuses with `2` when the
working tree differs from the snapshot the plan analysed (unless
`--allow-mismatched-worktree`) or `--repo` is not the top of a git
repository, and with `3` when discovery may be incomplete (unless
`--allow-incomplete-discovery`).

`validate` and `corpus` exit `0` when every outcome change was selected,
`1` when some were missed, `2` on errors. `check` exits `0` when every new
failure of the full run was selected, `1` when the plan missed one, `2`
when an input cannot be read. `report` exits `0` when the runs it read
show no problem, `1` when they show one (a miss, a check that reached no
verdict, a selected test pytest did not collect, an artifact it could not
read), `2` on errors. `collect`, `prune` and
`evidence` exit `0` on success (`collect` records whatever the suite's own
results) and `2` on errors. An internal error exits `2` in every command.

## `diffcone plan`

Produce a selection plan between two snapshots (revisions, INDEX or WORKTREE).

Compare two snapshots and report which targets are affected. A snapshot is a git revision, INDEX (staged content) or WORKTREE (files on disk). The report states exactly which kind was read. Targets come from --targets, from --discover, or both. Project code is never executed.

```text
diffcone plan [-h] --base BASE --head HEAD [--targets TARGETS] [--command COMMAND] [--repo REPO] [--source-root DIR[=PREFIX]] [--discover RUNNER] [--assume-external-fixture NAME] [--no-well-known-fixtures] [--output OUTPUT] [--no-cache] [--cache-dir CACHE_DIR] [--evidence auto|PATH] [--format {json,text}] [runner_args ...]
```

| Option | Default | Description |
|---|---|---|
| `--base` `BASE` |  | **Required.** Base snapshot: a git revision, INDEX or WORKTREE. |
| `--head` `HEAD` |  | **Required.** Head snapshot: a git revision, INDEX (staged content) or WORKTREE (files on disk, ignored files excluded). |
| `--targets` `TARGETS` |  | Path to a JSON target manifest. |
| `--command` `COMMAND` |  | The pytest command line the run will use: pytest options written in it (uv run pytest -p plugins.x) are read by discovery, as run reads them. |
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |
| `--source-root` `DIR[=PREFIX]` |  | Repo-relative directory whose .py files are analyzed as a module tree (repeatable; overrides the manifest's source_roots; default: .). DIR=PREFIX names its modules PREFIX.&lt;path&gt;, for per-package test trees whose files share names. |
| `--discover` `RUNNER` |  | Statically discover targets for a runner (pytest, asv); repeatable. One of `pytest`, `asv`. |
| `--assume-external-fixture` `NAME` |  | Pytest fixture provided by an installed plugin; not reported as unresolved (fixtures of well-known plugins such as pytest-mock's mocker are assumed by default). Repeatable. |
| `--no-well-known-fixtures` |  | Do not assume fixtures of well-known pytest plugins; report them as unresolved. |
| `--output`, `-o` `OUTPUT` |  | Write the result to this file instead of stdout (run: the command with --dry-run, otherwise the plan that ran, as plan writes it in JSON). |
| `--no-cache` |  | Do not read or write the cache (&lt;repo&gt;/.diffcone/cache): whole indexes of committed snapshots and per-module results, which also serve WORKTREE and INDEX. |
| `--cache-dir` `CACHE_DIR` |  | Where to keep the cache (default: &lt;repo&gt;/.diffcone/cache). |
| `--evidence` <code>auto&#124;PATH</code> |  | Opt-in execution evidence (see `diffcone collect`): select pytest targets on what each test executed when recorded. auto picks the recording at the nearest ancestor commit; other runners' targets are planned statically. |
| `--format` `FORMAT` | `json` | One of `json`, `text`. |
| `runner_args ...` (after `--`) |  | Pytest arguments the run will use (after --): options that change what pytest collects, such as --doctest-modules, are read by discovery. |

## `diffcone run`

Plan, then execute only the selected targets with the runner's CLI.

Build a plan exactly like `plan`, then invoke the runner on the selected targets (pytest node ids, or an asv --bench pattern). Nothing is executed during analysis. Arguments after `--` are passed to the runner.

```text
diffcone run [-h] --base BASE --head HEAD [--targets TARGETS] [--runner {pytest,asv}] [--command COMMAND] [--dry-run] [--allow-mismatched-worktree] [--allow-incomplete-discovery] [--repo REPO] [--source-root DIR[=PREFIX]] [--discover RUNNER] [--assume-external-fixture NAME] [--no-well-known-fixtures] [--output OUTPUT] [--no-cache] [--cache-dir CACHE_DIR] [--evidence auto|PATH] [--collect] [runner_args ...]
```

| Option | Default | Description |
|---|---|---|
| `--base` `BASE` |  | **Required.** Base snapshot: a git revision, INDEX or WORKTREE. |
| `--head` `HEAD` |  | **Required.** Head snapshot: a git revision, INDEX or WORKTREE. |
| `--targets` `TARGETS` |  | Path to a JSON target manifest. |
| `--runner` `RUNNER` | `pytest` | Which runner to execute. One of `pytest`, `asv`. |
| `--command` `COMMAND` |  | Runner command line (default: "python -m pytest", or "asv run --python=same", which benchmarks the checkout in the current environment); run in --repo. |
| `--dry-run` |  | Print the command instead of running. |
| `--allow-mismatched-worktree` |  | Run even when the checkout is not the snapshot the plan analysed. |
| `--allow-incomplete-discovery` |  | Run even when discovery reports tests the runner may collect that are not targets. |
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |
| `--source-root` `DIR[=PREFIX]` |  | Repo-relative directory whose .py files are analyzed as a module tree (repeatable; overrides the manifest's source_roots; default: .). DIR=PREFIX names its modules PREFIX.&lt;path&gt;, for per-package test trees whose files share names. |
| `--discover` `RUNNER` |  | Statically discover targets for a runner (pytest, asv); repeatable. One of `pytest`, `asv`. |
| `--assume-external-fixture` `NAME` |  | Pytest fixture provided by an installed plugin; not reported as unresolved (fixtures of well-known plugins such as pytest-mock's mocker are assumed by default). Repeatable. |
| `--no-well-known-fixtures` |  | Do not assume fixtures of well-known pytest plugins; report them as unresolved. |
| `--output`, `-o` `OUTPUT` |  | Write the result to this file instead of stdout (run: the command with --dry-run, otherwise the plan that ran, as plan writes it in JSON). |
| `--no-cache` |  | Do not read or write the cache (&lt;repo&gt;/.diffcone/cache): whole indexes of committed snapshots and per-module results, which also serve WORKTREE and INDEX. |
| `--cache-dir` `CACHE_DIR` |  | Where to keep the cache (default: &lt;repo&gt;/.diffcone/cache). |
| `--evidence` <code>auto&#124;PATH</code> |  | Opt-in execution evidence (see `diffcone collect`): select pytest targets on what each test executed when recorded. auto picks the recording at the nearest ancestor commit; other runners' targets are planned statically. |
| `--collect` |  | With --evidence: record the selected tests too and advance the recording to head (their new records, the old ones for every other test); needs a clean checkout of head and the command, pytest arguments and source roots the recording was made with. |
| `runner_args ...` (after `--`) |  | Extra runner arguments (after --). |

## `diffcone validate`

Run the full pytest suite at both snapshots and check the plan against it.

Outcome-based validation: runs the whole pytest suite at base and head (commits in temporary git worktrees, WORKTREE in place), then reports every test whose pass/fail outcome changed but was not selected. Behaviour changes that keep the same outcome are invisible to this check.

```text
diffcone validate [-h] --base BASE --head HEAD [--targets TARGETS] [--command COMMAND] [--coverage] [--setup-command SETUP_COMMAND] [--repo REPO] [--source-root DIR[=PREFIX]] [--discover RUNNER] [--assume-external-fixture NAME] [--no-well-known-fixtures] [--output OUTPUT] [--no-cache] [--cache-dir CACHE_DIR] [--evidence auto|PATH] [--format {json,text}]
```

| Option | Default | Description |
|---|---|---|
| `--base` `BASE` |  | **Required.** Base snapshot: a git revision or WORKTREE. |
| `--head` `HEAD` |  | **Required.** Head snapshot: a git revision or WORKTREE. |
| `--targets` `TARGETS` |  | Path to a JSON target manifest. |
| `--command` `COMMAND` |  | Pytest command line (default: "python -m pytest"). |
| `--coverage` |  | Also run the head suite under pytest-cov with per-test contexts and require every test that executed a changed symbol to be selected (reports recall/precision). |
| `--setup-command` `SETUP_COMMAND` |  | Shell command run inside each temporary checkout before its suite (recreate build-generated files such as a setuptools-scm _version.py). |
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |
| `--source-root` `DIR[=PREFIX]` |  | Repo-relative directory whose .py files are analyzed as a module tree (repeatable; overrides the manifest's source_roots; default: .). DIR=PREFIX names its modules PREFIX.&lt;path&gt;, for per-package test trees whose files share names. |
| `--discover` `RUNNER` |  | Statically discover targets for a runner (pytest, asv); repeatable. One of `pytest`, `asv`. |
| `--assume-external-fixture` `NAME` |  | Pytest fixture provided by an installed plugin; not reported as unresolved (fixtures of well-known plugins such as pytest-mock's mocker are assumed by default). Repeatable. |
| `--no-well-known-fixtures` |  | Do not assume fixtures of well-known pytest plugins; report them as unresolved. |
| `--output`, `-o` `OUTPUT` |  | Write the result to this file instead of stdout (run: the command with --dry-run, otherwise the plan that ran, as plan writes it in JSON). |
| `--no-cache` |  | Do not read or write the cache (&lt;repo&gt;/.diffcone/cache): whole indexes of committed snapshots and per-module results, which also serve WORKTREE and INDEX. |
| `--cache-dir` `CACHE_DIR` |  | Where to keep the cache (default: &lt;repo&gt;/.diffcone/cache). |
| `--evidence` <code>auto&#124;PATH</code> |  | Opt-in execution evidence (see `diffcone collect`): select pytest targets on what each test executed when recorded. auto picks the recording at the nearest ancestor commit; other runners' targets are planned statically. |
| `--format` `FORMAT` | `text` | One of `json`, `text`. |

## `diffcone corpus`

Validate the plan for every commit in a range and aggregate recall/precision.

Replay history: for each commit in A..B (first-parent order) plan parent -> commit and validate it like `validate`, then aggregate outcome misses, coverage recall/precision and selection savings. Each commit's suite runs once; coverage runs are per pair. Commits touching no .py file are skipped unless --all-commits is given.

```text
diffcone corpus [-h] --range REVISION_RANGE [--targets TARGETS] [--command COMMAND] [--coverage] [--setup-command SETUP_COMMAND] [--all-commits] [--max MAX_COMMITS] [--evidence auto|PATH] [--jobs JOBS] [--repo REPO] [--source-root DIR[=PREFIX]] [--discover RUNNER] [--assume-external-fixture NAME] [--no-well-known-fixtures] [--output OUTPUT] [--no-cache] [--cache-dir CACHE_DIR] [--format {json,text}]
```

| Option | Default | Description |
|---|---|---|
| `--range` `RANGE` |  | **Required.** Git range, e.g. main~20..main. |
| `--targets` `TARGETS` |  | Path to a JSON target manifest. |
| `--command` `COMMAND` |  | Pytest command line (default: "python -m pytest"). |
| `--coverage` |  | Also measure coverage recall/precision. |
| `--setup-command` `SETUP_COMMAND` |  | Shell command run inside each temporary checkout before its suite. |
| `--all-commits` |  | Validate commits without .py changes too. |
| `--max` `MAX` |  | Only the last N commits of the range. |
| `--evidence` <code>auto&#124;PATH</code> |  | Plan every pair with execution evidence: a fixed recording, or auto for the recording at the nearest ancestor of each commit. |
| `--jobs` `JOBS` | `1` | Validate this many pairs in parallel, each in its own temporary worktrees (default 1; suites that write to shared locations can interfere). |
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |
| `--source-root` `DIR[=PREFIX]` |  | Repo-relative directory whose .py files are analyzed as a module tree (repeatable; overrides the manifest's source_roots; default: .). DIR=PREFIX names its modules PREFIX.&lt;path&gt;, for per-package test trees whose files share names. |
| `--discover` `RUNNER` |  | Statically discover targets for a runner (pytest, asv); repeatable. One of `pytest`, `asv`. |
| `--assume-external-fixture` `NAME` |  | Pytest fixture provided by an installed plugin; not reported as unresolved (fixtures of well-known plugins such as pytest-mock's mocker are assumed by default). Repeatable. |
| `--no-well-known-fixtures` |  | Do not assume fixtures of well-known pytest plugins; report them as unresolved. |
| `--output`, `-o` `OUTPUT` |  | Write the result to this file instead of stdout (run: the command with --dry-run, otherwise the plan that ran, as plan writes it in JSON). |
| `--no-cache` |  | Do not read or write the cache (&lt;repo&gt;/.diffcone/cache): whole indexes of committed snapshots and per-module results, which also serve WORKTREE and INDEX. |
| `--cache-dir` `CACHE_DIR` |  | Where to keep the cache (default: &lt;repo&gt;/.diffcone/cache). |
| `--format` `FORMAT` | `text` | One of `json`, `text`. |

## `diffcone collect`

Run the whole pytest suite under the evidence recorder and store what each test executed.

Execution evidence (opt-in): run the whole suite once with diffcone's recorder (Python 3.12+) and write .diffcone/evidence/<commit>-<environment>.sqlite: the symbols each test executed and the repository files it touched, at a commit. Without --rev the repository itself runs and must be clean. Arguments after `--` are passed to pytest.

```text
diffcone collect [-h] [--repo REPO] [--source-root DIR[=PREFIX]] [--rev REV] [--command COMMAND] [--setup-command SETUP_COMMAND] [--reverse-check] [--env-var NAME] [--no-cache] [--cache-dir CACHE_DIR] [runner_args ...]
```

| Option | Default | Description |
|---|---|---|
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |
| `--source-root` `DIR[=PREFIX]` |  | As for plan (repeatable; default: .). |
| `--rev` `REV` |  | Collect at this commit, in a temporary worktree (default: the clean HEAD). |
| `--command` `COMMAND` |  | Pytest command line (default: "python -m pytest"). |
| `--setup-command` `SETUP_COMMAND` |  | Shell command run inside the temporary checkout (with --rev). |
| `--reverse-check` |  | Run the suite a second time in reverse order; tests whose records differ are marked unstable and always selected. |
| `--env-var` `NAME` |  | An environment variable that changes what tests do (a feature flag, say): recorded with the environment, and a run where it differs uses no evidence (repeatable). |
| `--no-cache` |  | Do not use the index cache. |
| `--cache-dir` `CACHE_DIR` |  | Where to keep the cache (default: &lt;repo&gt;/.diffcone/cache). |
| `runner_args ...` (after `--`) |  | Extra pytest arguments (after --). |

## `diffcone check`

Check a plan against a full run's JUnit XML: was every failing test selected?

Read a plan (diffcone plan --format json) and the JUnit XML of a full pytest run, and report every test that failed or errored there but was not selected. A failure the --baseline run also had is reported as already failing. Each --run is a selective run (diffcone's own, or another selector's such as pytest-testmon) compared on the same failures. Runs nothing.

```text
diffcone check [-h] --plan PLAN --full JUNIT [--baseline JUNIT] [--run NAME=JUNIT] [--format {text,markdown,json}] [--output OUTPUT]
```

| Option | Default | Description |
|---|---|---|
| `--plan` `PLAN` |  | **Required.** The plan, as JSON. |
| `--full` `JUNIT` |  | **Required.** JUnit XML of the full run (repeatable: several files are one run). |
| `--baseline` `JUNIT` |  | JUnit XML of a run without the change (e.g. the nightly run at the evidence commit): its failures are not misses. Repeatable. |
| `--run` `NAME=JUNIT` |  | JUnit XML of a selective run to compare (repeatable). |
| `--format` `FORMAT` | `text` | One of `text`, `markdown`, `json`. |
| `--output`, `-o` `OUTPUT` |  | Write the result to this file instead of stdout. |

## `diffcone report`

Report on many CI runs of the diffcone actions: selection, checks, misses.

Read the results the run and record actions uploaded, downloaded into one directory per workflow run (DIR/<run id>/run.json and DIR/<run id>/<artifact>/), and report per job: the share pull requests selected and why some were planned from the code, and for pushes whether each was checked, its new failures, flaky re-runs and confirmed misses. The report action downloads the directory. Runs nothing.

```text
diffcone report [-h] --dir DIR [--format {markdown,json}] [--output OUTPUT]
```

| Option | Default | Description |
|---|---|---|
| `--dir` `DIR` |  | **Required.** The directory of downloaded runs. |
| `--format` `FORMAT` | `markdown` | One of `markdown`, `json`. |
| `--output`, `-o` `OUTPUT` |  | Write the report to this file instead of stdout. |

## `diffcone prune`

Shrink the cache to what planning at given commits reads.

Delete every cached index and discovery result except those of the --keep commits, and every per-module record except those of their files (which also serve later commits and the working tree sharing them). For a cache shipped between CI runs: keep the commit the evidence was recorded at.

```text
diffcone prune [-h] [--repo REPO] --keep REV [--source-root DIR[=PREFIX]] [--cache-dir CACHE_DIR]
```

| Option | Default | Description |
|---|---|---|
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |
| `--keep` `REV` |  | **Required.** Commit to keep (repeatable). |
| `--source-root` `DIR[=PREFIX]` |  | The roots you plan with (repeatable; default: .); entries for other roots are deleted. |
| `--cache-dir` `CACHE_DIR` |  | The cache (default: &lt;repo&gt;/.diffcone/cache). |

## `diffcone evidence`

List the evidence recordings of a repository.

```text
diffcone evidence [-h] [--repo REPO]
```

| Option | Default | Description |
|---|---|---|
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |

## `diffcone discover`

Statically discover targets in a snapshot and emit a manifest.

Discover pytest tests and/or ASV benchmarks in a snapshot (a git revision, INDEX or WORKTREE) without importing them, and print a target manifest (JSON) for `diffcone plan --targets`. The output states which snapshot kind was read.

```text
diffcone discover [-h] [--rev REV] [--command COMMAND] [--repo REPO] [--source-root DIR[=PREFIX]] [--discover RUNNER] [--assume-external-fixture NAME] [--no-well-known-fixtures] [--output OUTPUT] [--no-cache] [--cache-dir CACHE_DIR]
```

| Option | Default | Description |
|---|---|---|
| `--rev` `REV` | `HEAD` | Snapshot to discover in: revision, INDEX or WORKTREE. |
| `--command` `COMMAND` |  | The pytest command line the run will use: pytest options written in it are read by discovery. |
| `--repo` `REPO` | `.` | Path to the git repository (default: .). |
| `--source-root` `DIR[=PREFIX]` |  | Repo-relative directory whose .py files are analyzed as a module tree (repeatable; overrides the manifest's source_roots; default: .). DIR=PREFIX names its modules PREFIX.&lt;path&gt;, for per-package test trees whose files share names. |
| `--discover` `RUNNER` |  | Statically discover targets for a runner (pytest, asv); repeatable. One of `pytest`, `asv`. |
| `--assume-external-fixture` `NAME` |  | Pytest fixture provided by an installed plugin; not reported as unresolved (fixtures of well-known plugins such as pytest-mock's mocker are assumed by default). Repeatable. |
| `--no-well-known-fixtures` |  | Do not assume fixtures of well-known pytest plugins; report them as unresolved. |
| `--output`, `-o` `OUTPUT` |  | Write the result to this file instead of stdout (run: the command with --dry-run, otherwise the plan that ran, as plan writes it in JSON). |
| `--no-cache` |  | Do not read or write the cache (&lt;repo&gt;/.diffcone/cache): whole indexes of committed snapshots and per-module results, which also serve WORKTREE and INDEX. |
| `--cache-dir` `CACHE_DIR` |  | Where to keep the cache (default: &lt;repo&gt;/.diffcone/cache). |

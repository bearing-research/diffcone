# Report

`diffcone plan` prints a JSON report (`--format text` prints a readable
summary of the same information). The report's format is versioned by
`schema_version`, currently `4`.

## Top-level fields

| Field | Contents |
|---|---|
| `schema_version` | The report format version, `4`. |
| `status` | `complete`, or `degraded` when an analysis error made diffcone select every target. |
| `discovery_incomplete` | `true` when the runner may collect tests diffcone couldn't see; the plan then exits with code `3`. |
| `analysis` | What was compared: both snapshots, the source roots, whether uncommitted files were analysed, counts, and the recording used, if any. |
| `changed_symbols` | Every symbol that differs between the snapshots. |
| `selected_targets` | The targets to run, and why. |
| `unselected_targets` | The targets not affected, and why. |
| `dependency_explanations` | For each selected target, the path from the target to the change. |
| `unresolved_relationships` | References diffcone couldn't resolve, and which changes they might reach. |
| `fallback_decisions` | Where diffcone selected more because it couldn't tell what a change reaches. |
| `analysis_errors` | Errors such as a file that doesn't parse. Any error selects every target. |
| `declarations` | The dependencies declared in [`diffcone.toml`](declarations.md). |
| `always_run` | The `[[always_run]]` entries of [`diffcone.toml`](declarations.md); each says how many targets it `matched`, and `analysis.counts.always_run` counts the targets they selected. |
| `discovery` | For each runner: how many targets were found, the configuration read, and any notes. For pytest, the configuration includes `rootdir`, `configfile` and `junit_prefix` (which [`diffcone check`](../guides/running.md#check-a-plan-against-a-full-run) uses to match JUnit names), and `run_arguments`, what was read besides the configuration file: `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, the pytest arguments in `--command` and those after `--`. |

## Recording

With a recording, `analysis.evidence` describes it:

| Field | Contents |
|---|---|
| `store` | The recording's path. |
| `commit` | The commit it was recorded at. |
| `environment_hash` | A hash of the recorded environment. |
| `environment_checked` | Whether this environment was checked against the recording (see below). |
| `python` | The recorded Python version. |
| `hash_seed` | The recorded `PYTHONHASHSEED`. |
| `variables` | The names of the environment variables it recorded. |
| `created` | When it was recorded, in seconds since the epoch. |
| `tests` | How many tests it holds. |
| `reverse_checked` | Whether it was recorded with `--reverse-check`. |
| `planned` | The plans made from it: from the recorded commit to each snapshot. |
| `changes_outside_range` | Changes made since the recording that lie outside base..head; they were planned too. |
| `escalated_modules` | Modules whose changes couldn't be judged from the recording and were planned from the code. |
| `preloaded_modules` | Modules of your project imported before the recorder started; changes to them are planned from the code. |

`plan` can't check the environment, so it reports `environment_checked:
false`: the selection holds only in the recorded environment. `run` checks
it before any test runs, and its `-o` report says `true` when it matched.

The plan `diffcone run -o` writes is the plan that ran. When the run
didn't use the recording because the environment differed, it also has
`evidence_not_used`: `reason` (`environment`), `differences` (what
differed, as `run` prints it) and `recording_changed_its_environment`
(`true` when this environment is the one the recording's own test run
started in, so the recorded tests changed it).

## Snapshots

`analysis.base` and `analysis.head` describe the two snapshots:

| Field | Contents |
|---|---|
| `revision` | What you asked for: `main`, `HEAD~1`, `WORKTREE`. |
| `commit` | The commit it resolved to (for `INDEX` and `WORKTREE`, the commit they sit on). |
| `kind` | `commit`, `index` or `worktree`. |
| `uncommitted` | `true` for `INDEX` and `WORKTREE`. |
| `description` | A readable summary, as the text report prints it. |
| `excluded` | The `[index] exclude` patterns of the snapshot's `diffcone.toml` (see [diffcone.toml](declarations.md)); empty when there are none. |

## Changed symbols

```json
{
  "id": "calc.ops.mul",
  "kind": "function",
  "changes": ["body_changed"],
  "base_path": "src/calc/ops.py",
  "head_path": "src/calc/ops.py"
}
```

`changes` lists one or more of `added`, `deleted`, `body_changed`,
`definition_changed`, `annotations_changed`, `dependencies_changed`,
`dependencies_added`, `imports_added` and `docstring_changed`
([what they mean](../concepts.md#symbols-and-changes)). `imports_added`
and `docstring_changed` are listed for completeness. They select tests
only where the new names or the docstring are used, or, for a new import,
where a module that now runs (or no longer runs) on import registers or
changes something the tests read.

## Selected targets

```json
{
  "runner": "pytest",
  "runner_id": "tests/test_calc.py::test_square",
  "entry_symbol": "tests.test_calc.test_square",
  "rules": ["dependency"],
  "conservative": false,
  "affected_dependencies": ["tests.test_calc.test_square"]
}
```

`rules` says how the target was selected. The fixtures, setup and
modules a target depends on aren't repeated here: they're in discovery's
output (`diffcone discover -o targets.json`), and the ones that selected
it are in `dependency_explanations`.

`conservative` is `true` when any of the rules is a fallback: something
diffcone couldn't bound, so it selected the target to be safe.

| Rule | Fallback | Meaning |
|---|---|---|
| `dependency` | | A chain of calls, references or imports connects the target to a change (the target's own code included), shown in `dependency_explanations`. |
| `new_target` | | The target is new. |
| `lifecycle_changed` | | What runs around the target changed though its own code didn't: a fixture, hook or plugin now applies to it, or no longer does (another test module adds a plugin to `pytest_plugins`, for example). With a recording, compared with what ran around it when it was recorded. |
| `entry_docstring_changed` | | The target's docstring changed; for a doctest, the docstring is the test. |
| `declared_dependency` | | A dependency declared in `diffcone.toml` connects it to a change. |
| `always_run` | | An `[[always_run]]` entry in `diffcone.toml` names it: it runs on every change. |
| `dynamic_reference` | yes | It reaches code that looks names up dynamically (`getattr`, `importlib`), runs text built at run time as code, or runs a program it can't bound, and a change could be among what that reaches; or it imports or runs a file `[index] exclude` keeps out of the analysis; or it runs with a module's globals (a doctest) and a change lies in what that module imports. |
| `unresolved_name_match` | yes | It reaches a reference diffcone couldn't resolve (a call, a name or an attribute) whose name matches something that changed. |
| `entry_symbol_unresolved` | yes | Its own code wasn't found in either snapshot. |
| `lifecycle_dependency_unresolved` | yes | Something it depends on wasn't found: a fixture diffcone couldn't find, a `conftest.py` or pytest plugin outside the source roots or kept out by `[index] exclude`, a benchmark's code it couldn't read, a doctest whose docstring is computed when the module is imported or whose examples can't be read. |
| `analysis_error` | yes | A file, or `diffcone.toml`, couldn't be analysed, so every target is selected. |
| `runner_dependency` | yes | Code the test runner itself imports changed. |
| `unanalysed_file_changed` | yes | A file diffcone doesn't read changed under the source roots (data, compiled sources, configuration, a `.py` file `[index] exclude` keeps out), or a file's mode changed there (made executable, turned into a symlink). Or, outside the roots, your runner configuration, a `conftest.py`, a build script, dependencies or a lock file changed. For one target: a Python file it runs by path, which no module name maps to, changed. |

With execution evidence, the rules name what the recorded run observed:

| Rule | Fallback | Meaning |
|---|---|---|
| `executed_changed` | | The test ran a changed function. |
| `executed_reader` | | The test ran code that reads a changed value or definition, or reads a cache that code able to run changed code fills (an earlier test may have filled it, so the test never ran that code). The cache may be a module variable, or kept on an object, in a closure, in a function attribute or by a memoising decorator such as `cached_property`. |
| `touched_file` | | The test read a file that changed. |
| `changed_target` | | The test's own code, or its class or module, changed. |
| `test_scope` | | Test code it shares a scope with changed (a mark, fixture or parameter). |
| `collection_code` | | pytest ran changed code, or code that reads something that changed, while it collected the test's file in the recording (building parameter ids, an `ids=` function, `pytest_generate_tests`): that code decides the file's tests, their parameters, ids and marks. |
| `escalated` | | The change can't be judged from a recording (code that runs on import, or a module imported before the recorder started), so it was planned from the code. |
| `cython_caller` | | The test ran Cython code that calls a changed function the recording can't see. |
| `lookup_site` | yes | The test looked a name up dynamically where something the lookup can see changed: a name added or removed, a definition or a value. |
| `no_evidence` | yes | The test has no recording (a new test, for example), or it failed or errored while it was recorded, so its recording stops where it failed. |
| `unstable` | yes | The test's recordings differed between runs. |
| `subprocess` | yes | The test started a process the recording can't follow, or ran while one was still running. As a fallback for every test: such a process was started outside any test (a hook, collection, or an import outside the roots). |
| `text_code` | yes | The test ran code compiled from text that no file of your code holds, such as a doctest's examples or a `timeit` statement: that code can use any name. |
| `pytest_hook_changed` | yes | A pytest hook or what decides what pytest loads changed, or code that a hook ran in the recording (or that ran while pytest collected a file, and could change the whole process or pytest's session) changed or reads something that changed: what a hook does, such as setting an environment variable, reaches every test. |
| `unobserved_file_changed` | yes | A change the recording can't attribute to tests: code that ran outside every test (a hook), a `conftest.py` outside the source roots, Cython outside functions or without a profiled build, other compiled sources, or your build, dependency or pytest configuration files. |
| `unindexed_import` | yes | Changed code ran while a file outside the source roots was being imported, or such a file (or one `[index] exclude` keeps out) changed and was imported before the recorder started or inside a test; every test is selected. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Plan produced; the analysis is complete. |
| `1` | Plan produced, but an analysis error made diffcone select every target. |
| `2` | No plan: a bad argument, an unreadable manifest, or an unknown revision. |
| `3` | Plan produced, but the runner may collect tests diffcone couldn't see. |

When both `1` and `3` apply, the plan exits with `3`.

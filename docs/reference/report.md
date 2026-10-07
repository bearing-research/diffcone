# Report

`diffcone plan` prints a JSON report (`--format text` prints a readable
summary of the same information). The report's format is versioned by
`schema_version`, currently `3`.

## Top-level fields

| Field | Contents |
|---|---|
| `schema_version` | The report format version, `3`. |
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
| `discovery` | For each runner: how many targets were found, the configuration read, and any notes. |

## Snapshots

`analysis.base` and `analysis.head` describe the two snapshots:

| Field | Contents |
|---|---|
| `revision` | What you asked for: `main`, `HEAD~1`, `WORKTREE`. |
| `commit` | The commit it resolved to (for `INDEX` and `WORKTREE`, the commit they sit on). |
| `kind` | `commit`, `index` or `worktree`. |
| `uncommitted` | `true` for `INDEX` and `WORKTREE`. |

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
`definition_changed` and `dependencies_changed`
([what they mean](../concepts.md#symbols-and-changes)).

## Selected targets

```json
{
  "runner": "pytest",
  "runner_id": "tests/test_calc.py::test_square",
  "entry_symbol": "tests.test_calc.test_square",
  "lifecycle_dependencies": ["tests.test_calc"],
  "rules": ["dependency"],
  "conservative": false,
  "affected_dependencies": ["tests.test_calc.test_square"]
}
```

`rules` says how the target was selected. `conservative` is `true` when
any of them is a fallback: something diffcone couldn't bound, so it
selected the target to be safe.

| Rule | Fallback | Meaning |
|---|---|---|
| `dependency` | | A chain of calls, references or imports connects the target to a change (the target's own code included), shown in `dependency_explanations`. |
| `new_target` | | The target is new. |
| `entry_docstring_changed` | | The target's docstring changed; for a doctest, the docstring is the test. |
| `declared_dependency` | | A dependency declared in `diffcone.toml` connects it to a change. |
| `dynamic_reference` | yes | It reaches code that looks names up dynamically (`getattr`, `importlib`), and a change could be among them. |
| `unresolved_name_match` | yes | It reaches a call diffcone couldn't resolve, whose name matches a changed function or method. |
| `entry_symbol_unresolved` | yes | Its own code wasn't found in either snapshot. |
| `lifecycle_dependency_unresolved` | yes | Something it depends on wasn't found: a fixture diffcone couldn't find, a `conftest.py` outside the source roots, a benchmark's code it couldn't read. |
| `analysis_error` | yes | A file couldn't be analysed, so every target is selected. |
| `runner_dependency` | yes | Code the test runner itself imports changed. |
| `unanalysed_file_changed` | yes | A file diffcone doesn't read changed under the source roots (data, compiled sources, configuration), or your runner configuration or build script changed outside them. |

With execution evidence, the rules name what the recorded run observed:

| Rule | Fallback | Meaning |
|---|---|---|
| `executed_changed` | | The test ran a changed function. |
| `executed_reader` | | The test ran code that reads a changed value or definition. |
| `touched_file` | | The test read a file that changed. |
| `changed_target` | | The test's own code, or its class or module, changed. |
| `test_scope` | | Test code it shares a scope with changed (a mark, fixture or parameter). |
| `escalated` | | The change can't be judged from a recording (code that runs on import, for example), so it was planned from the code. |
| `cython_caller` | | The test ran Cython code that calls a changed function the recording can't see. |
| `lookup_site` | yes | The test looked a name up dynamically where a name was added or removed. |
| `no_evidence` | yes | The test has no recording (a new test, for example). |
| `unstable` | yes | The test's recordings differed between runs. |
| `subprocess` | yes | The test started a subprocess, which the recording can't follow. |
| `pytest_hook_changed` | yes | A pytest hook or what decides what pytest loads changed. |
| `unobserved_file_changed` | yes | A change the recording can't attribute to tests: code that ran outside every test (a hook, collection), a `conftest.py` outside the source roots, Cython outside functions or without a profiled build. |
| `unindexed_import` | yes | Changed code ran while a file outside the source roots was being imported. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Plan produced; the analysis is complete. |
| `1` | Plan produced, but an analysis error made diffcone select every target. |
| `2` | No plan: a bad argument, an unreadable manifest, or an unknown revision. |
| `3` | Plan produced, but the runner may collect tests diffcone couldn't see. |

When both `1` and `3` apply, the plan exits with `3`.

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

`rules` says how the target was selected:

- `dependency`: a chain of calls, references or imports connects it to a
  change, shown in `dependency_explanations`;
- `changed_target` or `new_target`: the test itself changed or is new;
- other rules name a fallback: something diffcone couldn't bound, such as
  a fixture it couldn't find or a dynamic import. `conservative` is `true`
  when any rule is one of these.

With execution evidence, the rules name what the recorded run observed,
such as `executed_changed` (the test ran a changed function).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Plan produced; the analysis is complete. |
| `1` | Plan produced, but an analysis error made diffcone select every target. |
| `2` | No plan: a bad argument, an unreadable manifest, or an unknown revision. |
| `3` | Plan produced, but the runner may collect tests diffcone couldn't see. |

When both `1` and `3` apply, the plan exits with `3`.

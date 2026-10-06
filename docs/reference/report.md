# Report

`diffcone plan` prints a JSON report by default (`--format text` for a
readable summary of the same). It is `schema_version` 3.

| Section | Contents |
|---|---|
| `schema_version` | `3`. |
| `status` | `complete`, or `degraded` when analysis errors forced selecting everything. |
| `discovery_incomplete` | Whether discovery may be short of what the runner collects (the plan exits `3`). |
| `declarations` | The edges `diffcone.toml` declared: `from`, `to`, `why`. |
| `analysis` | Both snapshots (`revision`, `commit`, `kind`, `uncommitted`, `description`), source roots, `working_tree_analyzed` / `uncommitted_analyzed`, the supported scope with a one-sentence `analyzed` statement, counts, and `evidence` (the store an evidence plan used, or `null`). |
| `changed_symbols` | Every symbol that differs: `id`, `kind`, `changes` (`added`, `deleted`, `body_changed`, `definition_changed`, `dependencies_changed`, ...), and its path in each revision. |
| `selected_targets` | The targets to run: `runner`, `runner_id`, `entry_symbol`, `lifecycle_dependencies`, the `rules` that selected each, whether any rule is `conservative`, and the `affected_dependencies`. |
| `unselected_targets` | Targets with no path to a change, with the `reason`. |
| `dependency_explanations` | Per selected target, its reasons: the rule, the changed symbol, and the edge path from the target to it (each edge with `source`, `target`, `kind`, and the revisions it exists in). |
| `unresolved_relationships` | References the resolver could not bound, and which changed symbols they may match. |
| `fallback_decisions` | Every place the planner broadened selection instead of guessing: the `rule`, its `scope`, and why. |
| `analysis_errors` | Parse failures and other errors; any error selects all targets and sets `status` to `degraded`. |
| `discovery` | Per runner: the number of targets, the configuration read, and notes on what discovery could not resolve. |

A target's `rules` say how it was selected: `dependency` is a path through
the code (`dependency_explanations` shows it); the others name a fallback
or, with evidence, what the recorded run observed. A plan never fabricates
a path: every reason is a real edge or an explicit rule.

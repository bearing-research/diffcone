# Target manifest

A manifest lists targets explicitly. Use one for a runner that has no
discovery, or to adjust what discovery finds. `diffcone discover -o
targets.json` writes a manifest of the discovered targets, and `--targets
targets.json` reads one.

```json
{
  "source_roots": ["src", "."],
  "targets": [
    {
      "runner": "pytest",
      "runner_id": "tests/test_calc.py::test_add",
      "entry_symbol": "tests.test_calc.test_add",
      "lifecycle_dependencies": ["tests.conftest.db"]
    },
    {
      "runner": "asv",
      "runner_id": "bench_calc.TimeCalc.time_add",
      "entry_symbol": "benchmarks.bench_calc.TimeCalc.time_add",
      "lifecycle_dependencies": ["benchmarks.bench_calc.TimeCalc.setup"]
    }
  ]
}
```

| Field | Description |
|---|---|
| `source_roots` | Optional. The [source roots](../guides/planning.md#source-roots) the names below use. `--source-root` overrides it. |
| `runner` | Which runner runs the target. `diffcone run` supports `pytest` and `asv`; other runners can be planned but not run. |
| `runner_id` | The name the runner uses for the target: a pytest node ID (without parameters) or an ASV benchmark name. |
| `entry_symbol` | The test or benchmark function, as an import path: module, then classes, then function. |
| `lifecycle_dependencies` | Code the runner runs for this target outside its own function: pytest fixtures, ASV `setup` and `setup_cache`, or a module (so module-level code such as `pytestmark` counts). |

A manifest may also be just the list of targets, without the surrounding
object.

When you combine a manifest with `--discover`, a manifest entry replaces a
discovered target with the same `runner` and `runner_id`.

A dependency written `fixture:<name>` marks a fixture whose definition
couldn't be found. diffcone can't tell what such a fixture depends on, so
it selects the target on every change.

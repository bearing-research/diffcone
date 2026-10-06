# Target manifest

The manifest is the interchange format between discovery and the planner,
and the way to hand-author targets for a runner without discovery.
`diffcone discover -o targets.json` writes one; `--targets targets.json`
reads one.

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

| Field | Meaning |
|---|---|
| `source_roots` | Optional. The source roots the symbols are named against; `--source-root` on the command line overrides it. |
| `runner` | A label: `pytest` and `asv` can be run by `diffcone run`, any other runner only planned. |
| `runner_id` | How the runner names the target: a pytest node ID without parameters, an ASV benchmark name. |
| `entry_symbol` | The dotted identity of the test or benchmark function: the module (named from its source root), then classes and the function. |
| `lifecycle_dependencies` | Symbols the runner executes for the target outside its body: pytest fixtures, ASV `setup`/`setup_cache`, a module symbol (which makes module-level state such as `pytestmark` or `pytest.importorskip` count). `fixture:<name>` marks a fixture that could not be resolved: the target is then selected conservatively. |

diffcone does not infer lifecycle dependencies from naming conventions:
discovery finds them by pytest's and ASV's rules, or you list them. A
target's parameter cases are not modelled; a target is selected as a whole.
Combined with discovery, a manifest entry overrides a discovered target
with the same `runner` and `runner_id`. A manifest may also be a bare JSON
list of targets.

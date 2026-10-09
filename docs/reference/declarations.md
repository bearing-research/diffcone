# diffcone.toml

Some dependencies can't be seen in the code: a plugin registered through
entry points, a handler named in a configuration file, a registry filled
when a module is imported. Declare them in a `diffcone.toml` file at the
root of your repository:

```toml
[[edges]]
from = "pkg.registry.dispatch"
to = "pkg.handlers.json_handler"
why = "handlers register themselves through entry points"

[[edges]]
from = "pkg.cli"
to = "pkg.plugins"
```

Each `[[edges]]` entry says that `from` depends on `to`. A change to `to`
then selects every target that reaches `from`, and the report explains the
selection with your `why`.

| Key | Description |
|---|---|
| `from` | Required. The code that depends on something: a function, method, class or module, by its import path. A class or module includes everything defined in it. |
| `to` | Required. What it depends on, named the same way. |
| `why` | Optional. A short explanation, shown in the report. |

## Tests to run on every change

Some tests are worth running on every pull request whatever it changes: an
end-to-end suite, tests that drive the whole product. Name them with
`[[always_run]]`:

```toml
[[always_run]]
targets = "tests/e2e/*"
why = "end to end"

[[always_run]]
targets = "*.TimeSuite.*"
runner = "asv"
```

Every target an entry matches is selected in every plan, and the report
gives the entry as the reason.

| Key | Description |
|---|---|
| `targets` | Required. A pattern on the target's id, as the report shows it (`tests/e2e/test_flow.py::test_login`, `bench_io.TimeRead.time_csv`). `*` matches any characters, `/` and `::` included; `?` and `[...]` work as in shell patterns. |
| `runner` | Optional. Only this runner's targets (`pytest`, `asv`). |
| `why` | Optional. A short explanation, shown in the report. |

The tests still run when diffcone records a full run (`diffcone collect`),
so the run that records evidence still tests them.

## What can go wrong

Declarations can only add dependencies or selected tests, so they can only
make diffcone select more tests, never fewer.

diffcone reads the file from both snapshots, so it changes along with your
code. A declaration that names something that doesn't exist, an
`always_run` pattern that matches no target, an unknown key, or a file
that doesn't parse is reported as an analysis error: the
plan then selects every target, so a typo can't silently remove tests from
a plan. A declaration between two very large modules (more than 5,000
pairs of symbols) is also an error; declare the specific functions
instead.

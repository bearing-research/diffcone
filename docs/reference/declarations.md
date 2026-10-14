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
| `from` | Required. The code that depends on something: a function, method, class or module, by its import path. A class or module includes everything defined in it, and a package its submodules. |
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
| `runner` | Optional. Only this runner's targets: `pytest`, `asv`, or a runner your manifest's targets use. Any other name is an error. |
| `why` | Optional. A short explanation, shown in the report. |

The tests still run when diffcone records a full run (`diffcone collect`),
so the run that records evidence still tests them.

The report shows how many targets each entry matched. An entry whose
pattern matches nothing isn't an error, because one `diffcone.toml` serves
every job and a job may skip the directory an entry names; the text report
flags it with `MATCHES NOTHING`, and the JSON report gives `"matched": 0`.
diffcone can't tell a mistyped pattern (`test/e2e/*`) from one a job
legitimately skips, so check the count when you add an entry or move
tests.

## Files to leave out of the analysis

Some projects keep `.py` files that are not meant to be imported, and
some of them are not even valid Python: test cases for a formatter or a
linter, say. diffcone can't analyse such a file, so every plan selects
every test, and `diffcone collect` refuses to record. List them under
`[index]`:

```toml
[index]
exclude = ["tests/data/**"]
```

| Key | Description |
|---|---|
| `exclude` | A list of patterns on file paths from the repository root. `*` matches any characters, `/` included; `?` and `[...]` work as in shell patterns. |

A `.py` file that matches is treated like any other file diffcone doesn't
analyse, a data file, say. A change to it makes a plan without a
recording select every test. With a recording, it selects the tests that
read the file or ran its code. The report lists each snapshot's patterns
(`analysis.base.excluded`, `analysis.head.excluded`).

Don't exclude a file pytest collects tests from, or a `conftest.py`: its
tests can't be targets, so the plan says discovery is incomplete, and the
tests under an excluded `conftest.py` are always selected.

## What can go wrong

Declarations can only add dependencies or selected tests, so they can only
make diffcone select more tests, never fewer.

diffcone reads the file from both snapshots, so it changes along with your
code. These are reported as an analysis error:

- a declaration that names something that doesn't exist;
- an `always_run` entry without `targets`, or with an unknown `runner`;
- an `[index]` table that isn't a table, or an `exclude` that isn't a list
  of non-empty patterns;
- an unknown key;
- a file that doesn't parse;
- a file that is there but can't be read (left unmerged by a conflicted
  merge, say);
- a declaration between two very large modules (more than 5,000 pairs of
  symbols): declare the specific functions instead.

An analysis error makes the plan select every target, so these mistakes
can't silently remove tests from a plan. The one mistake diffcone can't
catch is an `always_run` pattern that matches nothing (see above): the
plan stands, and only the count shows it.

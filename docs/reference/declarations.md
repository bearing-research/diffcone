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
read the file, or every test when a test imported it or ran its code (a
later test may use what it left without running any of it). The report
lists each snapshot's patterns (`analysis.base.excluded`,
`analysis.head.excluded`).

Exclude only files nothing imports or runs. diffcone doesn't read what an
excluded file does, so code that imports it, or runs it (`import_module`,
`runpy`, a subprocess, loading it by path), depends on anything: a change
anywhere under the source roots selects it, and the reason names the
excluded file. Excluding a package's `__init__.py` does this to every
import of the package. Excluding a module your code or tests import
costs you that precision; it never makes diffcone skip a test.

Don't exclude a file pytest collects tests from, or a `conftest.py`: its
tests can't be targets, so the plan says discovery is incomplete, and the
tests under an excluded `conftest.py` are always selected. An excluded
pytest plugin makes every test always selected; excluding a module tests
inherit their base class from, or a benchmark file, makes the plan report
discovery incomplete (exit 3).

## Build files your tests don't use

A change to a build file outside your source roots makes every plan select
every test: a compiled source, a CMake or Meson file, a CI workflow, a
submodule. diffcone can't see what your tests' build reads, so it assumes
it reads all of them. In a repository that holds several components
(a C++ library with Python, R and Java bindings, say), most of those
changes belong to another component. Name the paths outside the source
roots that your tests' build and CI do read under `[build]`:

```toml
[build]
reads = ["cpp/**", "format/**", "testing", ".github/workflows/python.yml", "ci/scripts/python_*"]
```

| Key | Description |
|---|---|
| `reads` | A list of patterns on paths from the repository root. `*` matches any characters, `/` included; `?` and `[...]` work as in shell patterns. A pattern also covers everything under a directory it names: `testing` covers the submodule `testing` and every file in it. |

A changed build file or submodule outside the source roots that no pattern
covers is then set aside: it doesn't select anything by itself. The text
report and `diffcone run` list every path set aside, the JSON report
gives them under `build_reads.set_aside`, and
[`diffcone report`](../ci.md#report-on-recent-runs) lists them per job. A plan that uses a recording treats a set-aside
file like any other file: it selects the tests that read it.

Some files are never set aside: a `conftest.py`, your runner's
configuration at the repository root (`pytest.ini`, `pyproject.toml`), and
ASV's configuration still select every test.

!!! warning "This setting can hide a test that would fail"
    Unlike everything else in `diffcone.toml`, `[build] reads` makes plans
    select fewer tests. If your build starts reading a path the list
    doesn't cover (a new workflow, a directory a CMake file adds), a change
    there is set aside and the tests it breaks aren't selected. Keep the
    list next to the build files it describes, review the set-aside paths
    in the report, and compare plans with full runs
    ([`diffcone check`](cli.md)) now and then.

To limit the damage of a stale list, diffcone:

- selects every test when the `[build]` table differs between the two
  snapshots it compares (or, with a recording, the recording's commit);
- reports a pattern that covers no path of its snapshot as an analysis
  error, which also selects every test;
- ignores the table when a source root is the repository itself, since
  nothing is outside the roots then.

## What can go wrong

Declarations can only add dependencies or selected tests, so they can only
make diffcone select more tests, never fewer. The exception is
[`[build] reads`](#build-files-your-tests-dont-use).

diffcone reads the file from both snapshots, so it changes along with your
code. These are reported as an analysis error:

- a declaration that names something that doesn't exist;
- an `always_run` entry without `targets`, or with an unknown `runner`;
- an `[index]` table that isn't a table, or an `exclude` that isn't a list
  of non-empty patterns;
- a `[build]` table that isn't a table, a `reads` that isn't a list of
  non-empty patterns, or a `reads` pattern that covers no path;
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

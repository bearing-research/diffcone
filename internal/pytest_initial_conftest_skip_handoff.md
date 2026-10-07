# Handoff: pytest crashes when a command-line path's conftest skips

Written 2026-10-05 for an agent picking this up cold. Two pieces of work
hang off one pytest defect:

1. **Upstream (pytest):** report and fix the crash. Nothing has been filed.
   Filing an issue or opening a PR publishes under the user's GitHub
   account, so **draft both and have the user approve before posting**.
2. **Downstream (diffcone):** `diffcone run` hits the crash on pandas, which
   blocks the done-when check of roadmap item 6 (`run --collect`). The user
   has not yet chosen how `run` should select tests (see "diffcone side").

## The defect

When paths or node ids are passed on the command line, pytest imports the
conftests along those paths *while parsing the configuration*, before any
session exists. If one of those conftests skips at module level
(`pytest.importorskip(...)` or `pytest.skip(..., allow_module_level=True)`),
the `Skipped` exception escapes as a raw traceback and pytest exits 1. The
same conftest imported during collection (no path arguments) skips its
directory cleanly. So a directory of tests is skipped or crashes the whole
run depending only on how pytest was invoked.

### Minimal reproduction (pytest 9.1.1; main is unchanged)

```
tests/opt/conftest.py   import pytest
                        mod = pytest.importorskip("no_such_module_xyz")
tests/opt/test_a.py     def test_a():
                            pass
tests/test_b.py         def test_b():
                            pass
```

| invocation (`-p no:cacheprovider`) | result |
|---|---|
| `pytest` | `1 passed, 1 skipped`, exit 0 |
| `pytest tests` | `1 passed, 1 skipped`, exit 0 |
| `pytest tests/opt` | traceback ending `Skipped: could not import 'no_such_module_xyz'`, exit 1 |
| `pytest tests/opt/test_a.py::test_a` | same traceback, exit 1 |

`pytest tests` works only because, for each argument, pytest loads the
conftests of the argument's directory and of its `test*` subdirectories
(`opt` does not match `test*`). Rename `opt` to `test_opt` and that case
crashes too. Found on pandas: `pandas/tests/io/pytables/conftest.py` does
`tables = pytest.importorskip("tables")`, so any invocation naming a
pytables test crashes in an environment without PyTables.

### Root cause, in pytest's main branch

Line numbers are from `main` as fetched on 2026-10-05.

* `src/_pytest/config/__init__.py`
  * `PytestPluginManager._set_initial_conftests` (line 646) turns each
    command-line argument into an anchor directory (stripping `::` node-id
    parts, adding `test*` subdirectories) and calls
    `_loadconftestmodules` for each (around line 690).
  * `_loadconftestmodules` (line 712) imports every `conftest.py` from the
    root down to the directory and caches the list in `_dirpath2confmods`
    only after the loop finishes, so an exception leaves the directory
    uncached.
  * `_importconftest` (line 761) wraps import errors with
    `except Exception as e:` and raises `ConftestImportFailure` (lines
    792-794). `_main` turns that into a readable error and exit 4
    (line 252); `Config._preparse` lets `--help`/`--version` survive it
    (line 1741).
* `src/_pytest/outcomes.py`: `OutcomeException` derives from
  **`BaseException`** (line 13), and `Skipped` (line 39) and `Failed`
  (line 59) derive from it. (`Exit` is an `Exception` on main, line 65.) So
  a `Skipped` raised by a conftest bypasses `except Exception` and nothing
  catches it.
* `src/_pytest/runner.py`: during collection the same import happens
  inside a collector, and `pytest_make_collect_report` (line 388) treats
  `Skipped` as a skip outcome (`skip_exceptions = [Skipped]`, line 415).
  That is why the no-argument run works.

### Existing issues (none is this one)

* [#12371](https://github.com/pytest-dev/pytest/issues/12371), open:
  `pytest.skip(allow_module_level=False)` in a conftest still skips the
  whole package.
* [#7085](https://github.com/pytest-dev/pytest/issues/7085), open: pytest
  imports a conftest after the package `__init__` skipped.
* [#11662](https://github.com/pytest-dev/pytest/issues/11662): `Skipped`
  escaping another code path (accessing `Module.obj` in a hook) caused an
  INTERNALERROR.

Search again before filing: `gh search issues` returned nothing, but it may
not have been authenticated.

## Proposed upstream fix

**Behaviour to aim for:** an invocation that names tests under a skipping
conftest behaves like the full run, so those tests (or that directory) are
reported as skipped, not a crash. For the reproduction,
`pytest tests/opt/test_a.py::test_a` should report one skip.

**Likely shape:** when loading *initial* conftests, catch `Skipped` from a
conftest import and leave that directory unloaded, so collection imports
the conftest again inside a collector, where `Skipped` is already handled.
Either:

* in `_set_initial_conftests`, wrap the per-anchor `_loadconftestmodules`
  call in `try: ... except Skipped: pass`; or
* in `_loadconftestmodules`, stop the loop at a `Skipped` (the conftests
  above it are already imported and registered).

**Verify before trusting the shape:**

1. **Retry.** Collection must really import the conftest again. A failed
   import is not added to `_dirpath2confmods`, and `importlib` removes a
   failed module from `sys.modules`, so it should be. Check this with the
   test below, not by reading the code.
2. **Node ids under a skipped collector.** Check what `Session` reports when
   the collector on the path to a requested node id is skipped: one skip
   and exit 0, or a "not found" error. The fix should give the skip.
   Compare with an existing case where a test *module* named on the command
   line skips at module level.
3. **Exceptions it must not swallow.** `pytest.exit()` is an `Exception` on
   main and already becomes `ConftestImportFailure`; leave that as it is.
   `pytest.fail()` in a conftest (`Failed`, also an `OutcomeException`)
   currently crashes the same way. The narrow fix leaves it alone; whether
   to wrap other `OutcomeException`s as `ConftestImportFailure` is a
   question for the issue. `unittest.SkipTest` is an `Exception`, so it
   becomes a usage error today; mention it but keep it out of scope unless
   maintainers want it.
4. **`--help` / `--version`** with a skipping initial conftest must still
   work.
5. **`--confcutdir`, `--noconftest`, `--pyargs`, and `testpaths`** paths
   through `_set_initial_conftests` should be unaffected.

**Tests** (pytest's suite, with the `pytester` fixture; look for existing
initial-conftest tests in `testing/test_conftest.py`, and module-level skip
tests in `testing/test_skipping.py` and `testing/test_collection.py`):

* the reproduction: a node id, a directory argument, and a `test_*`-named
  parent argument each report the skip, with no traceback and exit 0 (or
  whatever exit code the equivalent module-level skip produces today);
* a conftest *above* the argument's directory that skips;
* `pytest.skip(..., allow_module_level=True)` as well as `importorskip`;
* `pytest.exit()` in an initial conftest is unchanged.

**pytest conventions:** a `changelog/<issue-number>.bugfix.rst` fragment
(so the issue comes first), `AUTHORS` if the contributor is new,
`pre-commit run --all-files`, and the relevant `tox` / `pytest testing/...`
subset. Read `CONTRIBUTING.rst` in the pytest repo before opening the PR.

**Suggested order:** draft the issue (title such as "Skipped raised by a
conftest of a command-line path crashes pytest instead of skipping"),
including the reproduction table and the root cause. Show it to the user,
and file it only on their approval. Then the patch and tests on a fork
branch, opened as a PR referencing the issue, again only on approval.

## diffcone side (state on 2026-10-05)

**Why it matters here.** `diffcone run` passes the selected tests' node ids
to pytest (`execution.build_command`, `src/diffcone/execution.py`), so any
selection that includes a test under a skipping conftest crashes pytest.
That holds for static and evidence plans alike, and an upstream fix will
not reach older pytest versions. The crash exits 1, the same code as "tests
failed".

**Decided and fixed in diffcone** (2026-10-05): `run` no longer names the
selected tests on pytest's command line. pytest collects from its own
starting points and `-p diffcone_select` (`src/diffcone/selection.py`)
deselects the rest, so conftests load as in a full run (see the `run`
bullet in `internal/design.md`). Selected targets pytest did not collect are
reported. `--collect` now says plainly when pytest stopped before any test
process finished, instead of folding an empty record directory. An
upstream fix is still worth filing: other tools that pass node ids hit the
same crash.

**Unblocked: roadmap item 6's done-when** (`internal/roadmap.md`, "6.
Advancing an evidence store"). `run --collect` is implemented, tested on the
fixture repository and pushed (`59c5f01`). The pandas chain check stopped
at its first advance (commit `4e48725269`) because of this crash; rerun it
with the fix.

* Script: `scripts/evidence_recall.py --advance`.
* Scratch state lives in the session scratchpad and may already be gone
  (it was wiped once): the pandas clone and Python 3.13 environment
  (`pdenv`), saved full-suite coverage runs (`recall/run1/cov-*.db`,
  `outcomes-*.json`, linked into `recall/chain/`), the full recordings at
  the four anchor commits moved to `recall/full_stores/` for comparison,
  and `dbg/compare_stores.py`. That last script compares each advanced
  store with the full one at the same commit (symbols, paths, dirs and the
  unstable flag per test).
* To rerun, the command is
  `scripts/evidence_recall.py --advance --repo PANDAS --command
  "PY -m pytest -n 8 -m 'not slow and not network and not db and not
  single_cpu' -W ignore" --out OUT --range 3f57341~24..3f57341 --setup "PY
  -c 'import pandas'"`. `run --collect` requires the store's arguments
  exactly; a positional path such as `pandas` is fine now that selection
  goes through the plugin.
* Done when: 100 % recall over the chained plans against the saved coverage
  runs, and every test's advanced record matching the full recording at
  the anchors, with any differences explained. Then record the result in
  `internal/evaluation.md`, and update roadmap item 6's status.

**Already confirmed:** pandas recall of evidence plans is 100 % over 20
commit pairs (`internal/evaluation.md`, "pandas: recall of evidence plans").
The remaining stage-4 work for evidence, the corpus re-plan, was deferred
by the user.

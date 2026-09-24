# Evaluation on real repositories

Numbers produced by `diffcone corpus --coverage`, which replays a commit
range: for each parent-to-commit pair it plans, runs the full pytest suite
at both snapshots, and checks the plan against (a) tests whose pass/fail
outcome changed and (b) tests that executed changed code under per-test
coverage. Re-run these whenever selection rules change; the numbers below
were produced by the rules at the commit that last touched this file.

Definitions, exactly as `CorpusReport` computes them:

* **affected**: a test that executed a line owned by a changed symbol, at
  either snapshot, whose change carries impact (additive-only changes do
  not count; a symbol deleted in head is attributed from the base run);
* **recall** = affected tests that were selected / affected tests, pooled
  over the validated commits; it must be 100 % for the run to pass;
* **precision** = affected tests that were selected / selected tests *that
  ran under coverage*, pooled over the validated commits. The denominator
  excludes selected targets the suite did not run (deselected by the
  project's own `addopts`, skipped by markers), which is why a row can show
  11 selected and 80 % precision;
* **savings** = 1 − selected / targets, per commit; the total is the mean
  over validated commits;
* **denominators**: *targets* is the static discovery count at head;
  *tests that ran under coverage* is the number of test functions with a
  coverage context (the suite may collect parametrised cases of the same
  function, skip tests, or deselect some through `addopts`, so it differs
  from both the discovery count and the runner's collected count).

`--max N` keeps the last N commits of the range *before* commits without
`.py` changes are skipped, so a run may show fewer validated rows than N.

## toolz (pytoolz/toolz, 193 tests, flat layout)

Last six commits at the time of measurement (all touch Python), suite
runtime about half a second, discovery clean (no notes). Reproduce with:

```bash
git clone --depth 80 https://github.com/pytoolz/toolz.git
cd toolz && uv venv .venv && uv pip install -p .venv/bin/python -e . pytest pytest-cov
diffcone corpus --repo . --range HEAD~40..HEAD --discover pytest \
  --command ".venv/bin/python -m pytest" --coverage --max 6
```

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| d287360 | Add frozendict signature | 186 / 186 | 0 % | 100 % | 1 % |
| 80ddcb3 | Add tests for get_in defaults, ... | 190 / 190 | 0 % | 100 % | 2 % |
| a1e25cb | Expose combined `__annotations__` on composed functions | 192 / 192 | 0 % | 100 % | 4 % |
| 55ce42d | Support Python 3.15 and refresh dev tooling | 192 / 192 | 0 % | n/a | 0 % |
| d2eba03 | Fix interpose([]) raising StopIteration | 193 / 193 | 0 % | 100 % | 1 % |
| 451af60 | Add pysentry-pre-commit | 0 / 193 | 100 % | n/a | n/a |

Totals: 7 outcome changes, 0 missed; recall 100 % (15 of 15); precision
2 %; mean savings 17 % under the import-time rule (see "Re-measurements"); 65 % before it. The table is from the re-run after classes came
to depend on their special methods (see "Re-measurements"): about 45 more
tests per commit, through `curry.__reduce__`, which imports the curried
function's module by its runtime name (`import_module(modname)`) and so
is an unbounded dynamic import; every test that uses `curry` now reaches
it through the class. The notes below describe the run before that.

Why the low-precision rows are low:

* **Import-time changes are invisible to coverage.** d287360 adds entries
  to a module-level signature registry and 55ce42d touches module-level
  code; those lines run at import, which carries no test context, so
  coverage credits no test although the tests selected through
  module-attribute references genuinely observe the change. Precision is
  understated there by construction.
* **A dynamic `exec` helper.** `tests/test_inspect_args.py::make_func`
  builds functions with `exec`, and its module imports all of toolz, so its
  import closure is the whole package: about 17 tests are selected on every
  production change. `_signatures.create_signature_registry` calls
  `import_module` (imported with `from importlib import import_module`)
  on each key of a module-info dict, a dynamic import with a runtime
  name, which can reach anything and is therefore affected by every
  change, tests-only changes included. It runs when `toolz` is imported
  and writes (`mutated_by`) the `_signatures.signatures` registry that
  `has_keywords`, hence `memoize` and `curry`, read: 32 tests reach it
  through the package or that registry. d2eba03 (a one-line `interpose`
  fix) selects 37 tests for that reason alone, 2 of which execute the
  change; 80ddcb3 only adds tests and selects 36, the 4 new tests and
  those 32. Until the dynamic import was recognised under that spelling
  (see "Re-measurements"), the tests-only commit selected 4.
* a1e25cb changed `Compose` structurally (new members), which invalidates
  every `Compose` method; only 8 tests execute `Compose` at all.

## click (pallets/click, 555 tests, `src` layout)

Last six commits at the time of measurement; three touch Python, the other
three (docs and release commits) are skipped. Suite runtime about five
seconds. Discovery resolved every fixture (the conftest `runner` chain) with
no notes. Reproduce with:

```bash
git clone --depth 120 https://github.com/pallets/click.git
cd click && uv venv .venv && uv pip install -p .venv/bin/python -e . pytest pytest-cov
diffcone corpus --repo . --range HEAD~60..HEAD --discover pytest \
  --source-root src --source-root tests \
  --command ".venv/bin/python -m pytest" --coverage --max 6
```

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| 2103e15 | Forward all user's parameters set in `PAGER` | 537 / 537 | 0 % | 100 % | 4 % |
| e1fd594 | Add support of `pathlib.Path` to `edit` | 67 / 538 | 88 % | 100 % | 13 % |
| 6aabf09 | Stable (a 30-file squash: `Option` restructured, tests reorganised) | 555 / 555 | 0 % | 100 % | 76 % |

Totals: 54 outcome changes, 0 missed; recall 100 % (441 of 441); precision
39 %; mean savings 29 % under the import-time rule (see "Re-measurements"),
65 % before it. Two of the three commits still select every test: every
test depends on `conftest.py`, which imports `click`, and 2103e15 changes
functions decorated with `@contextlib.contextmanager` (a decorator runs
at import) in a module with a module-level dynamic reference, 6aabf09 a
class body; e1fd594 changes only type hints under `from __future__ import
annotations` and selects 67.

The squash commit is wide because `click.core.Option` changed structurally
(a method was added), which invalidates every `Option` method and hence
every test that defines an option; 78 % of those tests do execute changed
lines, so the width is mostly real.

## structlog (hynek/structlog, 517 tests, `src` layout, async tests)

Last eight commits at the time of measurement; three touch Python, the
other five (docs, CI, sponsors) are skipped. Suite runtime about 1.5
seconds; uses `--import-mode=importlib`, pytest-asyncio, time-machine and
pytest-randomly (disabled through the command so outcomes are comparable
between snapshots). Discovery resolved every fixture with no notes.
Reproduce with:

```bash
git clone --depth 120 https://github.com/hynek/structlog.git
cd structlog && uv venv .venv && uv pip install -p .venv/bin/python -e . \
  pytest pytest-cov pytest-asyncio pytest-randomly simplejson time-machine
diffcone corpus --repo . --range HEAD~80..HEAD --discover pytest \
  --source-root src --source-root tests \
  --command ".venv/bin/python -m pytest -p no:randomly" --coverage --max 8
```

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| e26945d | fix: PytestRemovedIn10Warning | 34 / 516 | 93 % | 100 % | 6 % |
| d8e321a | Use built-in product | 34 / 516 | 93 % | 100 % | 100 % |
| 73393f3 | Better bankruptcy | 128 / 517 | 75 % | 100 % | 33 % |

Totals: 1 outcome change, 0 missed; recall 100 % (78 of 78); precision
40 %; mean savings 87 % under the import-time rule (see "Re-measurements"); 95 % before it.

The first structlog run reported two outcome misses for
`tests/test_tracebacks.py::test_recursive`, which passed at base and
failed at head on two unrelated commits. The test depends on recursion
depth and fails under the coverage tracer; the base suite had run
uninstrumented while the head suite ran under coverage. Both suites now
run under the tracer when `--coverage` is requested (lesson recorded
below).

## pytest-mock (pytest-dev/pytest-mock, 69 tests, entry-point plugin)

The project is itself a pytest plugin: its `mocker` fixture is registered
through a `pytest11` entry point rather than a conftest, and the plugin's
`pytest_configure` hook monkeypatches `unittest.mock` at session start.
Last six commits; one touches Python. Reproduce with:

```bash
git clone --depth 120 https://github.com/pytest-dev/pytest-mock.git
cd pytest-mock && uv venv .venv && uv pip install -p .venv/bin/python -e . pytest pytest-cov pytest-asyncio
diffcone corpus --repo . --range HEAD~60..HEAD --discover pytest \
  --source-root src --source-root tests \
  --command ".venv/bin/python -m pytest" --coverage --max 6
```

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| a8bd0b1 | Honour resetall() arguments for non-callable mocks | 69 / 69 | 0 % | 100 % | 7 % |

Every test depends on the plugin's hooks, so any change to the hook chain
selects the whole suite; this commit changed one `MockerFixture` method and
the 25 selected tests are those that reach `MockerFixture` through the
fixture or by name.

## attrs (python-attrs/attrs, 667 tests, `src` layout, hypothesis)

Last 40 commits; 16 touch a `.py` file and were validated, the other 24
(docs, CI, changelog) were skipped. Suite runtime about six seconds. Uses pytest 9's native
`[tool.pytest]` table and hypothesis `@given`. Discovery is clean once both
are understood (see lessons). Reproduce with:

```bash
git clone --depth 150 https://github.com/python-attrs/attrs.git
cd attrs && uv venv .venv && uv pip install -p .venv/bin/python -e . \
  pytest pytest-cov cloudpickle hypothesis pympler
diffcone corpus --repo . --range HEAD~120..HEAD --discover pytest \
  --source-root src --source-root tests \
  --command ".venv/bin/python -m pytest" --coverage --max 40
```

Totals: 26 outcome changes, 0 missed; recall 100 % (1 869 of 1 869);
precision 45 %; mean savings 60 % under the import-time rule (see "Re-measurements"); 66 % before it. The 16 validated commits (nine touch
only docstrings, `typing_tests/` examples outside the source roots, or
test-typing stubs, change no symbol with impact, and select nothing; they
count at 100 % savings):

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| 6851ab5 | Defer imports on the cold import path | 655 / 655 | 0 % | 100 % | 83 % |
| 97f8d17 | Fix ClassVar forward reference detection | 656 / 656 | 0 % | 100 % | 13 % |
| 4b5b295 | Make on_setattr hooks accept generators | 667 / 667 | 0 % | 100 % | 52 % |
| 5aa76a4 | Add `ne` validator | 241 / 667 | 64 % | 100 % | 2 % |
| 9b98a73 | Drop Python 3.9 | 666 / 666 | 0 % | 100 % | 83 % |
| 3e01de4 | docs: fix markup (removes a `from . import` binding) | 666 / 666 | 0 % | 100 % | 1 % |
| f53fc54 | Stop evolve dunders from being modified | 667 / 667 | 0 % | 100 % | 52 % |
| 9 others | docstrings, `typing_tests/` examples, typing stubs | 0 | 100 % | n/a | n/a |

The remaining wide rows share one cause: `attr/__init__.py` is a hub whose
import list changed (3e01de4 removes a `from . import` binding, which is
structural by rule), or the change is in `_make.py`, which every attrs
class definition runs through.

## pytest (pytest-dev/pytest, 3 486 discovered tests, 4 538 collected cases, `src` layout)

The multi-minute suite: 2 min 18 s serial for a plain run. Discovery finds
3 486 test functions (2 841 before the `python_files` fix in lesson 5);
pytest collects 4 538 cases from them because of parametrisation, and
3 436 of the functions ran under coverage (the rest are skipped by markers
or platform). Loads its own
`pytester` plugin through `addopts = ["-p", "pytester"]`, collects extra
files through `python_files = ["testing/python/*.py"]`, and needs a
build-generated `_version.py` in every checkout. One parent-to-commit pair
("Warn when writing or closing a cache file fails", which changed
`Cache.set`) validated with coverage:

```bash
git clone --depth 200 https://github.com/pytest-dev/pytest.git
cd pytest && uv venv .venv && uv pip install -p .venv/bin/python -e . \
  attrs hypothesis requests xmlschema mock setuptools argcomplete pygments pytest-cov
diffcone validate --repo . --base HEAD~1 --head HEAD --discover pytest \
  --source-root src --source-root testing \
  --command ".venv/bin/python -m pytest" \
  --setup-command "cp $PWD/src/_pytest/_version.py src/_pytest/_version.py" --coverage
```

| metric | value |
|---|---|
| wall time for the pair (two suites under coverage) | 8 min 14 s |
| outcome changes | 1, caught |
| tests that executed a changed symbol | 1 490 of the 3 436 that ran under coverage |
| recall | 100 % |
| precision | 43 % (1 490 of the selected tests that ran under coverage) |
| selected | 3 485 of 3 486 discovered |

Nearly everything is selected because pytest dispatches hooks by name
through pluggy and `pytester` runs a full inner session from within tests:
statically every `hook.pytest_*` call name-matches every implementation,
and 1 490 tests really do execute the changed method.

A six-pair corpus (last eight commits, two skipped) with `--jobs 4`:

```bash
diffcone corpus --repo . --range HEAD~40..HEAD --discover pytest \
  --source-root src --source-root testing \
  --command ".venv/bin/python -m pytest" \
  --setup-command "cp $PWD/src/_pytest/_version.py src/_pytest/_version.py" \
  --coverage --max 8 --jobs 4
```

| metric | value |
|---|---|
| wall time | 21 min 32 s (about 50 min serially) |
| outcome changes | 17, 0 missed |
| tests that executed a changed symbol | 4 960 |
| recall | 100 % |
| precision (pooled) | 24 %, per commit 0 % to 53 % |
| selected | every test on every commit |

Selection is total on every commit, including three whose changes are
confined to the test tree or remove dead code, because the `pytester` and
hook chains reach every test from any change in `_pytest` or
`testing/conftest.py`. This is the documented cost of hook-style
dispatch, not a defect the corpus revealed; it makes pytest a recall
benchmark rather than a savings one. The `--jobs` done-when asked for
under 20 minutes; 21.5 is the honest number on this machine with
coverage runs competing for CPU.

## pipx (pypa/pipx, 699 tests, `src` layout, installed plugin fixtures)

The first repository whose tests request fixtures from *installed*
plugins: `mocker` (pytest-mock) in 152 tests and `fake_process`
(pytest-subprocess) in one. Before the well-known plugin fixture table
(commit 69581fe) those 153 tests fell back to select-all on every change;
now both names are assumed and reported. The suite installs packages into
throwaway venvs (12 min serial, 27 min for this corpus with two jobs), so
the `--setup-command` copies the project's pre-populated package cache
(160 MB; the session fixture runs the cache update script against it, so
parallel checkouts must not share one) into each checkout along with the
build-generated `version.py`. Last four
commits, two touch Python. Reproduce with:

```bash
git clone --depth 80 https://github.com/pypa/pipx.git
cd pipx && uv venv .venv && uv pip install -p .venv/bin/python -e . --group test pytest-cov
.venv/bin/python -m pytest -q tests   # populates .pipx_tests/package_cache
diffcone corpus --repo . --range HEAD~40..HEAD --discover pytest \
  --source-root src --source-root tests \
  --command "$PWD/.venv/bin/python -m pytest -p no:cacheprovider -q" \
  --setup-command "cp -R $PWD/.pipx_tests .pipx_tests && cp $PWD/src/pipx/version.py src/pipx/version.py" \
  --coverage --max 4 --jobs 2
```

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| b83f660 | fix(upgrade): don't claim 'latest version' for local-path installs | 698 / 698 | 0 % | 100 % | 6 % |
| 84eaad3 | fix(reinstall): propagate returned failures in reinstall-all | 699 / 699 | 0 % | 100 % | 2 % |

Totals: 0 outcome changes, 0 missed; recall 100 % (50 of 50); precision
4 %; mean savings 0 % under the import-time rule (see "Re-measurements"); 33 % before it (the counts below are from
earlier runs). No test needed `--assume-external-fixture`. The
precision is bounded by the CLI's shape, not by a resolution gap. Every
test that drives the CLI through `run_pipx_cli` (456 of the 461 tests
selected for b83f660, 461 of the 462 for 84eaad3) is selected under the
`dynamic_reference` rule, because `get_command_parser` and
`run_pipx_command` call `vars(args)` on the argparse namespace. Removing
`vars` from the dynamic calls in an experiment selected exactly the same
tests through a static chain instead: `get_command_parser` registers
every subcommand handler with `set_defaults(func=_cmd_<name>)`, so it
references all of them, each `_cmd_<name>` calls its `commands.<name>`,
and every CLI test calls the parser builder. Which subcommand a test
actually invokes is decided by the argument strings it passes, and
argument-sensitive analysis is out of scope (AGENTS.md), so a CLI whose
entry point registers every handler stays total on command changes.
(Treating `vars(x)` as non-dynamic is not safe either: toolz passes
module objects through loop variables, `for mod in ...: vars(mod)`.)

## opentelemetry-python, SDK session (monorepo, 830 tests, `src` layouts)

A monorepo whose packages (`opentelemetry-api`, `opentelemetry-sdk`,
`opentelemetry-semantic-conventions`, a shared `tests/opentelemetry-test-utils`
package, exporters, shims) each carry a `tests/` tree and run as separate
tox sessions. One plan models one session: the SDK session imports four
packages and collects `opentelemetry-sdk/tests`. Planning the API and SDK
test trees together collides on same-named test modules (`context`,
`trace.test_globals`) and degrades, which is also how one pytest session
over both would fail. Test classes inherit from bases in the shared
test-utils package (`ConcurrencyTestBase`), which discovery reports as
unknown bases (12 notes; those methods are found through the class's own
definitions). With per-root prefixes (`opentelemetry-api/tests=api_tests`,
`opentelemetry-sdk/tests=sdk_tests`) both trees plan together without an
analysis error (1 107 targets), which is what an `--import-mode=importlib`
session over both would need. Last twelve commits, ten touch Python,
suite 18 s. Reproduce with:

```bash
git clone --depth 100 https://github.com/open-telemetry/opentelemetry-python.git
cd opentelemetry-python && uv venv -p 3.13 .venv \
  && uv pip install -p .venv/bin/python -r opentelemetry-sdk/test-requirements.txt pytest-cov
diffcone corpus --repo . --range HEAD~80..HEAD --discover pytest \
  --source-root opentelemetry-api/src --source-root opentelemetry-sdk/src \
  --source-root opentelemetry-semantic-conventions/src \
  --source-root tests/opentelemetry-test-utils/src --source-root opentelemetry-sdk/tests \
  --command "$PWD/.venv/bin/python -m pytest -p no:cacheprovider -q opentelemetry-sdk/tests" \
  --coverage --max 12 --jobs 3
```

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| 5aa2f8f | logs: add Enabled support to Logger API, SDK, and LogRecordProcessor | 810 / 827 | 2 % | 100 % | 10 % |
| 477ffd4 | opentelemetry-docker-tests: add Prometheus exporter docker tests | 0 / 827 | 100 % | n/a | n/a |
| ab22674 | fix(opentelemetry-sdk): keep synchronous gauge values across cumulative collections | 795 / 830 | 4 % | 100 % | 11 % |
| cfad5eb | Fix TraceState.update to only update already existing keys | 796 / 830 | 4 % | 100 % | 0 % |
| 34c5e5f | Added guard against negative value of max_value_len | 815 / 830 | 2 % | 100 % | 50 % |
| ee219ad | test(exporter-otlp-proto-grpc): relax timing delta ... | 0 / 830 | 100 % | n/a | n/a |
| 5843c4e | DOC(exporter-otlp-proto-http): clarify endpoint= kwarg ... | 0 / 830 | 100 % | n/a | n/a |
| b599a00 | opentelemetry-sdk: don't read other resource attributes ... | 815 / 830 | 2 % | 100 % | 47 % |
| 9bbc005 | docs(sdk): fix typos in SpanLimits docstring | 0 / 830 | 100 % | n/a | n/a |
| 5321c60 | docs(sdk): remove stale trace_config TODO | 0 / 830 | 100 % | n/a | n/a |

Totals: 14 outcome changes, 1 reported as missed, which is the flaky
timing test described below (`PASSED -> None` on 477ffd4, a commit that
changes only docker tests and selects nothing), not a selection miss;
recall 100 % (955 of 955); precision 24 %; mean savings 51 %. The table
is from the re-run after the third batch's rules (see "Re-measurements");
mean savings was 60 % before the import-time rule and 56 % under it,
and the third batch's rules added up to 152 tests on a commit (cfad5eb:
644 to 796). An earlier re-run reported two outcome misses, both that
timing test,
`test_batch_processor.py::TestBatchProcessor::test_shutdown_allows_1_export_to_finish`,
producing no result in one of the two runs (`None -> PASSED` on ee219ad,
`PASSED -> None` on 9bbc005, commits that change only another package's
tests and a docstring): a flaky test, not a selection miss. The counts in
the notes below are from the first run. Commits outside the session's packages (exporters,
docker tests, docs) select nothing, as they should. The two lowest rows
(checked with `validate --coverage --format json` on the pair):

* cfad5eb changes one method, `TraceState.update`; of the 559 selected
  tests, 557 ran under coverage and one executed it, because `update` is
  also what every dict on an unresolved receiver is called with and an
  unresolved `.update(...)` matches every in-scope symbol of that name
  (the name-bounded fallback, design.md "Uncertainty and fallbacks").
  Precision 0 % is 1 in 557 rounded.
* 5aa2f8f adds `enabled` to the API's `Logger`, `NoOpLogger` and
  `ProxyLogger` classes: a structural class change invalidates every
  member, and the SDK's logger tests construct these classes, so 565
  tests are selected, 563 of which ran under coverage and 78 executed a
  changed symbol.

(Precision's denominator is the selected tests that ran under coverage,
as defined at the top; the table's *selected* column counts every
selected target.)

Before the class-level `mock.patch` fix (commit 96d123c) seven tests of
this session fell back to select-all because the class decorator's
injected argument looked like an unknown fixture.

## hatch (pypa/hatch, 2 106 discovered tests, one session over two packages)

A monorepo that runs one pytest session over `src` (hatch), `backend/src`
(hatchling) and `tests`: cross-package imports (the CLI tests build
projects through hatchling) and one conftest tree. Suite 4 min serial;
`src/hatch/_version.py` is build-generated and untracked, so the setup
command copies it. Last eight commits, six touch Python. Reproduce with:

```bash
git clone --depth 100 https://github.com/pypa/hatch.git
cd hatch && uv venv -p 3.13 .venv && uv pip install -p .venv/bin/python -e backend -e . \
  filelock flit-core trustme editables pytest pytest-cov pytest-mock pytest-randomly \
  pytest-rerunfailures pytest-xdist
diffcone corpus --repo . --range HEAD~60..HEAD --discover pytest \
  --source-root src --source-root backend/src --source-root tests \
  --command "$PWD/.venv/bin/python -m pytest -p no:cacheprovider -p no:randomly -q" \
  --setup-command "cp $PWD/src/hatch/_version.py src/hatch/_version.py" \
  --coverage --max 8 --jobs 3
```

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| b6aaa1a | release Hatchling v1.32.1 | 2105 / 2105 | 0 % | n/a | 0 % |
| 248141c | Use hatchling plugin manager | 2105 / 2105 | 0 % | 100 % | 1 % |
| 6a2b14f | release Hatchling v1.32.2 | 2105 / 2105 | 0 % | n/a | 0 % |
| 0941887 | release Hatchling v1.32.3 | 2105 / 2105 | 0 % | n/a | 0 % |
| 5c6dc6c | Strip surrounding whitespace from version metadata | 2106 / 2106 | 0 % | 100 % | 14 % |
| cd57f68 | Revert type changes for BuildHookInterface | 2106 / 2106 | 0 % | 100 % | 22 % |

Totals: 0 outcome changes, 0 missed; recall 100 % (788 of 788); precision
6 %; mean savings 0 %, 30 min with two jobs. Every row selects every test.
The three releases change only `hatchling.__about__.__version__`, and
every test builds a `Platform` (the `PLATFORM` conftest constant), whose
`modules` attribute is a `LazilyLoadedModules` whose `__getattr__` calls
`import_module(name)`: a dynamic import that can reach any module. Since
classes depend on their special methods (see "Re-measurements"), every
user of `Platform` reaches that `__getattr__`, so every change selects
every test; before that rule the lookup was invisible, and the release
rows selected 152 (after instance-attribute tracking bounded
`ClassRegister.collect`'s `getattr(registered_class, self.identifier,
None)`), 2 105 before it. The honest reading: `platform.modules.<name>`
can import any module by the name of an attribute access, and diffcone
cannot bound that without knowing which names are accessed on it.
248141c changes `PluginManager` itself.
The last two rows change properties that every test reaches by name
through untyped receivers: 5c6dc6c changes `ProjectMetadata.version`
(1 691 tests via `installed_dist.version` alone), and cd57f68 changes
builder and build-hook interfaces whose `root`, `config` and `metadata`
properties are matched from `self.root`, `app.config` and
`project.metadata`.

## diffcone itself (129 tests)

Last six commits at the time of writing (`corpus --range HEAD~12..HEAD
--max 6 --jobs 2 --coverage`, 14 min): 0 outcome changes, 0 missed;
recall 100 % (344 of 344); precision 56 %; mean savings 18 %. Four of the
six commits changed the indexer, the cache or discovery, which nearly
every test exercises (114 of 121 up to 119 of 127 selected). Of the two
commits that changed only the execution module, 314cb71 selected 26 of
128 tests at 58 % precision, and 6836992 selected 122 of 129 at 14 %:
it changed `validate_pytest`, which the scenario fixture's cache check
and every validation test reach, and added a helper that the whole
execution module's tests import.

## How the numbers moved

Every planner or indexer change below came from a concrete path in a
corpus report, and each was re-measured on the same commits.

From the first toolz run (mean savings 73 % → 80 % → 87 %, recall 100 %
throughout):

1. `getattr(x, name)` with `name` drawn from a literal tuple was an
   unbounded dynamic reference (an always-on seed); it is now expanded over
   the literal candidates.
2. Adding a name to a module's import list marked the module structural,
   invalidating every test that lists the module as a lifecycle dependency
   and, through the module's helper class, name-matching 61 tests
   elsewhere; pure additions now carry no impact.
3. Dynamic references were affected by any change anywhere; they are now
   bounded by their module's import closure (dynamic imports excepted).

From the first click run (mean savings 10 % → 65 %; the first run's
recall was measured against the wrong code, see item 2):

1. `click._compat._is_compat_stream_attr` does `getattr(stream, attr)`
   with `attr` a parameter and was an always-on seed reachable from every
   test: on commit e1fd594 it accounted for 427 of the 464 selections. Its
   two call sites pass `"encoding"` and `"errors"`; call-site literals are
   now propagated into such parameters.
2. The validation runs imported the editable-installed clone instead of
   the temporary checkout: with a `src` layout the working directory does
   not put the package on `sys.path`, so the installed copy won, outcomes
   were measured against the wrong revision and coverage attributed nothing
   to the package. toolz's flat layout had hidden this. Validation now puts
   the checkout's source roots first on `PYTHONPATH` and fails when
   measured files outside the checkout shadow files inside it.
3. Removed tests are reported as removed, not as outcome misses, and
   additive-only module changes are not coverage ground truth.
4. Parameter ids containing spaces and pipes (`[TEXT: a|b]`) broke the
   node id parsers.

From the attrs and pytest runs (attrs mean savings 38 % → 61 % → 67 %):

1. attrs uses pytest 9's native `[tool.pytest]` table, which discovery did
   not read; hypothesis `@given` arguments (keyword by name, positional
   filling the last parameters) were taken for fixture requests.
2. Docstring edits on hub functions selected ~580 of 656 tests; docstrings
   are now hashed separately and a docstring-only edit carries no impact.
3. A one-set edit in `attr/__init__.py` reached 417 tests because every
   module-level binding was folded into the module symbol; simple
   `NAME = expr` bindings are now symbols of their own. That change first
   *lost* recall on toolz (a registry mutated in place at module level,
   then a registry filled inside a function from another variable, then a
   function whose parameter defaults were the variables): a variable's
   hash and coverage lines now include its module-level mutation
   statements, readers of a variable depend on the functions that mutate
   it, defaults resolve in the enclosing scope, and a parameter defaulting
   to a variable aliases it.
4. Changing how hashes are computed without bumping the cache format made
   a cached base index disagree with a fresh head index, producing phantom
   changed symbols; the cache key now fingerprints the indexer's source.
5. pytest's own suite: `-p pytester` in `addopts` was not honoured,
   `python_files` entries with a directory were matched against the
   basename (645 tests invisible, 356 reported as misses), and a fresh
   checkout lacks the generated `_version.py` (`--setup-command`).

From the first structlog run:

1. With `--coverage`, the base suite ran uninstrumented and the head suite
   under the tracer; a recursion-depth test flipped, producing false
   outcome misses. Both suites now run under the same instrumentation and
   the corpus outcome cache is keyed by mode.

From the first pytest-mock run (savings 19 % → 0 % → 64 %):

1. Fixtures registered through the project's own `pytest11` entry point
   were unknown to discovery (55 tests carried `fixture:mocker`); entry
   points from `pyproject.toml` and `setup.cfg` are now plugin modules,
   following one level of re-exports, and their hooks are lifecycle
   dependencies of every test. That last part first *widened* selection
   (0 %), because:
2. `mocker = pytest.fixture()(_mocker)` is an assignment-style fixture,
   now recognised; and
3. the hook's `for method, wrapper in wrappers.items(): getattr(m, method)`
   over a dict literal was an unbounded dynamic reference reachable from
   every test. Dict-literal keys now bound loop variables, and a traversal
   order bug that ignored function-local literal assignments placed before
   their loop was fixed.

## Re-measurements

Selection-rule changes re-run on every measurement in this file. Recall
stayed at 100 % in each; where a number moved, the affected table above has
been re-stated from the re-run:

* `import_module` recognised under any import spelling, and relative
  names resolved against a known `package`: toolz's
  `create_signature_registry` imports through `from importlib import
  import_module`, which had been taken for an ordinary external
  reference, so the tests-only commit 80ddcb3 went from 4 to 36 selected
  (the toolz table is re-stated; recall 100 % before and after); hatch
  248141c went from 268 to 253 selected (recall 100 %; the 78 template
  tests on the release rows are now selected through the templates rather
  than as a dynamic reference); every other recorded commit planned identically;
* literal module constants are inert at import (a narrowing): only
  boltons moved (38 % to 42 % mean savings over its five commits,
  re-validated at recall 100 %); hatch's release rows are unaffected,
  since they select everything through a lazy-module `__getattr__`;
* the third batch's rules (classes named in annotations escape, doctests
  and imported tests are discovered, decorators, defaults and class
  bodies are import-time code of their module): over the 171 recorded
  commits mean selection moved from 69.7 % to 70.0 %; the five
  repositories that moved (cattrs, opentelemetry, starlette, tenacity,
  typer) were re-validated at recall 100 %; the cattrs, opentelemetry and
  starlette figures are re-stated, while tenacity and typer gained two
  tests on one or two commits, which leaves their rounded savings (20 %,
  35 %) unchanged;
  pytest's own repository gains one always-selected text doctest target
  per commit (its rows were already select-all);
* inert `def` statements and annotation-only changes no longer count as
  running at import (a narrowing): four commits moved (click e1fd594
  538 to 67, one each in structlog, cattrs and trio) and those
  repositories were re-validated at recall 100 %;
* the import-time rule (changes that run at import reach every
  transitive importer; adopted after measuring it, see design.md): every
  validated repository whose selections moved was re-validated, 23 in
  all, each at recall 100 % with no outcome miss (jinja has no ground
  truth); the tables and per-repository savings above are from those
  runs, with the previous savings stated beside them. The httpx and trio
  re-import tests are now selected. flask, pydantic, packaging, hatch and
  pytest did not move;
* the recall-validation fixes (special methods, every name of an
  unresolved chain, symlinked directories, runner dependencies; see
  "Recall validation beyond the corpora"): every recorded commit was
  re-planned; toolz, click, attrs, pipx, opentelemetry and hatch moved and
  were re-validated, all at recall 100 % (tables re-stated; opentelemetry
  reported two outcome flips of one flaky timing test). hatch returned to
  select-all through a lazy-module `__getattr__` that calls
  `import_module`;
* instance-attribute tracking, with call sites for constructions and
  `super()` calls, rebound parameters, `getattr`-returned functions and
  classes as escapes: every recorded commit of every corpus planned
  identically before and after except hatch, which moved from 0 % to 61 %
  mean savings with recall still 100 % (its table above is from the
  re-run);
* override-aware dispatch for `self`/`cls` lookups, including mixin and
  class-attribute overrides (commits 4722191 and its review follow-up);
* module-level variables as symbols with writer edges and enclosing-scope
  defaults (commits ab49129 through cca7217): toolz, structlog and
  pytest-mock unchanged; click moved from 65 % to 66 % savings and 77 % to
  80 % precision and its table above is from the re-run. Between the first
  and last of those commits toolz temporarily lost one affected test
  (recall 93 %), which is what drove the follow-ups;
* base-side coverage attribution (commit 6836992, a measurement change,
  not a selection change): every table in this file was re-run under the
  new definition. toolz (15 of 15 affected), click (441 of 441),
  pytest-mock (5 of 5) and attrs (1 869 of 1 869) are identical row by
  row; structlog gained one affected test (78 of 78, "Better bankruptcy"
  87 % to 88 % precision; a test that executed a symbol deleted in that
  commit) and its table is re-stated; the pytest pair re-validated
  identically (3 485 of 3 486 selected, 1 490 of 1 490 affected, 43 %
  precision) and the six-pair pytest corpus reproduced its 4 960 affected
  tests, 24 % pooled precision and 0 % to 53 % per commit, with 16 outcome
  changes instead of 17 (one timing-sensitive test flipped differently
  under the tracer this time; recall was 100 % both times); the pipx
  corpus re-ran identically with the cache copied per checkout instead of
  linked. The toolz re-run also showed that the
  toolz table had not been re-stated after module-level variables became
  symbols, contrary to the note below: d287360 now selects 33 (was 37),
  a1e25cb 40 (was 43), 55ce42d 35 (was 38) and d2eba03 37 (was 22: the
  `signatures` registry's writer edge now leads every `memoize`/`curry`
  user to the dynamic registry builder, see the toolz bullets). The
  table above is from the re-run;
* prefix-bounded dynamic names (commit 042f611): toolz, click, pytest-mock
  and attrs unchanged (attrs' lazy loader had already stopped being a seed
  once module-level variables became symbols); the pytest pair re-validated
  identically (3 485 of 3 486 selected, 1 490 of 1 490 affected, 43 %
  precision); structlog moved from 94 % to 95 % savings and 91 % to 92 %
  precision (its "Better bankruptcy" row went from 49 to 48 selected; the
  table above is from the re-run); the diffcone-itself section above was
  re-measured over its last six commits.

## Selection census (42 repositories, planning only)

The corpora above validate recall but cover nine repositories, and the
last two rules (instance attributes, relative dynamic imports) were built
around one of them. `scripts/census.py` asks the wider question of what
makes plans broad: it plans (never runs) the last eight first-parent
commits that change `.py` files in each of 42 popular pytest projects,
332 plans in 3 minutes with six processes (median 1.3 s per plan, 21 s at
most). Nothing is installed; roots are `src` and `.` for a `src`
layout and `.` otherwise, never tuned per project. Reproduce with:

```bash
uv run python scripts/census.py run --work /tmp/census -o census.json --jobs 6
uv run python scripts/census.py report census.json
```

A plan explains each selected test by the first path its search finds,
so causes come from counterfactual plans over the same indexes: a test
still selected with every unresolved reference removed has a resolved
**dependency**; one lost only when dynamic references are removed is
caused by a **dynamic reference**, one lost only when name matches are
removed by a **name match**, one lost only when both are removed by
**either**; **unknown fixture** and **degraded** (an analysis error
forcing select-all) come from the plan's fallbacks. Nothing here says
whether a selection was needed; a conservative selection is an upper
bound on waste, and recall is measured only by the corpora above.

| repository | modules | commits | mean selected | dependency | dynamic | name match | either | unknown fixture | degraded plans |
|---|---|---|---|---|---|---|---|---|---|
| pallets/flask | 83 | 8 | 62 % | 44 % | 51 % | 0 % | 5 % | 0 % | 0 |
| pallets/jinja | 60 | 8 | 96 % | 37 % | 49 % | 0 % | 13 % | 0 % | 0 |
| pallets/werkzeug | 131 | 8 | 88 % | 40 % | 1 % | 46 % | 13 % | 0 % | 0 |
| pallets/itsdangerous | 15 | 8 | 42 % | 41 % | 0 % | 16 % | 0 % | 43 % | 0 |
| encode/httpx | 60 | 8 | 39 % | 44 % | 0 % | 56 % | 0 % | 0 % | 0 |
| encode/starlette | 84 | 8 | 37 % | 51 % | 0 % | 49 % | 0 % | 0 % | 0 |
| encode/uvicorn | 84 | 8 | 91 % | 48 % | 17 % | 7 % | 27 % | 1 % | 0 |
| psf/requests | 37 | 8 | 54 % | 76 % | 0 % | 21 % | 0 % | 2 % | 0 |
| urllib3/urllib3 | 81 | 8 | 39 % | 7 % | 7 % | 31 % | 48 % | 8 % | 0 |
| Textualize/rich | 192 | 8 | 94 % | 32 % | 36 % | 2 % | 30 % | 0 % | 0 |
| fastapi/typer | 637 | 8 | 69 % | 34 % | 11 % | 29 % | 26 % | 0 % | 0 |
| fastapi/fastapi | 1138 | 6 | 16 % | 0 % | 2 % | 97 % | 0 % | 1 % | 0 |
| pydantic/pydantic | 286 | 8 | 75 % | 65 % | 0 % | 0 % | 34 % | 0 % | 0 |
| marshmallow-code/marshmallow | 38 | 8 | 52 % | 4 % | 2 % | 27 % | 67 % | 0 % | 0 |
| python-attrs/cattrs | 122 | 8 | 64 % | 71 % | 23 % | 0 % | 6 % | 0 % | 0 |
| jd/tenacity | 20 | 8 | 44 % | 59 % | 0 % | 41 % | 0 % | 0 % | 0 |
| theskumar/python-dotenv | 20 | 8 | 42 % | 26 % | 33 % | 10 % | 32 % | 0 % | 0 |
| pytest-dev/pluggy | 27 | 8 | 47 % | 44 % | 4 % | 19 % | 33 % | 0 % | 0 |
| pytest-dev/pytest-xdist | 32 | 8 | 66 % | 40 % | 1 % | 6 % | 53 % | 1 % | 0 |
| pytest-dev/pytest-asyncio | 51 | 6 | 2 % | 79 % | 21 % | 0 % | 0 % | 0 % | 0 |
| pypa/pip | 638 | 8 | 100 % | 0 % | 0 % | 0 % | 0 % | 0 % | 8 |
| pypa/packaging | 74 | 8 | 50 % | 89 % | 8 % | 1 % | 1 % | 0 % | 0 |
| pypa/build | 30 | 8 | 83 % | 26 % | 51 % | 0 % | 9 % | 14 % | 0 |
| pypa/twine | 33 | 8 | 28 % | 35 % | 0 % | 62 % | 0 % | 3 % | 0 |
| pypa/virtualenv | 165 | 8 | 100 % | 1 % | 0 % | 48 % | 47 % | 4 % | 0 |
| tox-dev/tox | 262 | 8 | 100 % | 50 % | 43 % | 0 % | 7 % | 0 % | 0 |
| pre-commit/pre-commit | 134 | 8 | 24 % | 36 % | 0 % | 64 % | 0 % | 0 % | 0 |
| PyCQA/isort | 108 | 8 | 33 % | 79 % | 18 % | 0 % | 3 % | 0 % | 0 |
| PyCQA/flake8 | 73 | 8 | 38 % | 20 % | 52 % | 15 % | 13 % | 0 % | 0 |
| nedbat/coveragepy | 166 | 8 | 97 % | 42 % | 12 % | 0 % | 46 % | 0 % | 0 |
| arrow-py/arrow | 21 | 8 | 32 % | 30 % | 8 % | 38 % | 24 % | 0 % | 0 |
| dateutil/dateutil | 38 | 8 | 5 % | 1 % | 28 % | 22 % | 49 % | 0 % | 0 |
| more-itertools/more-itertools | 7 | 8 | 2 % | 58 % | 42 % | 0 % | 0 % | 0 % | 0 |
| mahmoud/boltons | 65 | 8 | 14 % | 10 % | 15 % | 50 % | 25 % | 0 % | 0 |
| python-poetry/poetry | 423 | 8 | 78 % | 48 % | 0 % | 1 % | 41 % | 10 % | 0 |
| psf/black | 338 | 8 | 100 % | 0 % | 0 % | 0 % | 0 % | 0 % | 8 |
| sqlalchemy/alembic | 120 | 8 | 69 % | 18 % | 52 % | 0 % | 20 % | 9 % | 0 |
| networkx/networkx | 688 | 8 | 88 % | 2 % | 85 % | 0 % | 14 % | 0 % | 0 |
| scrapy/scrapy | 504 | 8 | 100 % | 63 % | 25 % | 0 % | 12 % | 1 % | 0 |
| agronholm/anyio | 79 | 8 | 50 % | 2 % | 0 % | 97 % | 0 % | 0 % | 0 |
| python-trio/trio | 149 | 8 | 71 % | 14 % | 45 % | 2 % | 32 % | 7 % | 0 |
| pygments/pygments | 402 | 8 | 100 % | 0 % | 0 % | 0 % | 0 % | 0 % | 8 |

The table is the re-run after the fixes described below (repeated class
definitions, package bindings that shadow a submodule, class creation
hooks). Across all 189 653 selections: dependency 34 %, dynamic
reference 27 %, name match 9 %, either 18 %, unknown fixture 1 %,
analysis error 11 %. The first run, before those fixes, had 196 664
selections: dependency 22 %, dynamic reference 23 %, name match 7 %,
either 14 %, unknown fixture 1 %, analysis error 33 %.

**Analysis errors were the largest cause.** Seven repositories (17 %)
selected every test on every commit in the first run, for three reasons:

* a class defined more than once in one module (`if`/`else`
  definitions in anyio's `to_interpreter`, black's test-case data
  files): the class symbols are merged but each body's methods are
  indexed separately, so the second `__init__` collides. A diffcone bug,
  since fixed: anyio re-planned without errors, its mean selection going
  from 100 % to 50 %; black still degrades on its deliberately invalid
  test-case files (the third reason);
* a package `__init__` that binds a name which is also a submodule
  (`tenacity.retry`, `pip._internal.main`, `poetry.layouts.layout`,
  scrapy's `tests.test_utils_misc.test_walk_modules`): legal Python
  (the binding executed last wins), reported as an identity collision.
  Since fixed (the binding is `pkg.__init__.retry`): tenacity now selects
  44 % on average, poetry 78 %, scrapy still 100 % but through
  dependencies; pip then degrades on a test data file that is not UTF-8
  (the third reason);
* files that do not parse (pygments' Python 2 example file under
  `tests/examplefiles`, black's invalid-syntax test cases, pip's
  non-UTF-8 test data file), which pytest never imports. Not planned:
  any unparseable file under a source root forces select-all by design
  (design.md, "Known gaps"); narrowing `--source-root` so such data
  files fall outside it is the remedy.

**Dynamic references** by construct (a seed with several uses splits its
selections between them; "present" counts repositories whose index has
the construct at all):

| construct | selections caused | repositories (selections) | repositories (present) |
|---|---|---|---|
| `getattr` with a loop variable over a non-literal | 7 023 | 18 | 30 |
| `getattr` with a parameter (call sites unbounded or escaping) | 1 696 | 17 | 31 |
| `getattr` with a local variable | 3 979 | 11 | 25 |
| `import_module` with a local variable | 5 030 | 8 | 10 |
| `__import__` with a parameter | 1 291 | 4 | 6 |
| `globals()` | 730 | 6 | 16 |
| `getattr` with a `self` attribute | 28 724 | 1 | 11 |
| `vars()`, `exec`, `eval` (together) | 209 | 5 to 7 each | 10 to 17 each |

The `self`-attribute row is one seed in networkx
(`_dispatchable._call_with_backend`, a backend dispatcher whose name is
not a constructor literal): instance-attribute tracking (built for
hatch) bounds none of the census's cases. The widespread constructs are
loop variables and parameters, causing selections in 18 and 17 of 42
repositories. Single seeds dominate where dynamic references are large:
jinja's `utils.import_string` and `filters.do_round`, rich's
`repr.auto`, tox's `Pep517VirtualEnvFrontend.__init__`, flask's
`helpers.get_root_path` (`__import__` of a parameter).

**Name matches** dominate in six repositories, through a few
attributes on untyped receivers: `app` (fastapi 2 231, starlette 627),
`callback` (typer 1 150), anyio's task-group methods
(`get_current_task`, `aclose`, `send`, `wait`, about 850 each, visible
once anyio stopped degrading), `load_cert_chain` (urllib3 758), `get`,
`headers`, `update`.

**Unknown fixtures** cause 1 % of selections (poetry's repository
fixtures once poetry stopped degrading, urllib3's `runtime`, trio's
`autojump_clock` and `mock_clock`, build's package fixtures):
discovery completeness matters far less than the roadmap order implied.

**A soundness gap found on the way.** In flask, a change to
`flask.views.http_method_funcs` is reached only through
`MethodView.__init_subclass__`, which runs whenever a test defines a
subclass; diffcone did not model subclassing as calling
`__init_subclass__`, so only the dynamic fallback selected those tests.
Class creation now depends on the bases' `__init_subclass__` and a
metaclass's `__new__`/`__init__` (design.md): on 2a8a38b the 12 tests
that subclass `MethodView` are reached through a dependency path.

## What the dynamic references actually are

The census ranks dynamic references first among the causes of selection
(27 % of selections alone), but "dynamic reference" is a fallback, not a
construct. Three measurements over the 42 census clones at HEAD (vendored
`.venv`, `build` and `.tox` trees excluded) say what the constructs are
and what a rule could bound.

**The sites.** 768 `getattr` calls pass a name the indexer cannot bound.
What the receiver is decides whether any rule could:

| receiver of the unbounded `getattr` | sites | share |
|---|---|---|
| a parameter, a local, a call result, a chain (no type known) | 568 | 74 % |
| `self` / `cls` (the class is known) | 128 | 16 % |
| a name imported from a module in the source roots | 36 | 4 % |
| a name imported from the standard library or a dependency | 35 | 4 % |

324 of those names are loop variables, and what they iterate is mostly
not a table a static rule can read:

| iterable of the loop variable | sites | share |
|---|---|---|
| a name that is not a literal display | 102 | 31 % |
| an attribute (`self.x`, `mod.X`) | 54 | 16 % |
| `.items()` / `.keys()` of a non-literal | 43 | 13 % |
| `dir()` | 34 | 10 % |
| a literal display (already bounded) | 38 | 12 % |
| `__slots__`, `__all__`, `__dict__` | 25 | 8 % |
| a function result, a subscript, `zip()`, a built string | 28 | 9 % |

**Why a parameter is unbounded.** Instrumenting `_param_values` over the
42 repositories records the condition that gave up, 314 times:

| condition | cases | share |
|---|---|---|
| no call site in scope: the function is a public entry point | 180 | 57 % |
| a call site passes a non-literal | 74 | 24 % |
| the function's own name is itself a dynamic candidate | 48 | 15 % |
| the function escapes as a value | 6 | 2 % |
| a call site passes `*args`/`**kwargs` | 4 | 1 % |
| its class is constructed unseen | 2 | 1 % |

**What causes selections.** The first two tables count sites; only the
seeds the census attributes selections to matter. Classifying those
34 866 selections by the shape of their seed:

| seed shape | selections | share | repositories |
|---|---|---|---|
| `getattr` on a receiver with no known type | 24 897 | 71 % | 14 |
| `import_module` / `__import__` (no receiver at all) | 7 502 | 22 % | 12 |
| `getattr` on an imported module or name | 1 754 | 5 % | 6 |
| `getattr` on `self`/`cls`, or on an instance attribute | 152 | 0.5 % | 4 |

And they concentrate: the ten heaviest seeds are networkx's
`_dispatchable._call_with_backend` (19 053 selections, 55 % of the whole
census on its own), scrapy's `load_object`, anyio's
`install_lazy_importer`, jinja2's `import_string` and `do_round`, tox's
`_load_plugin`, flask's `get_root_path`, pydantic's two `import_string`s
and uvicorn's `import_from_string`.

**The conclusion.** The widespread construct is not a loop or a parameter
shape: it is the by-name import utility (`import_string`, `load_object`,
`import_from_string`, `_load_plugin`) and the plugin dispatcher, called
with names that come from a user's configuration rather than from the
code. Their parameters are unbounded for the reason the second table
leads with -- they are public entry points, so no set of in-repository
call sites bounds them. Bounding the 71 % needs the receiver's type,
which is out of scope by design (AGENTS.md). The only shapes a rule could
bound are the last two rows, 5.5 % of dynamic-caused selections, and even
that bound is unavailable in most repositories: it holds only while
nothing may have attached an unseen attribute to the receiver, and a
`setattr` whose name the indexer cannot bound (`monkeypatch.setattr` in a
test suite) exists in 34 of the 44 repositories measured. The roadmap
records the shape and leaves it unbuilt.

## A degraded census repository, explained

pip degrades on every census commit, so its 100 % selection is an
analysis error rather than a judgement. The error is one file:
`tests/data/packages/SetupPyLatin1/setup.py`, which declares `# -*-
coding: latin-1 -*-` and is deliberately not UTF-8. Python reads the
declared encoding before it reads the source, and diffcone did not; it
now does (design.md). With that file analysed, the same commit plans
`complete` and selects 6 of 1939 targets instead of all of them. The two
repositories that still degrade hold files that are deliberately
unparseable, as data for the tool under test: pygments'
`tests/examplefiles/python/unicodedoc.py` (a Python 2 `ur""` string) and
twelve of black's `tests/data/cases` (future syntax, `async` as an
identifier, an invalid header). Nothing imports them and pytest collects
none of them, so only the census's blanket `--source-root .` puts them in
scope; a project would exclude them. Re-scoping the fallback to the
targets that actually depend on an unparseable file was considered and
dropped; this is the evidence for how often it would matter -- 2
repositories of 44, both of them tools whose fixtures are broken Python
on purpose.

## Recall validation beyond the corpora (10 census repositories)

The governing rule is that a plan may run more tests than needed but must
never miss one. The census plans but never checks, so ten census
repositories unlike the recorded corpora were validated with
`corpus --coverage` (outcome changes and per-test coverage at both
snapshots), each in a Python 3.13 venv with the project editable and its
test dependencies, source roots `src` and `.` for a `src` layout and `.`
otherwise. The first pass found real misses in three of them; each was
turned into a scenario and a fix, and every repository was then re-run
on the final code:

| repository (HEAD) | validated commits | first pass | final: recall | mean savings (import-time rule) |
|---|---|---|---|---|
| flask (d73fa1c) | 5 | 100 % | 100 % (354 of 354) | 40 % |
| jinja (5ef7011) | 5 | n/a | n/a (0 affected) | 0 % |
| rich (9d8f9a3) | 18 | (id mismatch) | 100 % (1 480 of 1 480) | 22 % |
| marshmallow (7f0792b) | 9 | 100 % | 100 % (742 of 742) | 33 % |
| cattrs (5bf7c97) | 4 | 100 % | 100 % (51 of 51) | 46 % |
| tenacity (3e58094) | 5 | **73 %** | 100 % (509 of 509) | 20 % |
| pluggy (9836e54) | 4 | **79 %** | 100 % (229 of 229) | 24 % |
| packaging (10590c1) | 4 | 100 % | 100 % (273 of 273) | 47 % |
| boltons (961dcff) | 5 | 100 % | 100 % (37 of 37) | 42 % |
| pydantic (915896d) | 3 | **1 missed** | 100 % (4 923 of 4 923) | 33 % |

No run had an outcome miss. The misses the first pass found, all
coverage misses (a test executed a changed symbol and was not selected):

* **tenacity, 135 tests.** `@retry` returns a wrapper that runs
  `copy = self.copy(); copy(fn, ...)`, calling a `Retrying` instance, and
  the retry strategies are instances called as `self.stop(state)`.
  Calling an instance runs `__call__`, which no call names; a probe showed
  the same for `==` (`__eq__`) and `len()` (`__len__`). Fix: a class
  depends on its special methods (design.md). Tracing that fix's widening
  found a second gap: every name of an unresolved chain after the first
  was dropped (`o.a.b()` recorded only `a`); every name is now recorded.
* **pluggy, 49 tests.** pytest itself calls pluggy's hook machinery during
  every test (with the checkout first on the path), so a change to
  `HookCaller._verify_all_args_are_provided` ran under tests that never
  reference it. Fix: the `runner_dependency` fallback.
* **pydantic, 1 test (of 119 unseen files).** `tests/pydantic_core` is a
  tracked symlink to `../pydantic-core/tests`; pytest collects through it,
  the snapshot readers skipped it. Fix: in-repository symlinks are
  expanded. pydantic went from 2 849 to 4 537 targets.

packaging passed the first pass (76 % savings) but pays for the pluggy
fix: pytest imports `packaging.version` and `packaging.requirements`, so
the two commits that touch their import closure now select every test
(`runner_dependency`), 47 % mean savings. That is the rule's intent: a
broken `packaging.version` breaks pytest's own startup.

Not misses: rich's first pass reported 0 of 27 because the command passed
`tests` and rich has no root pytest configuration, so pytest took `tests/`
as its rootdir and its node ids lost the prefix (`--rootdir .` fixes the
measurement, not diffcone); jinja's commits change only import-time code
(`__init__`, a regex constant, `docs/conf.py`), which coverage cannot
attribute, so there is no ground truth. dateutil was dropped (its test
dependencies require the released package). Setups that needed care:
pluggy's shallow clone has no tags, so `SETUPTOOLS_SCM_PRETEND_VERSION`
avoids a resolver fallback to pytest 3; cattrs needs its serialisation
extras and `tests` on the command line (its `bench/` needs more);
pydantic needs its `testing-extra` group plus hypothesis, dirty-equals,
pytest-mock, pytest-benchmark, inline-snapshot, jsonschema,
pytest-examples and pytest-timeout; tenacity's untracked `_version.py`
is copied by `--setup-command`. The command for each (roots as above):

```bash
diffcone corpus --repo . --range HEAD~60..HEAD --discover pytest \
  --source-root src --source-root . \
  --command "$PWD/.venv/bin/python -m pytest -p no:cacheprovider -q" \
  --coverage --max 6 --jobs 2
```

with `--range HEAD~75..HEAD --max 30` for rich and marshmallow,
`--rootdir . tests` appended to rich's pytest command, `tests` to
cattrs' and pydantic's, and `--setup-command "cp $PWD/tenacity/_version.py
tenacity/_version.py"` for tenacity.

## Recall validation, second batch (8 more repositories)

Eight more census repositories, chosen to differ from the first
nineteen (WSGI, ASGI and HTTP clients, a type-hint CLI, two async
frameworks with their own pytest plugins, pure functions, datetimes),
validated the same way over up to 12 of their last 75 commits:

| repository (HEAD) | validated commits | recall | mean savings (before the import-time rule, in parentheses) | notes |
|---|---|---|---|---|
| werkzeug (a7cad31) | 10 | 100 % (867 of 867) | 0 % (4 %) | |
| starlette (57de5fa) | 8 | 100 % (1 163 of 1 163) | 38 % (39 %) | 64 % recall before the external-base fix |
| httpx (b5addb6) | 3 | 100 % (2 of 2) | 67 % (100 %) | the re-import test, missed before the import-time rule |
| typer (a80f6e5) | 3 | 100 % (6 of 6) | 35 % (41 %) | |
| anyio (f7df682) | 7 | 100 % (828 of 828) | 14 % (43 %) | |
| trio (50b9825) | 6 | 100 % (433 of 433) | 15 % (20 %) | the re-import test, missed before the import-time rule; REPL tests deselected |
| more-itertools (1da45ae) | 8 | 100 % (87 of 87) | 46 % (97 %) | |
| arrow (2224255) | 5 | 100 % (441 of 441) | 46 % (53 %) | 39 outcome changes, none missed |

No outcome was missed. werkzeug, more-itertools, arrow, typer and anyio
ran before the external-base rule, which only adds selections.

* **starlette, 421 tests: a real miss, fixed.** `TestClient` is an
  `httpx.Client` around `_TestClientTransport(httpx.BaseTransport)`;
  httpx calls `handle_request` for every request and nothing in the
  source roots does. Classes with external bases now depend on every
  method they define (design.md).
* **httpx and trio: tests that re-import the package.**
  `test_httpcore_lazy_loading` and `test_trio_import` delete the package
  from `sys.modules` and import it again inside the test, so coverage
  credits them with import-time code: an `__all__` assignment in httpx;
  the `trio._sync` and `trio.socket` module bodies, a `deprecated` helper
  and `__version__` in trio. By design a module's import-time code
  reaches only code that reads its state (design.md, module bodies);
  these tests read none of it. trio's other credited symbols
  (`WorkerThread._handle_job`, `MemorySendChannel.send_nowait`,
  `KqueueIOManager.notify_closing`) ran on background threads while the
  test's coverage context was active: coverage contexts are process-wide.
* Setup: trio's REPL tests start `python -m trio` subprocesses that wait
  on stdin under the harness, so trio ran with `-k "not repl"` and
  httpx and trio with pytest-timeout (`--timeout=120`); an httpx run
  without the timeout hung.

## Recall validation, third batch: annotation-driven construction

The review's open question (frameworks that build classes from
annotations) was tested on two repositories that do it:

| repository (HEAD) | validated commits | first pass | final: recall | mean savings |
|---|---|---|---|---|
| injector (d8f707d) | 4 | **93 %** (147 of 158) | 100 % (158 of 158) | 0 % |
| fastapi (50113da) | 15 | **99.6 %** (1 707 of 1 713) | 100 % (1 713 of 1 713) | 47 % |

Each miss became a scenario and a fix:

* **Classes named in annotations** (a probe written from injector's
  documented use): injector builds `Service()` from `App.__init__`'s
  annotation with its default argument, which no visible call passes.
  Annotations no longer exempt a class from escaping.
* **Doctests** (injector, 11 missed): `--doctest-modules
  --doctest-glob=*.md` collects docstring examples and README.md, which
  discovery did not; they are now targets (design.md, "Doctests").
  Discovery matches pytest's 145 collected items exactly.
* **Imported tests** (fastapi, 5 of 6 missed): tutorial tests import
  `test_read_main` from `docs_src`, and pytest collects it in the
  importing module; discovery now does too.
* **Import-time code in decorators** (fastapi, the 6th):
  `@app.get("/")` builds the route handler while `docs_src` is imported,
  and a test reaches it through `runpy.run_module`; decorators, defaults
  and class statements now count as the module's import-time code, and
  `runpy.run_module` as a dynamic import.

Setup: fastapi from its `uv.lock` (`uv sync --frozen --all-extras
--group tests`; a newer anyio turns a deprecation warning into a
collection error), with pytest-timeout; most fastapi commits touch only
docs and translations, so the range is its last 15 Python-touching
commits (`--range 5f25505~1..HEAD`). injector's own `addopts` run
pytest-cov with a coverage floor, which the validation tolerates.

## Recall validation, fourth batch: frameworks with their own conventions

Three projects of kinds not covered before: a pytest plugin for Django, a
Django framework tested with pytest, and a plugin-heavy documentation
tool. django-debug-toolbar was dropped: it runs its suite through
Django's own test runner, which `validate` cannot drive.

| repository (HEAD) | validated commits | first pass | final: recall | mean savings |
|---|---|---|---|---|
| pytest-django (67f9798) | 4 | **35 %** (26 of 75) | 100 % (75 of 75) | 74 % |
| django-rest-framework (fe26559) | 5 | 100 % (591 of 591) | 100 % | 21 % |
| sphinx (b04a210) | 7 | 100 % (258 of 258) | 100 % | 0 % |

* **pytest-django, 49 tests.** Its autouse `_django_db_marker` reaches
  `_django_db_helper` through `request.getfixturevalue(...)` with a
  literal, which discovery did not model; the helper went from 23 to 209
  of 210 targets' dependencies, and mean savings from 96 % to 74 %.
* **sphinx selects every test on every commit.** The first seed found was
  `_cli._load_subcommand`'s `import_module(_COMMANDS[command_name])`,
  unbounded because only what *iterating* a dict literal yields was bound;
  indexing one now yields its values too (design.md), which removes that
  seed. It does not move sphinx: eleven further dynamic seeds are genuine
  runtime-driven imports (autodoc's `_import_module`, the extension
  registry's `load_extension`, `util._importer.import_object`, the
  pygments style and search-language lookups, `pycode`'s analyzer), and
  any one of them selects everything. A tool that imports what its
  documents name is the worst case for a static bound, not a rule gap.
  Its suite also takes 75 s, so the corpus took 20 minutes.
* Django's settings-driven imports and REST framework's serializer
  metaclasses raised no miss.

Found while tracing sphinx's dynamic import, and fixed: a container that
any module mutates in place (`REGISTRY = {}` filled by `REGISTRY[k] = v`)
was still read as its literal, so names drawn from it were bounded to
nothing at all; such a variable is now unbounded everywhere (design.md).
No recorded commit of the validated repositories changed. django-rest-
framework and sphinx were validated before that fix and before the
`getfixturevalue` one, both of which only add dependencies. The rule that
bounds what indexing a literal table yields changed no selection either,
on any of the 171 recorded commits: it is kept because it is sound and
removes a whole shape of seed, not because it was measured to pay.

## Recall validation, fifth batch: a vendored installer and a plugin-collected suite

Two projects of kinds not covered before: one that vendors its
dependencies inside itself, and one whose suite is collected by another
project's pytest plugin.

| repository (HEAD) | validated commits | outcome misses | coverage recall | mean savings |
|---|---|---|---|---|
| pip (892d13b) | 3 | 0 | 100 % (18 of 18) | 0 % |
| alembic (b42ebe1) | 6 | 3 | **1 % (20 of 1825)** | 28 % |

* **pip could not be planned at all before this batch.** Every commit
  degraded on `tests/data/packages/SetupPyLatin1/setup.py`, a file that
  declares `# -*- coding: latin-1 -*-`; diffcone decoded every file as
  UTF-8 and one failure forces select-all. Files are now read in the
  encoding they declare (design.md).
* **pip then selects everything anyway**, soundly: `pip._vendor.vendored`
  calls `__import__(modulename, globals(), locals())`, and every test
  imports `pip`. A runtime-named import may name any module, so any
  change fires it -- including a change to a test helper, which is what
  the three validated commits are. The reason text used to explain this
  with the other fallback's sentence ("reachable from its module's
  imports"); the two rules now say which fired.
* **alembic is a recall failure, and not one the analysis could have
  avoided.** Its suite is 2387 tests; pytest's own collection rules find
  23 of them, because SQLAlchemy's testing plugin collects classes named
  `<Name>Test` (and parametrises the class per backend:
  `BatchNamingConventionTest_sqlite+pysqlite_3_47_1`). Everything the
  corpus reports as missed is a test that was never a target. Discovery
  cannot reproduce another project's plugin without running it, so it now
  reports what it leaves behind: 175 `uncollected_test_class` notes for
  alembic, against 23 targets. A plan whose target list is not the suite
  now says so; targets a plugin creates have to come from a manifest.

## Recall validation, sixth batch: inherited and re-imported test suites

Two more kinds: a large CLI whose plugins arrive through entry points, and
a scientific library whose test classes are shared by inheritance. Both
started with a recall failure, and in both cases every target was already
selected -- the missed tests were not targets at all, so the failure was
in discovery, not in the analysis.

| repository (HEAD) | validated commits | first pass | after the fix | mean savings |
|---|---|---|---|---|
| poetry (94b6e35) | 1 | 96 % (139 of 145) | 100 % (145 of 145) | 0 % |
| networkx (bd45cfe) | 3 | 88 % (3054 of 3466) | 100 % (3466 of 3466) | 0 % |

* **poetry, 6 tests.** `tests/console/commands/test_sync.py` is
  `from tests.console.commands.test_install import *` with a different
  `command` fixture: the install suite, re-run for `sync`. Discovery
  skipped star imports entirely. The star now binds what `__all__` lists,
  or every name the module defines that does not start with an
  underscore, minus what the importing module defines itself (poetry
  redefines one test to skip it). 1546 targets became 1567.
* **networkx, 412 tests.** `TestDiGraph(BaseGraphTester)` inherits its
  tests from another module; discovery followed base classes only within
  one module and reported the rest (`unknown_base_class`). A base
  imported from a module in the source roots is now followed, with its
  own bases resolved in the module that defines it. 4844 targets and 29
  notes became 5529 and none.
* **Neither repository saves anything**, for reasons already recorded:
  networkx's `_dispatchable._call_with_backend` is the census's single
  heaviest dynamic seed, and poetry's commit changed a symbol every test
  reaches. The value of both was the recall check.
* No recorded corpus changes its target count under either fix, so the
  numbers in the sections above stand unmeasured again.

## Recall validation, seventh batch: collection by a plugin's own rules

Two projects whose suites are not what pytest's documented rules alone
would collect.

| repository (HEAD) | validated commits | first pass | after the fixes | mean savings |
|---|---|---|---|---|
| scrapy (b6ac785) | 2 | 98 % (2678 of 2742) | 100 % (2741 of 2742) | 0 % |
| virtualenv (72e1906) | 3 | n/a (no test covers the changes) | same | 0 % |

* **scrapy, 64 tests, two causes.** `python_files` is
  `["test_*.py", "test_*/__init__.py"]`, and pytest matches those patterns
  against *absolute* paths, so a pattern with a separator behaves as if
  prefixed with `*/`; matching the repo-relative path alone missed
  `tests/test_settings/__init__.py` and its siblings (4575 targets became
  4663). The rest were `unittest.TestCase` subclasses reached through an
  intermediate base (`PickleLifoDiskQueueTest(t.LifoDiskQueueTest)`),
  which pytest's unittest plugin collects whatever they are called.
* **The one test still missed is now declared.** `docs/conftest.py` binds
  `pytest_collect_file` to a Sybil instance, which turns the `.rst`
  documentation into doctests; one of them executes changed code. Nothing
  static can enumerate what a plugin collects, so the plan reports
  `plugin_collects_files` and exits 3 rather than looking complete.
* **virtualenv needed `--setup-command`**, not a rule change:
  `src/virtualenv/version.py` is generated at build time, so a temporary
  checkout cannot import the package at all (the same shape as tenacity's
  `_version.py`). Its suite also shares an app-data cache between
  processes, so it is validated with `--jobs 1`. With both, three commits
  validate clean; all three change build metadata that no test covers, so
  coverage recall is `n/a` rather than a number.
* The `unittest`-subclass fix is the one with reach beyond these two
  repositories: it also added targets in django-rest-framework, dateutil
  and marshmallow. No recorded corpus changed its target count under any
  of these fixes.

## Recall validation, eighth batch: two suites that actually narrow

| repository (HEAD) | validated commits | outcome misses | coverage recall | mean savings |
|---|---|---|---|---|
| isort (131f4ad) | 4 | 0 | 100 % (401 of 401) | **75 %** |
| pytest-asyncio (01ff313) | 2 | 0 | 100 % (2 of 2) | **100 %** |

The first repositories since the third batch where the plan narrows
anything: isort selects 2 of 585 targets on three of its four commits,
pytest-asyncio 1 of 208 on both of its. Neither has a dynamic seed that
everything reaches, which is the whole difference.

* **pytest-asyncio had to be told how to name a directory.** Its
  `testpaths` include `docs` and its `python_files` include
  `*_example.py`, so pytest collects eleven files under
  `docs/how-to-guides` -- a directory whose name is not a Python
  identifier, so no plain source root can name it and diffcone dropped
  them silently. They are now reported (`unparsed_file`, incomplete
  discovery, exit 3) with the fix in the message: with
  `--source-root docs/how-to-guides=docs_howto`, 197 targets become 208
  and the plan is complete. The numbers above use that root.
* **isort needed nothing.** Its `--max 5` sample included three commits
  that touch only `__main__` handling and its test, which is exactly the
  shape the engine is for.
* pytest-asyncio's recent history is dependency bumps, and `--max` caps
  candidate commits *before* skipping the ones with no `.py` change, so
  the range had to be aimed at Python-touching commits by hand.

## Static discovery against real collection (29 repositories)

The recall batches found five discovery gaps in a row, each by running a
whole suite twice under coverage. `scripts/collection_check.py` does the
same check in seconds: it runs `pytest --collect-only` in a repository and
diffs the node ids against the targets discovery produces for the same
snapshot (parameter cases collapsed, since diffcone plans whole test
functions). It executes project code, so it is a script, not part of
planning.

Over all 48 census repositories, once each had an environment:

| result | repositories |
|---|---|
| every collected test is a target | **45** |
| tests collected that are not targets | 3, all of them declared |

* **alembic's 1587, scrapy's 881 and pygments' 850** are the cases the
  plan declares and exits 3 for: SQLAlchemy's plugin collecting
  `<Name>Test`; a base class in the external `queuelib` plus Sybil
  doctests in `docs/*.rst`; and pygments' `pytest_collect_file`, which
  makes a test of every file under `tests/examplefiles`. None can be
  enumerated without running the plugin.
* **Two repositories need a source root to be exact**, and say so without
  one: pytest-asyncio's `docs/how-to-guides` (a directory that cannot be a
  module name) and itsdangerous, whose test modules import each other as
  `test_itsdangerous.test_serializer`, which only `--source-root tests`
  names.
* **The sweep found one real gap**, in the two repositories that import
  test classes from their siblings: only the class's own methods became
  targets, so urllib3 missed 216 of 1299 tests and itsdangerous 96 of 133.
  An imported class now contributes its whole MRO, and both are exact.

The reverse direction -- targets pytest did not collect -- is
over-selection and safe, and it is small except where a suite is skipped
wholesale: packaging 427 and django-debug-toolbar 336 (which runs under
Django's own runner), then fastapi 46, networkx 40, pydantic 29, rich 26,
poetry 20, virtualenv 7, cattrs 4, pip 3, sphinx 2. They are tests that
this platform, Python version or optional dependency set skips.

This is the check to run first on a new repository: it is cheap, it needs
no commit range, and every discovery gap found by the batches above would
have shown up in it.

**ASV, the same way.** `--runner asv` asks ASV's own discovery step
(`python -m asv.benchmark discover`, run from the directory holding
`asv.conf.json`, as ASV runs it) and diffs the benchmark names. The three
census repositories with an ASV suite match exactly: networkx 59,
packaging 20, rich 32, none missing and none extra. networkx only matches
after `benchmark_dir` began resolving against its config's directory:
its `asv.conf.json` is in `benchmarks/`, so every benchmark had been named
`benchmarks.<module>...` instead of `<module>...` and no `--bench` regex
would have matched one. Benchmarks inherited from a base class are now
targets too, though no census suite uses inheritance, so that rule is
sound and tested but unmeasured in the wild.

## Are name-match selections worth their cost?

The census attributes 9 % of selections to name matches alone, and the
roadmap asked for a measurement before any rule.
`scripts/cause_precision.py` answers it: it takes the census's cause for
every selected target and the per-test coverage `validate --coverage`
already collects, and reports, per cause, how many selected tests actually
executed a changed symbol. Three Python-touching commits per repository:

| repository | dependency | name match | dynamic | either |
|---|---|---|---|---|
| rich | 32 of 684 (5 %) | -- | -- | 29 of 1329 (2 %) |
| typer | 6 of 934 (1 %) | 0 of 9 | 0 of 11 | 0 of 866 |
| starlette | 523 of 1059 (49 %) | 0 of 1 | -- | -- |
| werkzeug | 455 of 1702 (27 %) | -- | -- | -- |
| virtualenv | -- | 0 of 266 | -- | 0 of 487 |
| marshmallow | 1 of 1 | -- | -- | -- |

**A name-match rule would not pay.** Where the bucket exists at all it is
tiny -- typer 9 selections, starlette 1 -- and the large buckets are
`dependency` and `dynamic or name match`. The second of those is the
decisive one: it means the target is reachable *both* ways, so bounding
name matches would not deselect a single test in it. virtualenv is the one
repository where name matching alone causes real volume (266 selections),
and its three commits change build metadata that no test covers, so
nothing was worth selecting there by any cause -- a degenerate denominator
rather than evidence against name matching.

What the table does show is that the cost of a selection has little to do
with its cause: the same `dependency` bucket is worth 49 % in starlette,
27 % in werkzeug and 1 % in typer. That spread is the import-time rule
(design.md), the trade-off chosen deliberately after measuring, not an
unbounded fallback. The sample is small -- six repositories, three commits
each -- but it is consistent, and it is the reason roadmap item 1 closes
with no rule.

## What the caller-object rule cost

Bounding a `getattr` on an object a caller supplied by the seeding module's
imports was a miss (design.md, "Dynamic references"); removing that bound is
what the governing rule requires, and it is expensive. Re-planning the 171
recorded corpus commits, nothing is deselected anywhere and eleven
repositories select more:

| repository | commits | mean savings before | after |
|---|---|---|---|
| structlog | 3 | 87 % | **0 %** |
| starlette | 8 | 38 % | **0 %** |
| pluggy | 4 | 24 % | **0 %** |
| tenacity | 5 | 19 % | **0 %** |
| trio | 6 | 15 % | **0 %** |
| boltons | 5 | 42 % | 8 % |
| cattrs | 4 | 46 % | 25 % |
| marshmallow | 9 | 33 % | 22 % |
| attrs | 16 | 60 % | 56 % |
| arrow | 5 | 46 % | 44 % |
| packaging | 4 | 47 % | 44 % |

Five of them stopped narrowing at all: one helper that reads an attribute
off an object its caller passed is enough, and every test reaches it.

**That version is not what shipped.** Binding the read to the classes that
call sites name (design.md, "Attributes read off an object") returns 26 of
the 28 repositories to the savings in the first column -- structlog to 87 %,
starlette to 38 %, cattrs to 46 %, boltons to 42 %, attrs to 60 %, pluggy,
tenacity and trio to theirs -- while still catching the miss, now as an
ordinary dependency path rather than a fallback. Two repositories pay for
it: arrow 46 % to 12 % and typer 35 % to 33 %, both because their classes
are handed around widely. Nothing is deselected anywhere and the tables
above this section stand, except for those two.

That version's price was an accepted exception to the governing rule: a
class whose instances only ever come from a factory is never named at a call
site. Typing what a factory returns closed it, and cost nothing measurable --
the corpora are identical with and without it, because a factory's class is
almost always named somewhere else too. What remains is the return the
analysis cannot type at all.

A sound version of the same bound -- resolve the receiver back through call
sites, as a `getattr` *name* already is -- is implemented alongside
(`_receiver_classes`) and recovered nothing measurable: real receivers
arrive through several hops before anything names a class.

The last two rows of the table are the `new_target` rule instead, which
selects a discovered target the base snapshot did not have: a few dozen new
tests on commits that added tests, which is what it is for.

**Validated, not just re-planned.** The three repositories the rule affects
most were re-run with `corpus --coverage` under it: structlog 0 outcome
misses and 100 % coverage recall (77 of 77) at 87 % savings, tenacity 100 %
(507 of 507), pluggy 100 % (143 of 143) at 32 %. Binding the read to the
classes that reach it holds up against what the suites actually execute.

**What is left.** A return the analysis cannot type at all -- an object
built by a `classmethod`, or handed back through a chain of such calls.
Closing one means typing more receivers, not widening the fallback again,
which this section measured.

## Not yet exercised

* A corpus over a monorepo whose per-package test trees share module
  names and are collected in one `--import-mode=importlib` session.
  Per-root prefixes plan such trees together (the opentelemetry API and
  SDK trees above), but only planning, not validation, has been run over
  one.
* A corpus over more than one pytest session of the same repository.

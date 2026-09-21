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
| d287360 | Add frozendict signature | 33 / 186 | 82 % | 100 % | 3 % |
| 80ddcb3 | Add tests for get_in defaults, ... | 36 / 190 | 81 % | 100 % | 11 % |
| a1e25cb | Expose combined `__annotations__` on composed functions | 40 / 192 | 79 % | 100 % | 20 % |
| 55ce42d | Support Python 3.15 and refresh dev tooling | 35 / 192 | 82 % | n/a | 0 % |
| d2eba03 | Fix interpose([]) raising StopIteration | 37 / 193 | 81 % | 100 % | 5 % |
| 451af60 | Add pysentry-pre-commit | 0 / 193 | 100 % | n/a | n/a |

Totals: 7 outcome changes, 0 missed; recall 100 % (15 of 15); precision
8 %; mean savings 84 %.

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
| 2103e15 | Forward all user's parameters set in `PAGER` | 34 / 537 | 94 % | 100 % | 62 % |
| e1fd594 | Add support of `pathlib.Path` to `edit` | 11 / 538 | 98 % | 100 % | 80 % |
| 6aabf09 | Stable (a 30-file squash: `Option` restructured, tests reorganised) | 520 / 555 | 6 % | 100 % | 81 % |

Totals: 54 outcome changes, 0 missed; recall 100 % (441 of 441); precision
80 %; mean savings 66 %.

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
| e26945d | fix: PytestRemovedIn10Warning | 3 / 516 | 99 % | 100 % | 67 % |
| d8e321a | Use built-in product | 34 / 516 | 93 % | 100 % | 100 % |
| 73393f3 | Better bankruptcy | 48 / 517 | 91 % | 100 % | 88 % |

Totals: 1 outcome change, 0 missed; recall 100 % (78 of 78); precision
92 %; mean savings 95 %.

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
| a8bd0b1 | Honour resetall() arguments for non-callable mocks | 25 / 69 | 64 % | 100 % | 20 % |

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
precision 53 %; mean savings 67 %. The 16 validated commits (nine touch
only docstrings, `typing_tests/` examples outside the source roots, or
test-typing stubs, change no symbol with impact, and select nothing; they
count at 100 % savings):

| commit | subject | selected | savings | recall | precision |
|---|---|---|---|---|---|
| 6851ab5 | Defer imports on the cold import path | 655 / 655 | 0 % | 100 % | 83 % |
| 97f8d17 | Fix ClassVar forward reference detection | 551 / 656 | 16 % | 100 % | 15 % |
| 4b5b295 | Make on_setattr hooks accept generators | 555 / 667 | 17 % | 100 % | 62 % |
| 5aa76a4 | Add `ne` validator | 23 / 667 | 97 % | 100 % | 22 % |
| 9b98a73 | Drop Python 3.9 | 666 / 666 | 0 % | 100 % | 83 % |
| 3e01de4 | docs: fix markup (removes a `from . import` binding) | 551 / 666 | 17 % | 100 % | 1 % |
| f53fc54 | Stop evolve dunders from being modified | 551 / 667 | 17 % | 100 % | 63 % |
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
| b83f660 | fix(upgrade): don't claim 'latest version' for local-path installs | 461 / 698 | 34 % | 100 % | 9 % |
| 84eaad3 | fix(reinstall): propagate returned failures in reinstall-all | 462 / 699 | 34 % | 100 % | 2 % |

Totals: 0 outcome changes, 0 missed; recall 100 % (50 of 50); precision
5 %; mean savings 34 %. No test needed `--assume-external-fixture`. The
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
| 5aa2f8f | logs: add Enabled support to Logger API, SDK, and LogRecordProcessor | 565 / 827 | 32 % | 100 % | 14 % |
| 477ffd4 | opentelemetry-docker-tests: add Prometheus exporter docker tests | 0 / 827 | 100 % | n/a | n/a |
| ab22674 | fix(opentelemetry-sdk): keep synchronous gauge values across cumulative collections | 561 / 830 | 32 % | 100 % | 16 % |
| cfad5eb | Fix TraceState.update to only update already existing keys | 559 / 830 | 33 % | 100 % | 0 % |
| 34c5e5f | Added guard against negative value of max_value_len | 698 / 830 | 16 % | 100 % | 58 % |
| ee219ad | test(exporter-otlp-proto-grpc): relax timing delta ... | 0 / 830 | 100 % | n/a | n/a |
| 5843c4e | DOC(exporter-otlp-proto-http): clarify endpoint= kwarg ... | 0 / 830 | 100 % | n/a | n/a |
| b599a00 | opentelemetry-sdk: don't read other resource attributes ... | 658 / 830 | 21 % | 100 % | 59 % |
| 9bbc005 | docs(sdk): fix typos in SpanLimits docstring | 0 / 830 | 100 % | n/a | n/a |
| 5321c60 | docs(sdk): remove stale trace_config TODO | 0 / 830 | 100 % | n/a | n/a |

Totals: 11 outcome changes, 0 missed; recall 100 % (955 of 955); precision
32 %; mean savings 63 %. Commits outside the session's packages (exporters,
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
| b6aaa1a | release Hatchling v1.32.1 | 152 / 2105 | 93 % | n/a | 0 % |
| 248141c | Use hatchling plugin manager | 253 / 2105 | 88 % | 100 % | 11 % |
| 6a2b14f | release Hatchling v1.32.2 | 152 / 2105 | 93 % | n/a | 0 % |
| 0941887 | release Hatchling v1.32.3 | 152 / 2105 | 93 % | n/a | 0 % |
| 5c6dc6c | Strip surrounding whitespace from version metadata | 2106 / 2106 | 0 % | 100 % | 14 % |
| cd57f68 | Revert type changes for BuildHookInterface | 2106 / 2106 | 0 % | 100 % | 22 % |

Totals: 0 outcome changes, 0 missed; recall 100 % (788 of 788); precision
16 %; mean savings 61 %, 18 min with three jobs. Before instance-attribute
tracking every row selected every test. The three releases change only
`hatchling.__about__.__version__`, and
`hatchling.plugin.manager.ClassRegister.collect` called
`getattr(registered_class, self.identifier, None)` with the attribute set
from a constructor argument: a dynamic reference bounded only by the
module's import closure, which contains `__about__`, in a method that
every test reaches (`ClassRegister.get` from `CoreMetadata.name`,
`ProjectConfig.env` and `BuilderInterface.__init__`). The one construction
passes a literal (`ClassRegister(..., "PLUGIN_NAME", ...)`), so the lookup
is now the name-bounded `registered_class.PLUGIN_NAME`. Of the 152 still
selected on a release, 67 come from hatchling's CLI entry point, which
dispatches through `vars(parser.parse_args())` (the argparse pattern of
design.md, "Known gaps"), and 78 from the test helper
`__load_template_module`, which imports `f"..templates.{template_name}"`
relative to `__name__`: that resolves to the `helpers.templates.`
modules, and the wheel templates embed
`hatchling.__about__.__version__` in the metadata the tests compare, so
those tests do depend on the release's change (the version is read at
import time, so coverage cannot credit them). 248141c changes
`PluginManager` itself.
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

## Not yet exercised

* A corpus over a monorepo whose per-package test trees share module
  names and are collected in one `--import-mode=importlib` session.
  Per-root prefixes plan such trees together (the opentelemetry API and
  SDK trees above), but only planning, not validation, has been run over
  one.
* A corpus over more than one pytest session of the same repository.

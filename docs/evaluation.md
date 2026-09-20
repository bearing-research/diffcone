# Evaluation on real repositories

Numbers produced by `diffcone corpus --coverage`, which replays a commit
range: for each parent-to-commit pair it plans, runs the full pytest suite
at both snapshots, and checks the plan against (a) tests whose pass/fail
outcome changed and (b) tests that executed changed code under per-test
coverage. Re-run these whenever selection rules change; the numbers below
were produced by the rules at the commit that last touched this file.

Definitions, exactly as `CorpusReport` computes them:

* **affected**: a test that executed a line owned by a changed head symbol
  whose change carries impact (additive-only changes do not count);
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
| d287360 | Add frozendict signature | 37 / 186 | 80 % | 100 % | 3 % |
| 80ddcb3 | Add tests for get_in defaults, ... | 4 / 190 | 98 % | 100 % | 100 % |
| a1e25cb | Expose combined `__annotations__` on composed functions | 43 / 192 | 78 % | 100 % | 19 % |
| 55ce42d | Support Python 3.15 and refresh dev tooling | 38 / 192 | 80 % | n/a | 0 % |
| d2eba03 | Fix interpose([]) raising StopIteration | 22 / 193 | 89 % | 100 % | 9 % |
| 451af60 | Add pysentry-pre-commit | 0 / 193 | 100 % | n/a | n/a |

Totals: 7 outcome changes, 0 missed; recall 100 % (15 of 15); precision
10 %; mean savings 87 %.

Why the low-precision rows are low:

* **Import-time changes are invisible to coverage.** d287360 adds entries
  to a module-level signature registry and 55ce42d touches module-level
  code; those lines run at import, which carries no test context, so
  coverage credits no test although the tests selected through
  module-attribute references genuinely observe the change. Precision is
  understated there by construction.
* **A dynamic `exec` helper.** `tests/test_inspect_args.py::make_func`
  builds functions with `exec`, and its module imports all of toolz, so its
  import closure is the whole package: about 10 tests are selected on every
  production change. `_signatures.create_signature_registry` uses
  `import_module` with a runtime name (unbounded by design) and costs
  another 5.
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
| 73393f3 | Better bankruptcy | 48 / 517 | 91 % | 100 % | 87 % |

Totals: 1 outcome change, 0 missed; recall 100 % (77 of 77); precision
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
and 1 490 tests really do execute the changed method. A corpus over six
such pairs would take about 50 minutes serially; `corpus` has no parallel
mode yet.

## diffcone itself (115 tests)

Last six commits at the time of writing (three docs commits skipped):
4 outcome changes, 0 missed; recall 100 % (310 of 310); precision 97 %;
mean savings 6 %. All three validated commits changed the indexer, which
nearly every test exercises, so they selected 105 of 112, 106 of 113 and
108 of 115 tests; an earlier run over commits that changed only the
execution module selected 20 of 86 and 22 of 87.

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

* override-aware dispatch for `self`/`cls` lookups, including mixin and
  class-attribute overrides (commits 4722191 and its review follow-up);
* module-level variables as symbols with writer edges and enclosing-scope
  defaults (commits ab49129 through cca7217): toolz, structlog and
  pytest-mock unchanged; click moved from 65 % to 66 % savings and 77 % to
  80 % precision and its table above is from the re-run. Between the first
  and last of those commits toolz temporarily lost one affected test
  (recall 93 %), which is what drove the follow-ups;
* prefix-bounded dynamic names (commit 042f611): toolz, click, pytest-mock
  and attrs unchanged (attrs' lazy loader had already stopped being a seed
  once module-level variables became symbols); the pytest pair re-validated
  identically (3 485 of 3 486 selected, 1 490 of 1 490 affected, 43 %
  precision); structlog moved from 94 % to 95 % savings and 91 % to 92 %
  precision (its "Better bankruptcy" row went from 49 to 48 selected; the
  table above is from the re-run); the diffcone-itself section above was
  re-measured over its last six commits.

## Not yet exercised

A full corpus on a multi-minute suite (one pytest pair is measured above;
`corpus` would need parallel worktrees to be practical), fixtures from
*installed third-party* plugins (`--assume-external-fixture`; none of the
six repositories needed it), and monorepos.

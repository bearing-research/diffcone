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
  over validated commits.

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
| 6aabf09 | Stable (a 30-file squash: `Option` restructured, tests reorganised) | 540 / 555 | 3 % | 100 % | 78 % |

Totals: 54 outcome changes, 0 missed; recall 100 % (444 of 444); precision
77 %; mean savings 65 %.

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
| 73393f3 | Better bankruptcy | 49 / 517 | 91 % | 100 % | 85 % |

Totals: 1 outcome change, 0 missed; recall 100 % (77 of 77); precision
91 %; mean savings 94 %.

The first structlog run reported two outcome misses for
`tests/test_tracebacks.py::test_recursive`, which passed at base and
failed at head on two unrelated commits. The test depends on recursion
depth and fails under the coverage tracer; the base suite had run
uninstrumented while the head suite ran under coverage. Both suites now
run under the tracer when `--coverage` is requested (lesson recorded
below).

## diffcone itself (87 tests)

Last five commits at the time of writing (one docs commit skipped):
6 outcome changes, 0 missed; recall 100 % (172 of 172); precision 87 %;
mean savings 41 %. The two commits that changed the indexer selected
76 of 82 and 79 of 84 tests at 96 % precision, since nearly every test
indexes code; the two that changed only the execution module selected
20 of 86 and 22 of 87.

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

From the first structlog run:

1. With `--coverage`, the base suite ran uninstrumented and the head suite
   under the tracer; a recursion-depth test flipped, producing false
   outcome misses. Both suites now run under the same instrumentation and
   the corpus outcome cache is keyed by mode.

## Not yet exercised

Suites that take minutes (per-test coverage cost at scale), plugin-provided
fixtures such as `mocker` (`--assume-external-fixture`; structlog's async
tests needed none), and monorepos.

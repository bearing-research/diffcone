# Evaluation on real repositories

Numbers produced by `diffcone corpus --coverage`, which replays a commit
range, plans each parent-to-commit pair, runs the full pytest suite at both
snapshots and checks the plan against (a) tests whose outcome changed and
(b) tests that executed a changed symbol under per-test coverage. Recall is
the share of dynamically affected tests that were selected and must be 100 %;
precision is the share of selected tests that were dynamically affected;
savings is the share of tests *not* selected.

## toolz (pytoolz/toolz, 193 tests)

Six most recent commits touching Python at the time of measurement
(September 2026), suite runtime about half a second, discovery clean (no
notes). Reproduce with:

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

Totals: 7 outcome changes, 0 missed; coverage recall 100 % (15 of 15);
mean savings 87 %.

What the low-precision rows are:

* **Import-time changes are invisible to coverage.** d287360 adds entries
  to a module-level signature registry; those lines run at import, which
  carries no test context, so coverage credits no test even though the 19
  tests selected through module-attribute references genuinely observe the
  change. 55ce42d is the same shape. Precision is understated there by
  construction, not over-selected by the planner.
* **A dynamic `exec` helper.** `toolz/tests/test_inspect_args.py::make_func`
  builds functions with `exec` and the module imports all of toolz, so its
  import closure is the whole package; its ~10 tests are selected on every
  production change. `_signatures.create_signature_registry` uses
  `import_module` with a runtime name (unbounded by design) and costs
  another 5.
* a1e25cb changed a class structurally (`Compose` gained members), which
  invalidates every `Compose` method; 8 tests execute `Compose` at all.

## click (pallets/click, 555 tests, `src` layout)

Six most recent commits (three touch Python; the other three are docs and
release commits and are skipped). Suite runtime about five seconds.
Discovery resolved every fixture (the conftest `runner` fixture chain) with
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

Totals: 54 outcome changes, 0 missed; coverage recall 100 % (444 of 444);
precision 77 %; mean savings 65 %.

The squash commit is wide because `click.core.Option` changed structurally
(a method was added), which invalidates every `Option` method and hence
every test that defines an option; 78 % of those tests do execute changed
lines, so the width is mostly real.

What the first click run taught, in order:

1. **`getattr` with a parameter as the name.** One helper,
   `click._compat._is_compat_stream_attr`, does `getattr(stream, attr)`
   with `attr` a parameter and was an always-on dynamic seed reachable from
   every test (427 of 464 selections). Its two call sites pass `"encoding"`
   and `"errors"`; call-site literals are now propagated into such
   parameters.
2. **Editable installs shadow `src` layouts.** The validation runs imported
   the editable-installed clone instead of the temporary checkout, so
   outcomes were measured against the wrong revision and coverage attributed
   nothing to the package. toolz's flat layout had hidden this because the
   working directory wins there. Validation now puts the checkout's source
   roots first on `PYTHONPATH` and fails when measured files outside the
   checkout shadow files inside it.
3. **Removed tests are not misses**, and additive-only module changes are
   not coverage ground truth.
4. **Parameter ids with spaces and pipes** (`[TEXT: a|b]`) broke the node
   id parsers.

Mean savings before and after: 10 % → 65 %, with recall at 100 % once the
runs measured the right code.

## diffcone itself (81 tests)

Last three commits at the time of writing: 6 outcome changes, 0 missed;
coverage recall 100 % (186 of 186), precision 95 %, mean savings 17 %
(the commits changed the indexer and planner, which nearly every test
exercises).

## How these numbers moved

Three planner/indexer changes came out of the first toolz run, each driven
by a concrete path in the report:

1. `getattr(x, name)` with `name` drawn from a literal tuple was treated as
   an unbounded dynamic reference (an always-on seed); it is now expanded
   over the literal candidates.
2. Adding a name to a module's import list marked the module structural,
   invalidating every test that lists the module as a lifecycle dependency
   and, through the module's helper class, name-matching 61 tests elsewhere;
   pure additions now carry no impact.
3. Dynamic references were affected by any change anywhere; they are now
   bounded by their module's import closure (dynamic imports excepted).

Mean savings on the same six toolz commits: 73 % → 80 % → 87 %; recall
stayed at 100 % throughout.

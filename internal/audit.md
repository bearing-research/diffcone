# Pre-release audit (2026-10-06)

Five reviews before tagging 0.1.0 (planner and model; indexer, cache and
snapshot; discovery; evidence and Cython; CLI, execution, actions), each
finding confirmed by a reproduction unless marked plausible. Every item
below is a **miss**: a test or benchmark whose outcome changes is not
selected or not run, usually with a complete plan and exit 0. Decided with
the user: all confirmed misses are fixed before the tag, each with a
regression scenario that fails on the old code; docstring changes count
only where code can run them (non-inert decorators, explicit `__doc__`
reads). Status: `open`, `fixed (<commit>)`, or `plausible` (not reproduced;
fix if cheap, else note).

Over-selection, crashes on rare input and stale text are listed at the end.

Later rounds follow the `audit` skill (`.claude/skills/audit/SKILL.md`):
review, record here before fixing, fix in worktree batches, validate on
strata, the census and the corpus, then fact-check the docs.

## Planner and model (P)

| id | finding | status |
|---|---|---|
| P1 | A target's base-revision lifecycle dependencies are dropped: deleting an autouse conftest fixture, a fixture override, or an ASV module `setup` selects nothing (only head targets' dependencies reach the graph). | fixed (planner batch) |
| P2 | `imports_added`/`dependencies_added` carry no impact: adding a missing import to a test (NameError to passing), or an import of an in-scope module whose top level registers something, selects nothing. Not structural, but the symbol's own behaviour changed. | fixed (planner batch) |
| P3 | Rebinding a `def`/`class` name at module level (`helper = 3` after `def helper`) is in neither the module body hash nor a variable symbol: no change at all. | fixed (planner batch) |
| P4 | A manifest target gets no edge to its entry's module, so import-time changes reach it only if the module is listed as a lifecycle dependency. | fixed (planner batch) |
| P5 | Unknown manifest keys (`lifecycle_dependency`, top-level `source_root`) are ignored instead of rejected. | fixed (planner batch) |
| P6 | Docstring changes select nothing even where code runs them (`@doc`-style decorators formatting `__doc__` at import; `f.__doc__` reads). | fixed (planner batch) |

## Exit codes and CLI (X)

| id | finding | status |
|---|---|---|
| X1 | An uncaught exception (e.g. `ValueError` from `--source-root "src="`, `FileNotFoundError` for a missing `--command`) exits 1, the "degraded plan" code. | fixed (planner batch) |
| X2 | `discover` always exits 0, though the docs give it the plan codes; incomplete-discovery notes are not carried into the manifest. | fixed (planner batch) |
| X3 | A source root matching no file gives an empty, complete plan. | fixed (planner batch) |

## Indexer, cache, snapshot (I)

| id | finding | status |
|---|---|---|
| I1 | A module-level name bound more than once (`if/else` or `try/except` imports, a def plus a fallback import) resolves to one binding; changes to the other are invisible. | fixed (indexer batch) |
| I2 | Literal-string tracking misses rebinding (`+=`, walrus, `with ... as`, tuple unpacking, `nonlocal`, module `NAME += ...`, `global NAME` assigned in a function), so `getattr`/`import_module` are bounded to a stale name. | fixed (indexer batch) |
| I3 | Moving or reordering imports is not detected (imports are stripped from body hashes and kept as an unordered set): moving `import pkg.plugin` under `if TYPE_CHECKING:` changes nothing. | fixed (indexer batch) |
| I4 | Imports in a class body create no import edge. | fixed (indexer batch) |
| I5 | A module-level `__getattr__` (PEP 562 lazy loading) has no dependents: `from pkg import thing` served by it is bounded by name only. | fixed (indexer batch) |
| I6 | A relative import above the top-level package resolves to module `""` and is recorded as external, not unresolved. | fixed (indexer batch) |
| I7 | A module-level variable's annotation is not hashed (evaluated at import without `from __future__ import annotations`). | fixed (indexer batch) |
| I8 | A cached whole index keeps the revision spelling first used in `errors[].revision` (cached and uncached reports differ). | fixed (indexer batch) |

## Discovery (D)

| id | finding | status |
|---|---|---|
| D1 | Test files outside the source roots are invisible: `--source-root src` with tests in `tests/` plans "complete", 0 targets, exit 0. | fixed (discovery batch) |
| D2 | pytest 9's `pytest.toml` / `.pytest.toml` and `.pytest.ini` are not read; precedence differs from pytest's. | fixed (discovery batch) |
| D3 | Tests bound by assignment (`test_alias = test_orig`, `test_x = _helper`, class attribute `test_attr = f`, Hypothesis `TestMachine = Machine.TestCase`) are not collected or noted. | fixed (discovery batch) |
| D4 | unittest `runTest` (collected when a TestCase has no `test*` methods) is ignored. | fixed (discovery batch) |
| D5 | An imported unittest TestCase whose bound name does not match `python_classes` is dropped (also through star imports). | fixed (discovery batch) |
| D6 | Any base named `*TestCase` is excused from `unknown_base_class` even when it resolves nowhere (`SharedTestCase = make_base()`). | fixed (discovery batch) |
| D7 | `testpaths`: when no entry exists pytest collects from the rootdir (discovery: nothing); a file named in `testpaths` bypasses `python_files`. | fixed (discovery batch) |
| D8 | `addopts` is split on whitespace, not shlex: `--doctest-glob="*.rst"` keeps its quotes. | fixed (discovery batch) |
| D9 | Doctests: module names under pytest's default `prepend` mode differ from source-root names; modules that cannot be named are dropped without a note; `_docstrings` misses defs under `if`/`try` and `__test__`; text globs other than `.txt/.rst/.md` find nothing. | fixed (discovery batch) |
| D10 | ASV: underscore modules excluded; `benchmark_dir/__init__.py` skipped; imported classes and functions not collected; CamelCase prefixes (`TimeX`, `TrackX`, ...) not recognised; class attribute aliases not collected. | fixed (discovery batch) |
| D11 | A function with `__test__ = True` whose name does not match is collected by pytest. | fixed (discovery batch) |
| D12 | An installed plugin that collects other files (pytest-typing's `tests/*.md` in cattrs, configured by `typing_checkers`; Sybil, nbval) yields tests discovery cannot see and does not report (found by the corpus re-run of collection_check after the discovery batch). A recording settles them (`collected_not_target`). | fixed where it can be (evidence batch): configured collecting plugins are reported (`plugin_collects_files`); `run` executes every collected test the plan does not know (R8); a recording settles the rest. A plugin collecting with no configuration at all stays invisible to a static plan that selects nothing (documented limitation) |

## Running, checking, actions (R)

| id | finding | status |
|---|---|---|
| R1 | `run --evidence` skips the environment check when the evidence plan selects nothing ("nothing selected; not running", exit 0) though the environment differs from the recording; the CI action takes this path. | fixed (run batch) |
| R2 | The selection plugin compares paths against an unresolved root: a relative or symlinked `--repo` deselects every test (exit 5); `collect --repo <relative>` aborts with a false "installed copy" error. | fixed (run batch) |
| R3 | Selected targets that did not run are a warning only; `run` exits with pytest's code. | fixed (run batch) |
| R4 | `--repo` naming a subdirectory of a git repository silently analyses the top level (wrong module names, changes missed). | fixed (run batch) |
| R5 | `validate`/`corpus` parse verbose output: with `-q` in addopts nothing is parsed and they report OK. | fixed (run batch) |
| R6 | `check` ignores misses of diffcone's own run (`NOT RUN`) in its verdict; the run action's plan.json can differ from the plan `run` executed (fallbacks). | fixed (run batch) |
| R7 | Every selected test in a module skipped at import (`importorskip`) makes `run` exit 5 (a false failure). | fixed (run batch) |
| R8 | pytest options after `--` that widen collection (`--doctest-modules`) are invisible to discovery and their tests are deselected. | fixed (run batch) |
| R9 | `run`'s whole-suite fallback with 0 static pytest targets prints "nothing selected; not running" and returns 0 although the suite ran (plausible). | fixed (run batch) |
| R10 | Actions accept `diffcone plan` exit 2 (`|| [ $? -le 3 ]`); `run` action has no `allow-incomplete-discovery` input. | fixed (run batch) |
| R11 | Default `asv run` benchmarks the configured branch, not the checkout (plausible). | fixed (run batch) |

## Evidence and Cython (E)

| id | finding | status |
|---|---|---|
| E1 | `co_filename` is not normalised: a module imported via `sys.path` with `..` (`tests/../src/...`) maps to no symbol, so its tests record nothing of it. | fixed (evidence batch) |
| E2 | Class-body attribute changes consumed at class creation (`@dataclass` field defaults, `Enum` members, metaclasses, `__init_subclass__`) reach only readers of the attribute name. | fixed (evidence batch) |
| E3 | Adding or deleting a decorated function or a subclass (registration at import) never escalates; only DEFINITION_CHANGED does. | fixed (evidence batch) |
| E4 | `multiprocessing` spawn/forkserver children are not flagged (no audited event). | fixed (evidence batch) |
| E5 | Only `PY_START` is monitored: a generator or coroutine started earlier and resumed in a later test is not credited to it. | fixed (evidence batch) |
| E6 | Changed `.py` files outside the source roots are never listed; the unresolved-fixture fallback is suppressed for an unindexed root `conftest.py`. | fixed (evidence batch) |
| E7 | Files read through `pkgutil.get_data` are classified as import activity and dropped. | fixed (evidence batch) |
| E8 | A changed `__all__` does not reach star importers. | fixed (evidence batch) |
| E9 | Adding a module-level `__getattr__`/`__dir__` is not treated as a special-method change. | fixed (evidence batch) |
| E10 | A Cython function with `@cython.profile(False)` is never recorded; changes to it select nothing. | fixed (evidence batch) |
| E11 | Environment fingerprint omits sibling editable installs, `sys.flags`, `PYTHONWARNINGS`, `PYTHONPATH`, pytest plugins and options; `PYTHONHASHSEED=random` accepted (plausible). | fixed (evidence batch: optimize, PYTHONWARNINGS, PYTHONPATH and sibling editables fingerprinted; sibling editable code is not traced, documented) |
| E12 | A missing environment-check report counts as a match (a wrapper stripping `DIFFCONE_CHECK_ENV`) (plausible). | fixed (run batch: the check always reports; no report is a mismatch) |

## Not misses (fix while there)

Fixed in the evidence batch: the stale texts below, `.diffcone/.gitignore`, malformed TOML tables and non-object `asv.conf.json`, malformed or truncated evidence stores (an `EvidenceError`, a malformed store skipped when listing), ASV relative imports in `__init__.py` (D10). The rest remain as listed.

- Over-selection: `norecursedirs` not applied to test files; no `__test__ = False`; ASV relative imports in `__init__.py` give a spurious `unknown_base_class`; WORKTREE follows symlinks leaving the repo; `find_store` ignores the environment when choosing among stores.
- Crashes on malformed input: non-table `tool.pytest`/`ini_options`, non-object `asv.conf.json`; store meta missing keys, truncated `tests-*.bin`.
- `selection.py`: a skipped `Class` collector hides uncollected tests elsewhere in its file from the missing list.
- `worktree_mismatch` only compares `.py` files under the roots.
- `.diffcone/` could carry its own `.gitignore` (`*`), as pytest's cache does.
- Stale text: `report.py` `SCOPE_DESCRIPTION["not_resolved"]` (ships in every report), `declarations.py` docstring ("the head one"), `manifest.py` docstring ("temporary", "future work"), `planner.plan()` docstring ("two committed revisions"), `pytest_static.py` docstring (config bullet; "bases defined elsewhere are reported"), `asv_static.py` docstring (config location), `design.md` ("Four notes", "not modelled: base classes in other modules"), `collection_check.py` `NODE_FILE` suffixes.
- `declarations.py`: any `GitError` is read as "no diffcone.toml" (plausible).

# Round 2 (2026-10-07)

A second round of five reviews: the fix diff itself (regressions), then
static planning, discovery, evidence mode and the documented user journeys
and CI actions from angles the first round did not try. Same rule: every
confirmed miss is fixed with a regression scenario; regressions and
precision blow-ups introduced by round 1's fixes come first.

## Regressions from round 1's fixes (F)

| id | finding | status |
|---|---|---|
| F1 | P6: a docstring edit under any decorator not on the inert list is a definition change, so a DOC commit selects everything (pandas cebedf3f94 `@set_module`: 2 -> 24878 tests; networkx 92f497e2eb: 0 -> 5532). Only decorators that read docstrings, or that the analysis cannot see, should fold it in. | fixed (round 2, F batch) |
| F2 | E2/E3: `open_classes` counts every class with an external base, `object` and `Exception` included, so evidence mode escalates ordinary class-body edits and class additions. | fixed (round 2, F batch) |
| F3 | E3: adding any annotated function (no `from __future__ import annotations`) escalates its module in evidence mode: annotations make `inert_definition` false. | fixed (round 2, F batch) |
| F4 | E11: after `collect --rev`, the project's own editable install (pointing at the real repository, not the temporary worktree) is fingerprinted, so every later evidence run mismatches. | fixed (round 2, F batch) |
| F5 | P2: the base import closure counts function-local and `TYPE_CHECKING` imports, so moving a registration import to the top of the module is judged already run (a miss). | fixed (round 2, F batch) |
| F6 | I3: the import layout ignores ordinary statements, so moving an import across `os.environ[...] = ...`, `sys.path.insert`, `warnings.filterwarnings` in the same block is invisible (a miss). | fixed (round 2, F batch) |
| F7 | The run action plans without the pytest args `run` gets (doctest options now feed discovery), so plan.json and `selected` can differ from what ran; `--junitxml` in the run args misses the warm discovery cache. | fixed (round 2, F batch) |
| F8 | `check --format markdown` counts a plan miss twice (also a NOT RUN of diffcone's run). | fixed (round 2, F batch) |
| F9 | `--source-root ./src` is not normalised (old: silent miss; now: a wrong "holds no Python file" error). | fixed (round 2, F batch) |
| F10 | Import-layout labels omit `with`/`for`/`match` headers: moving an import between two `with` blocks is invisible (plausible). | fixed (round 2, F batch: with/for/match headers in the layout) |

## Static planning (S)

| id | finding | status |
|---|---|---|
| S1 | A src layout planned with the default root `.` names `src/calc/ops.py` `src.calc.ops`; `from calc.ops import add` resolves to nothing, counted as external: complete plan, 0 selected (getting-started's own journey; every static fallback in CI). | fixed (round 2, S batch) |
| S2 | Functions reached only through a registering decorator (`@show.register`, `@register` filling a registry, `@app.command`, a class registered by `__init_subclass__` whose special methods change) are unreachable. | fixed (round 2, S batch) |
| S3 | Class scope: class-level defs are missing from the class body's own scope (a method name used in the body resolves to a module-level homonym), and class bindings leak into lambdas and comprehensions in the class body. | fixed (round 2, S batch) |
| S4 | Star imports lose to earlier bindings: `from a import f` then `from b import *` (or two star imports, the settings pattern) resolves `f` to the first; at runtime the last wins. | fixed (round 2, S batch) |
| S5 | Added imports from outside the source roots (`from json import dumps` fixing a NameError, `from __future__ import annotations`, a builtin shadowed by an import) are invisible: unresolved/external references are not in dependency signatures. | fixed (round 2, S batch) |
| S6 | `exec`/`eval`/`compile` of text read from a file is bounded by the import closure; the file need not be imported (`exec(open("version.py").read())`, plugin loaders). | fixed (round 2, S batch) |
| S7 | Names consumed through strings (`monkeypatch.setattr("a.b.X", ...)`, `mock.patch("a.b.c")`, a string `skipif` condition) or deleted literal variables used by other modules' tests are not dependencies. | fixed (round 2) |
| S8 | An import-time write to another module's globals (`settings.DEBUG = True` in a conftest) reaches only the writer's module. | fixed (round 2) |
| S9 | `setup.py` (and other build hooks) is indexed as a module nobody imports, so a build change selects nothing. | fixed (round 2) |

## Discovery (D, continued)

| id | finding | status |
|---|---|---|
| D13 | Session-wide hooks (`pytest_collection_modifyitems`, `pytest_configure`, `pytest_sessionstart`, ...) in conftests off a test's path are not its dependencies. | fixed (round 2, D batch) |
| D14 | The ini option `usefixtures` is not read. | fixed (round 2, D batch) |
| D15 | Doctests get no fixture closure: autouse fixtures (the usual `doctest_namespace` filler, as in pandas), plugins, ini `usefixtures`. | fixed (round 2, D batch) |
| D16 | An ASV module `setup` that is imported (`from .common import setup`, 33 pandas modules) is not a dependency. | fixed (round 2, D batch) |
| D17 | `pytest_plugins` in a test module registers the plugin for the session; its autouse fixtures and hooks reach every test. | fixed (round 2, D batch) |
| D18 | Hooks and xunit functions bound by import or assignment (`from x import pytest_generate_tests`) are invisible. | fixed (round 2, D batch) |
| D19 | Lifecycle names missing: `setUpModule`/`tearDownModule`, `asyncSetUp`/`asyncTearDown`, Django `setUpTestData`, a class-level `pytest_generate_tests`. | fixed (round 2, D batch) |
| D20 | `@staticmethod` tests lose their first fixture (the first parameter is dropped as `self`). | fixed (round 2, D batch) |
| D21 | `x = pytest.fixture(f, autouse=True)` (one call) is not recognised. | fixed (round 2, D batch) |
| D22 | Collected tests not targets, unreported: a TestCase with `__init__`; nested test classes inherited from a base; star imports re-exporting imported names; a non-literal `__all__`; `-o`/positional paths in addopts; doctests outside the roots; non-literal `getfixturevalue` over a fixture list; collection hooks in plugin modules. | fixed (round 2, D batch) |
| D23 | A `pytest11` plugin of a sibling package in the repository is not loaded. | fixed (round 2, D batch) |
| D24 | Runner configuration outside the source roots (`pyproject.toml`'s pytest table with roots `src tests`, `asv.conf.json`, a root conftest) changes without selecting anything. | fixed (round 2, D batch) |
| D25 | ASV: module-level aliases (`time_alias = _impl`), `benchmark_name`, `timeraw_` code strings, `benchmark_dir` with `..` (0 targets, complete). | fixed (round 2, D batch) |
| D26 | `from django.test import TestCase` in a test module is `imported_test_out_of_scope` (exit 3 for every Django test module). | fixed (round 2, D batch) |

## Evidence mode (E, continued)

| id | finding | status |
|---|---|---|
| E13 | Windows: recorded paths keep `\\`, so nothing maps to a symbol and almost nothing is selected. | fixed (round 2, E batch) |
| E14 | xdist "compute once" session fixtures: only the computing worker's tests are credited. | fixed (round 2, E batch) |
| E15 | A thread outliving its test runs project code no record names. | fixed (round 2, E batch) |
| E16 | A generator or coroutine re-entered with `.throw()`/`.close()` is not seen (PY_THROW). | fixed (round 2, E batch) |
| E17 | Python 3.14 subinterpreters are not flagged. | fixed (round 2, E batch) |
| E18 | An indexed `.py` file read as data (`exec(open(...))`, `inspect.getsource`) selects nothing when it changes. | fixed (round 2, E batch) |
| E19 | Compiled/build file suffixes incomplete and case-sensitive (`Cargo.toml`, `.c.src`, `.F90`, `.pyf`, `.i`). | fixed (round 2) |
| E20 | Files opened by C code (`sqlite3.connect`, `ctypes.dlopen`, `os.access`) are not touches. | fixed (round 2, E batch) |

## Running and CI (R, continued)

| id | finding | status |
|---|---|---|
| R12 | The run action fails every PR when the recorded commit cannot be fetched (force-pushed main). | fixed (round 2, R batch) |
| R13 | A shallow (depth 1) checkout lacks `HEAD^1`: plan exits 2 with an opaque message. | fixed (round 2, R batch) |
| R14 | Unknown-revision errors do not name the revision. | fixed (round 2, R batch) |
| R15 | WORKTREE plans select everything once `__pycache__` exists in a repo that does not ignore it; ASV `results/` blocks `collect`. | fixed (round 2, R batch) |
| R16 | `run -o FILE` writes nothing on a real run. | fixed (round 2, R batch) |
| R17 | Overlapping key prefixes (`diffcone-ubuntu`, `diffcone-ubuntu-py312`) restore another environment's recording and baseline. | fixed (round 2, R batch) |
| R18 | The nightly re-records but cannot save when main has not moved. | fixed (round 2, R batch) |
| R19 | Docs: src-layout roots missing from several journeys; cli.md snapshot claim and exit codes for collect/prune/evidence; report.md rule list. | fixed (round 2, R batch) |

# Round 3 (2026-10-09, before 0.4.0)

Six reviews of the whole code base, weighted to what changed since v0.1.0
(about 3 500 lines: child-process recording, the CI report, the narrowings
of 0.2.0, items 12-17): the evidence recorder (REC), planning from
evidence (EVP), static planning (PLN), indexer, cache and snapshots (IDX),
discovery (DSC), and the CLI, execution and actions (CI). Each finding was
reproduced against real pytest, asv_runner or the action's own shell steps
unless marked plausible; the repro scripts lived in the session's scratch
directory, and each fix carries its own regression scenario. Same rule as
before: every confirmed miss is fixed, with a scenario that fails on the
old code, before the release. "Regression" means it passed at v0.1.0.

The cache identity check found nothing: 42 ordered commit pairs and 7
working-tree states planned through one shared cache, byte-identical to
uncached plans.

## CI: a miss reported as a clean push (CI)

| id | finding | status |
|---|---|---|
| CI-1 | `check` folds baseline failures by target: `test_x[a]` failing at the baseline hides a new failure of `test_x[b]`, reported as already failing (record's push check and the check action). | fixed (CI batch) |
| CI-5 | Re-running a push job that found a miss restores the newest recording (`restore-keys: <prefix>--`), not the push's own, so its kept verdict is ignored and the re-check against the later baseline passes green. | fixed (CI batch) |
| CI-4 | An uncaught exception in `check`/`report` exits 1 (outside `main`'s try); the record step's verdict parsing then fails silently and records the push as checked with 0 misses. | fixed (CI batch) |
| CI-3 | A re-run whose JUnit cannot be read leaves `again` empty and `$((confirmed + again))` aborts the step before `check.json`; the context says checked, 0 misses. | fixed (CI batch) |
| CI-6 | A `workflow_dispatch` report with `post: false` succeeds and so starts the next report's window: misses before it never get an issue. | fixed (CI batch) |
| CI-2 | `diffcone report` accepts `record` contexts from fork pull-request runs, and does not validate field types: a forged artifact files an issue with attacker text, and a malformed one (`"misses": "lots"`) crashes every report until it expires. | fixed (CI batch) |
| CI-9 | A pull-request run whose pytest did not collect selected tests (`run` exit 3) is neither refused nor failed in the report. | fixed (CI batch) |
| CI-7 | The record re-run's argument filter keeps a path after a flag (`-ra tests`): the whole directory re-runs, an unrelated flaky test becomes a confirmed miss, and the flaky count can go negative. | fixed (CI batch) |
| CI-8 | `resolve_command` re-joins with POSIX `shlex.join`, which Windows' argv parsing reads literally (plausible: no Windows run). | fixed (CI batch) |

## Static planning: holes in the narrowings since 0.1.0 (PLN, IDX)

| id | finding | status |
|---|---|---|
| PLN-1 | A class served by a bounded lazy-export table (or `getattr(import_module(...), 'Alt')`, or `m.Alt()` on an unknown receiver) is reached by name match only, which does not reach `__init__`/`__new__`/`__call__`: a constructor body change selects nothing. Regression for the lazy table. | fixed (narrowing batch) |
| PLN-2, IDX-2, IDX-3 | A literal table or dict stays bounded although it is changed through an alias, a helper it is passed to (`fill(T)`), `dict.update(T, ...)`, `globals()[...]`, `setattr(pkg, '_LAZY', ...)`, `sys.modules[...]`, `vars(mod)`, through its module (`core.TABLE[k] = ...`, `pkg.core.TABLE`, `monkeypatch.setitem`, `mock.patch.dict` with an object or a string), `+=` on an imported list, a tuple target, or `symmetric_difference_update`. The `.get` and lazy-table forms are regressions. | fixed (narrowing batch) |
| PLN-3, IDX-1 | A write onto an external module that does not name it directly (an alias, a helper taking the module, `for m in (logging,)`, `MOD = logging`, `object.__setattr__`, `logging.__setattr__`, `exec`, `MonkeyPatch().setattr`, `operator.setitem(vars(...))`, `mock.patch.dict(logging.__dict__)`) is not seen, so `getattr(logging, name)` stays bounded. Regression; `design.md` calls aliases outside the model, which the never-miss rule does not allow. | fixed (narrowing batch) |
| EVP-8 | A test's decorator or default newly running a test-code writer at import is missed statically (evidence handles it). | fixed (narrowing batch) |

## Static planning: other misses (PLN, IDX, EVP)

| id | finding | status |
|---|---|---|
| PLN-4 | Dependency files missed: `dev-requirements.txt`, `test-requirements.txt` (the prefix must start the name), case (`Requirements.txt`), `requirements.pip`, pandas' `ci/deps/*.yaml`, `hatch.toml`, `sitecustomize.py` outside the roots; a nested `setup.py` under a root; any lock-file change between `INDEX` and `WORKTREE` (neither side committed returns `[]`). | fixed (static batch) |
| PLN-5 | A pytest plugin in the repository outside the roots (`pytest_plugins = ["support.plugin"]`, `-p support.plugin`) only gets a `plugin_out_of_scope` note: its autouse fixture changes, complete plan, nothing selected. | fixed (discovery batch) |
| EVP-5 | `pytest_plugins` in a test module registers the plugin for the session (D17), but a change to or addition of it reaches only that module's tests, in both modes. | fixed (discovery batch: static; evidence batch: evidence) |
| EVP-6 | Changing the argument of an import-time call into a mutator (`X = set_mode("slow")` -> `"faster"`, a class attribute, a `parametrize` argument, a test-module variable) reaches no reader of the mutated state, in both modes. | fixed (static batch: static; evidence batch: evidence) |
| IDX-4 | PEP 695/696 type parameters (bounds, defaults, count) are not hashed: no changed symbol. | fixed (static batch) |
| IDX-5 | Reflection over a module (`mod.__dict__['f']`, `.get`, `sys.modules[...].__dict__`, `inspect.getmembers(mod)`, e.g. parametrizing over every function of a module) is not a dependency; only `vars(mod)` is. | fixed (narrowing batch) |
| IDX-6 | WORKTREE trusts `ls-files -m`, which skips assume-unchanged and skip-worktree entries: edits to such files are invisible. | fixed (static batch) |

## Discovery (DSC)

| id | finding | status |
|---|---|---|
| DSC-1 | An autouse fixture imported into a conftest or test module (`from x import auto`, `from x import *`, `auto = x.auto`, a two-level star chain, a `pytest_plugins` package re-exporting two levels deep) is never a dependency: nobody requests it by name. | fixed (discovery batch) |
| DSC-2 | ASV: `setUp`/`SetUp`/`TearDown` (asv_runner matches case-insensitively) and the benchmark class's `__init__` (built for every benchmark, inherited too) are not dependencies. | fixed (discovery batch) |
| DSC-3 | pytest loads `<initial path>/test*/conftest.py` at startup even when the directory is `--ignore`d or in `norecursedirs`; discovery drops it (the `--ignore` half is a regression). | fixed (discovery batch) |
| DSC-4 | `norecursedirs` is applied to an initial path's own components (`-- integration` with `norecursedirs = ["integration"]`): its conftests and doctests are dropped. | fixed (discovery batch) |
| DSC-5 | `PYTEST_ADDOPTS`, `PYTEST_PLUGINS` and pytest options inside `--command` never reach discovery: `run` says "0 of 1 selected (plan complete)". | fixed (discovery batch) |
| DSC-6 | A conftest `pytest_ignore_collect` returning `False` overrides `--ignore`; discovery still drops those tests with no note. | fixed (discovery batch) |
| DSC-7 | `.` as a run argument is not an initial path, so `testpaths` stays in force with no note. | fixed (discovery batch) |
| DSC-8 | A `.rst`/`.txt` file named as a run argument is a doctest whatever `--doctest-glob` says; not listed, no note. | fixed (discovery batch) |
| DSC-9 | `--ignore DIR` (and `--ignore-glob`, `--deselect`, `--doctest-glob`) as two tokens is reported as unmodelled: exit 3, `run` refuses (conservative). | fixed (discovery batch) |
| DSC-10 | An absolute `--ignore` path is not matched: ignored tests stay targets and `run` errors "did not collect" when they are selected. | fixed (discovery batch) |

## Evidence recorder (REC)

| id | finding | status |
|---|---|---|
| REC-1 | A subprocess started outside every test window (conftest import, `pytest_sessionstart`, a session server) sets only `import_subprocess`, which nothing reads, and its record is never folded: a test reading what it produced is not selected. Predates 0.1.0; an xdist controller spawning workers must not trip the fix. | fixed (recorder batch) |
| REC-2 | A process pool reused by a later test (`ProcessPoolExecutor`, the forkserver after first use) flags only the test that started the workers. | fixed (recorder batch) |
| REC-3 | `_actor` returns "import" at the first importlib frame even when a third-party module's top level did the read (matplotlib's `matplotlibrc`): the touch is dropped. | fixed (recorder batch) |
| REC-4 | `ENVIRONMENTS` (item 16) excludes everything under a `sys.prefix` that is also a source directory (`cd svc && python -m venv .`): source goes unrecorded, or `collect` fails with a false installed-copy error. Regression. | fixed (recorder batch) |
| REC-5 | A child whose entry is a module outside the project (`-m doctest`, `-m timeit`, a kernel) or a console script runs text the index does not hold but is not flagged like `-c`; in-process `doctest.testfile` misses the same way. | fixed (recorder batch) |
| REC-6 | A file opened with other casing on a case-insensitive filesystem (`tests/Data/Expected.TXT`) never matches the indexed path. | fixed (recorder batch) |
| REC-7 | The inert-probe rule is bypassed by `posix.posix_spawn` or an assembled `__import__` in a `-c` snippet (contrived; item 10 accepted a deny-list). | fixed (recorder batch) |
| REC-9 | `child._write` swallows `OSError`, so a record can be cut short silently (plausible). | fixed (recorder batch) |

## Planning from evidence (EVP)

| id | finding | status |
|---|---|---|
| EVP-1 | Item 13: a fake reached through a test-code base's `__subclasses__()` is guarded away (holders only include code naming the fake). | fixed (evidence batch) |
| EVP-2 | A function body run only during another module's import that mutates a third module's state escalates only the importing module (`_import_effect`, `_cython_import_effect`). | fixed (evidence batch) |
| EVP-3 | Library lookup sites are dropped for every test-code namespace, but library code can `import_module("tests.test_a")` and `getattr` into it. | fixed (evidence batch) |
| EVP-4 | `run --collect` writes records that were never order-checked yet keeps `reverse_checked=True`: new tests reading a module-level cache miss a change to the cached function. | fixed (evidence batch): an advance from a reverse-checked recording runs the selection again in reverse; what a full `collect --reverse-check` cannot see (a module-level cache read in the middle of both orders) stays the documented limit |
| EVP-7 | The Cython body hash drops blank and `#`-leading lines inside multi-line strings. | fixed (evidence batch) |

## Not misses (fix while there)

All fixed: IDX-7, IDX-8, PLN-6 (static batch), REC-8, REC-10 (recorder batch), DSC-11 (discovery batch), EVP-9 (evidence batch), the `release.yml` heading match (exact now).

- Over-selection: a project `sitecustomize.py` on a source root shadows the child recorder (every child-spawning test always selected; REC-8); a submodule under the roots makes every WORKTREE plan select everything (IDX-7).
- Crash: a `.py` path containing a newline breaks `cat-file --batch` (IDX-8).
- Silent: an `always_run` entry with a misspelt runner or pattern only shows `matched: 0`, though declarations.md says a typo can't silently remove tests (PLN-6).
- Stale text: item 15 promises an exit line at `atexit` and says outside-window spawns "flag as today"; item 10's mechanism describes the old word-list rule (REC-10); the `pytest_static.py` docstring omits `--ignore`/`--ignore-glob` and run-argument paths, and `collection_check.py` discovers without the command's pytest arguments (DSC-11); evidence_design.md says `plan --evidence auto` picks a store whose environment matches, but `find_store` ignores the environment and `plan` never checks it (EVP-9); `release.yml`'s version regex leaves `.` unescaped.

## Found while validating round 3 on strata (V)

| id | finding | status |
|---|---|---|
| V1 | REC-1's accounting of spawns outside every test flagged pytest-xdist workers' start-up queries (`platform` running `uname -p` and `file -b <python>`), so every change of strata selected every test. | fixed: standard-library system queries (`uname`, `file -b <interpreter>`, `ldconfig -p`) are no problem |
| V2 | Escalating a module (EVP-2/EVP-6's `_import_writes`) followed every non-inert `def` body as import-time code; a changed test-data list in a module with a fixture writing shared state selected 6 175 of strata's 6 231 tests. | fixed: a `def` whose header runs no project code (`quiet_header`: `pytest.fixture`, `pytest.mark.*`, `functools`, `contextlib`, `property`, with arguments, defaults and annotations that call nothing) is not followed |
| V3 | REC-4's `environments()` compared the prefix with case-folded roots without folding it on Windows: the environment was not found. | fixed |
| V4 | With V1 and V2 fixed, strata's first replayed commit selects 2 693 of 6 231 (43 %) where 0.3 selected 582 (9.3 %): 2 103 tests only through `lookup_site`. strata's run-time module handles (`_deserialize_module`, `_ensure_cell_module`) unbind every literal table (PLN-2), so `strata.__getattr__`'s lazy table can import any module, and EVP-3 lets it (and `GCTracker`'s graph walk) see a test module's changed name. | open: precision, a decision for the user |

## Known after round 3 (W)

What the round-3 fixes noticed outside their scope, and V4. Each is
reproduced before it is fixed; a plausible one that does not reproduce is
closed with the scenario that shows it.

| id | finding | status |
|---|---|---|
| W1 | V4: a module named at run time (an unbounded `import_module(name)`, `sys.modules.get(n)`, `exec` into a module's `__dict__`) is taken to be any module: it unbinds every literal table and lets lookup sites see any test module. Precision: strata 9.3 % -> 43 % on one commit. Bound what such a handle can be instead. | fixed: run-time module names bounded (prefixes, fresh modules, explicit exec globals, single-name stores), table getters guarded by their writers, `gc` walkers by member, at-import external sites by reach; strata 2 693 -> 939 of 6 231 |
| W2 | An attribute read off a resolved class that nothing defines (`C.__type_params__`, `C.__mro__`, `C.__name__`, `C.nothing`) records no edge to the class, only a name match (none for dunders): what is reached only through the class is missed (plausible). | fixed |
| W3 | Static planning does not see a test running a project script or module as a subprocess (`[sys.executable, "scripts/gen.py"]`, `-m pkg.tool`): a change to it selects nothing. | fixed: a script path, `-m`, `-c` named in code is a dependency; a Python command built at run time is unbounded |
| W4 | Writes through an argument or receiver at import are not modelled (`set_mode(REG)` where the callee mutates its parameter), in both modes; and statically, a body change in a function that calls a writer through a helper with another argument (`_store(m + "!")`) misses the readers. | fixed: writes through parameters and receivers (`indexer/writes.py`), a third-party value's receiver matched only to classes deriving from a third-party class; code that may call a writer differently (`calls:` nodes) |
| W5 | Evidence mode: `lifecycle_changed` (a target whose fixtures, hooks or plugins change without its code changing) is static only; in-process code a hook calls with no static path to a test may be planned only by static escalation (plausible). | fixed: lifecycle changes in evidence mode; hook-phase code that changed or reads a change selects every test |
| W6 | A parameter whose name matches a module-level literal may be read as that literal (`_string_candidates` falls back to the module table) (plausible). | fixed |
| W7 | EVP-4's residual: a module-level cache filled by an earlier test (`get()` storing `compute()`'s result) hides `compute` from a later test's record in every order, so a change to `compute` misses it, also after `collect --reverse-check`. | fixed: callers of a changed function that write module state make its readers affected |
| W9 | An in-scope module passed as a value and read with an unbounded `getattr` (`read(ops, ["other"])` with `def read(obj, names): return getattr(obj, names[0])()`) misses a change to `ops.other`: the receiver-from-elsewhere rule counts only classes handed on, never modules. | fixed: an escaped module depends on its members |
| W10 | `obj.__dict__[k]` and `self.__class__.__dict__[k]` on an object of unknown type recorded no read, where `getattr(obj, k)` records a dynamic one. | fixed: read as `getattr` |
| W11 | Static planning misses code a hook runs that reaches tests only through its effects: a plugin object's `pytest_*` methods registered in `pytest_configure` select nothing, `pytest_generate_tests` in a sub-directory conftest reaches only that directory; process-global effects (`os.environ`) are not modelled. | fixed: `pytest_*` methods of in-scope classes and `import_plugin` modules are session-wide hooks; collection code writing process state is a dependency of every test |
| W12 | A test module's import-time code writing `os.environ` (or other process state) read by a test that does not import it (plausible). | fixed: import-time code of conftests, test modules and what they import that can write process state (`indexer/process.py`) is a dependency of every test |
| W13 | `hasattr(obj, "literal")` is recorded as a reflection site that sees every name (precision). | fixed |
| W14 | A write-only reference (`global X; X = None`) is not told from a read, so a reset fixture counts as a reader of what it resets (precision). | fixed: write-only sites (`writes`, `rebinds` edges) are not readers of a content change |
| W15 | `store._CACHE[k] = v` (a write through a module attribute and a subscript) probably records no `mutated_by` edge (plausible). | fixed: writes through a module attribute and items |
| W16 | A module handed on (`read(m, ...)`) hands on only its submodules, not what it imports: `m.core.TABLE` through a handed-on `m` reaches a module that is not a submodule (plausible). | fixed: a handed-on module hands on what its imports bind |
| W17 | An existing object installed into `sys.modules` (`monkeypatch.setitem(sys.modules, "pkg.core", fake)`, `sys.modules["pkg.core"] = fake`) is ignored by the uses model (plausible). | fixed: installs into `sys.modules` are external writes and, permanent, edges from the module to its installer |
| W18 | Run-time module reach the uses model does not cover: `pickle.loads` (can return any module's table), `f.__globals__`, `gc.get_objects()` as a module handle, a `module_from_spec` copy of a project module written through its handle (plausible). | fixed: `__globals__`, frame globals, unpicklers, `resolve_name`/`locate`, `gc` object lists, `module_from_spec` copies |
| W1b | Static planning keeps strata's literal tables unbound: `serializer._deserialize_module` and `_ensure_cell_module` take module names from serialized data, which can name any module. | decided (2026-10-10, the user): no declaration. Those modules are users' notebook cells, never project modules; strata plans with evidence, where W1 bounds them (939 of 6 231 on the sample commit). |
| W19 | The uses scanner's import map ignores local shadowing (`annotations` from `from __future__ import annotations` taken as a module): harmless escape records. | fixed: names resolve per scope in the use scan |
| W20 | An unresolved attribute (`value.real`, `self.app`) is name-matched to module-level functions and variables of any module, test modules included (strata's `value.real` matches a fixture named `real`; fastapi's `self.app` every `app` in `docs_src`): an attribute read reaches a module's top-level name only through the module object, so only modules handed on as values (W9's escaped modules) should match (precision). | fixed: an attribute read off a value of unknown type matches a module-level name only in modules such a value can be (handed on, named by a module handle or a string, test modules and conftests); bare names and `module_attribute` lookups still match every symbol. Regression found by evaluation (2026-10-11, unreleased): a module bound to another name the use scan follows (`m = ops`, local or module-level; `m = pkg.ops`) was no such module, so `m.value()` missed a change to `ops.value` (0.3.0 selected it). Fixed twice over: a name every binding of which is a module reads off the module (`indexer/aliases.py`, a `references` edge; a name that may hold something else keeps the name-matched read too), and every module bound to a name is one a value of unknown type may be (`tests/test_planning_imports.py`, modules under aliases) |
| W21 | A method called on an attribute of `self` whose type is unknown (`self._client.get(...)`) counts as possibly writing `self` through name matching (`Artifact.info` became a writer of its receiver): sound but broad; typing instance attributes would make it precise (precision). | fixed: an instance attribute is typed when every write constructs one plainly built in-scope class or a third-party value |
| W22 | Static planning: a function obtained from `pickle.loads` by reference, or from `module_from_spec` + `exec_module` of a path nothing bounds, and then called, is no dynamic import site in `references.py`: a change to it selects nothing (evidence mode records it). | fixed: unpicklers and loaders of an unbounded path are unbounded dynamic imports; round trips, pickle bytes in the code and paths the code bounds are bounded by what they load |
| W23 | Evidence mode: unpicklers and loaders of an unbounded spec are not getters that can obtain a test module (plausible). | fixed (by W22): unpicklers and loaders are module getters in evidence mode |
| W24 | A lookup on an in-scope module (`SITE_CLOSURE`) does not see objects other code puts on that module or installs under its name (plausible). | fixed: `module_writers` (code that stores on a module, hands it on or installs under its name) joins the closure of a lookup on the module, in both modes |
| W25 | An object installed into `sys.modules` permanently under a name nothing bounds: importers of the module it may replace do not depend on the installer (a limitation W16-W19 documented; the sound rule makes every module's importers depend on it). | decided (2026-10-10, the user): the sound rule in static planning only (every module's importers depend on an unbounded permanent installer; census: boltons 2 080 -> 3 269 of 3 269 over 8 commits, the rest already near everything); evidence mode skips it, the recording shows what an installed module ran (strata stays 939). Fixed: an `installed in sys.modules under a name nothing bounds` edge from every matching module to the installer, ignored by evidence mode. |
| W26 | Found by the final corpus check (marshmallow 024d8d358a: 2 -> 646 of 646 since W4). What counts as a patch undone after the test is wrong both ways. Pass 2 records every dotted-string patch (`with patch("datetime.datetime", fake)` in a test) as a write onto the external module, so a `getattr(dt, name)` in library code sees any change, and W4's caller effects carry that into every reader of `class_registry._registry` (precision). W24's `SCOPED` mark covers every `patch(...)` call and every `X.setattr(...)`, also `patch(...).start()` with no stop, a bare `MonkeyPatch()` and a `with patch(...)` around a fixture's `yield`, which outlive the test, so `module_writers` drops writers that put objects on a module for later code (plausible miss). | fixed: one rule for a patch undone after the test (`indexer/patches.py`: a `with` item whose body does not yield, a decorator, the `monkeypatch`/`mocker` fixture of a test or fixture); such a patch writes no external module, anything else is a store (marshmallow 024d8d358a: 1 of 646) |
| W27 | Found by the docs pass (2026-10-10). An `asv.conf.json` that does not parse falls back to the default `benchmark_dir` with an `unparsable_config` note that is not an incomplete-discovery kind: when the real `benchmark_dir` differs, the plan has no benchmarks and exits 0 (plausible miss). | fixed: `unparsable_config` makes discovery incomplete; pytest had the same gap (a configuration file pytest stops on was read as empty or skipped), now the same note; `asv.conf.jsonc` is read and trailing commas parse, as asv's JSON5 loader allows. Discovery over the 42 census repositories and pandas: no new note |
| W28 | Found by the docs pass. `environment_differences` (evidence.py) does not compare `optimize` or `pythonpath`, though both are in the recorded environment and its hash: when only `-O` or `PYTHONPATH` differs, `run` falls back with an empty list of differences (and `evidence_not_used.differences` is empty). Not a miss: the fallback happens. | fixed: every field the hash covers is compared, and the list is never empty |
| W29 | Found by the code-quality pass (2026-10-10). `_reverse_run` (execution.py, `run --collect` on a recording made with `--reverse-check`) removed the last `PYTHONPATH` entry after the reverse run, but since 5fa0ed0 (unreleased) `plugin_environment` puts its directory first: with `PYTHONPATH` set, the user's last entry (`src`) was deleted, errors ignored, and the plugin directory leaked. Not in any release (0.3.0 appended the directory). | fixed: `remove_plugin_dir`, as at every other call site; regression test with a sentinel `PYTHONPATH` |
| W8 | Text: a module seeded by an added member is explained as `uses a dynamic import/attribute access ()`; `collection_check.py` cannot read a collection when the project's `addopts` add `-q` (coverage.py); CLAUDE.md's indexer layout omits `uses.py`. | fixed |

## Round 4: the code-quality pass (Q, 2026-10-10)

Six read-only reviews of the whole code base for quality (stale
docstrings, dead code, duplication that must stay in sync, robustness)
before 0.4.0. Duplication that had already drifted turned out to hide
misses, listed here; the quality findings proper are fixed in the same
batches. Every Q item was reproduced with a `FixtureRepo` scenario by the
reviewer unless marked.

| Id | Finding | Status |
|---|---|---|
| Q1 | Planner: `_propagate`, `_import_call_effects` and `_add_caller_effects` test an edge's `detail` (`registers`, `rebinds`, `writes`, `mutated_by`, `_NO_CALL`) without its `kind`; a name-match edge carries the unresolved chain as detail, so a bare unresolved call to a function named `rebinds` (or, at module level, `registers`) is treated as that edge kind: miss (the `registers` case is in 0.3.0). | fixed (A): `model.edge_mark` gives an edge's mark only on `references` edges; every mark check goes through it (planner, evidence_plan, session_state) |
| Q2 | Static: `runpy.run_module(<non-literal>)`'s detail has no "import", so the planner closure-bounds it, while evidence mode special-cases it: miss. One classifier of dynamic details for both planners. | fixed (A): `model.imports_anything` is the one reader of dynamic details (planner, evidence_plan, session_state); `runpy.run_path` and `python -m <parameter>` had the same gap |
| Q3 | Evidence: an added or deleted function is judged by `inert_header`, which ignores annotations; before Python 3.14 annotations run with the `def` (`def f(x: set_mode("fast"))`): miss on 3.12/3.13. `_change` re-derives `planner._runs_at_import` three times. | fixed (C): `_runs_at_import` in evidence_plan (`inert_header and (inert_definition or quiet_header)`) at all three places, variables' evaluated annotations included; a definition change whose annotations call nothing no longer escalates (scenario: readers still selected) |
| Q4 | Discovery: `pytest_plugins` built with `+`, `+=`, `.append`/`.extend` is not read, so the plugin's fixtures and hooks are no dependency: miss. | fixed (D): `pytest_plugins` read however built or imported; unreadable gives `unreadable_plugins` and an unknown dependency of every test |
| Q5 | Discovery: `pytestmark` built with `+`/`+=` loses its `usefixtures`: miss (module and class). | fixed (D) |
| Q6 | Discovery: `indirect=` given as a variable or as the third positional argument counts as not indirect, so the fixture is pruned as a parameter: miss. | fixed (D): anything but literal `True`/a literal list is maybe-indirect: the fixture is followed when it exists |
| Q7 | Discovery (ASV): a `timeraw_` benchmark bound under another name (`timeraw_x = _impl`) is judged by the function's name, so its code's imports are no dependency: miss. | fixed (D) |
| Q8 | Discovery: a test imported from a module that defines it under `if`/`try` (`_definition_bound_to` reads only the top level), and a module-level test binding under `if`, are dropped with no note: tests under-run, exit 0. | fixed (D): one binding walker `_bound_to`; what it can't follow is an `unmodelled_test_binding` note |
| Q9 | Discovery: a collection hook (`pytest_collect_file`) bound by a star import, or imported from outside the roots, gives no `plugin_collects_files` note: exit 0 instead of 3. Three copies of hook registration have drifted. | fixed (D): one `_add_bound` |
| Q10 | Discovery (ASV): `benchmark_dir` resolving to the configuration's own directory (`"."`) matches nothing, with only a `no_benchmarks` note (not incomplete). | fixed (D): `.` matches the configuration's directory; a `benchmark_dir` outside the repository is incomplete |
| Q11 | Static: `_attributed_imports` (dynamics.py) attributes a dynamic import to its known callers without the unresolved-name and constructor-escape guards `_param_values` has: a caller through an unknown receiver is unseen and the dynamic reference is dropped: miss. | fixed (B1): one `_known_callers` guard |
| Q12 | Static: `_is_inert_decorator` and `_canonical` (definitions.py) read only the last import binding, ignoring `alt_imports`: `try: from pkg.deco import final / except: from typing import final` makes a project decorator inert: miss (`_calls_back` likewise for bases). | fixed (B1): `_canonical_names` (alternative, star and local alternative bindings) for inert decorators, quiet decorators, container kinds, call-backs and attribute chains |
| Q13 | Static: a decorator calls the decorated function at import (`deco(f)` calls `f()`), but no edge leads from the module to the decorated symbol, unlike the explicit `final(helper)`: miss. | fixed (B1): a decorator not known passive gives the module (and class) a `decorated` references edge to the symbol, which escapes; evidence mode does not follow it (the recording shows import-time calls) |
| Q14 | Static: a deeply nested expression raises `RecursionError` out of `build_index`: no plan at all. Catch per module as an analysis error. | fixed (B1): `RecursionError` per module is an analysis error, the index is not cached whole |
| Q15 | Static (latent): `Resolver._record` reassigns its `kind` parameter on `uncertain_attr`, so later override/also/alternative edges get kind `attribute`, which the `calls:` and import-call-effect walks skip. No miss found (a name-bounded reference beside it selects). | fixed (B1) |
| Q16 | Evidence: a store path with `#`, `?` or `%` cannot be opened (`sqlite3.connect(f"file:{path}?mode=ro")`); the store is skipped and planning reports no recording. | fixed (C) |
| Q17 | Evidence (precision): `build_input` matches `build.py`/`setup.py` anywhere, so editing `pkg/build.py` selects every test in evidence mode (static uses `is_build_script`). Narrowing: needs a regression scenario. | fixed (C): `is_build_script` for `.py` files, as static planning |
| Q18 | Runtime: `_dirty` parses `git status --porcelain` without `-z`, so a quoted non-ASCII path defeats the filters and `collect` refuses a clean tree. | fixed (E) |
| Q19 | Static: `mock.patch.multiple("pkg.mod", NAME=...)` gives the test no dependency on `pkg.mod`: `_string_targets` (references.py) omits the form that `_patch_like` (uses.py) includes, so neither records it: miss. Three copies of the patch-form list. | fixed (B2): one `patch_target_form`; a dotted patch target is an import of its module |
| Q20 | Static: a process-state write through a patcher nothing undoes (`mock.patch.dict(os.environ, ...).start()`, `pytest.MonkeyPatch().setenv(...)`, `.setitem(os.environ, ...)`) is not a process write (`_process_call`/`_process_store` know direct stores and mutators only): miss. `_restored` is a second notion of "undone" beside `patches.py`. | fixed (B2): `_patched_state`; `_restored` decides `patch.dict` through `module.undone()` |
| Q21 | Static (unverified): `-c` code that changes `sys.path` by assignment (`sys.path[:0] = ...`, `+=`, `=`) is treated as bounded by `_code_edges` (scripts.py), which knows only insert/append/extend and `addsitedir`. | fixed (B2): stores, deletes, augmented assignments and mutators on `sys.path`/`meta_path`/`path_hooks`/`path_importer_cache`/`sys.modules`, and `os.chdir` |
| Q22 | Found while fixing Q3: a subscript or attribute read in an annotation or default counted as calling nothing, though a project `__class_getitem__`, metaclass `__getitem__` or module `__getattr__` runs there at import (`X: Registry["k"] = 1`). | fixed (B1): a subscript or attribute read is safe only off a builtin or a `typing`/`collections`/`types`/`abc` name; a module variable's evaluated annotation is the module's import-time code |
| Q23 | Found while merging (L2): `_canonical_name` (references.py) read only the last import binding, so a module-level `environ['MODE'] = ...` with `environ` from `from os import *`, a `try`/`except` import or a function-local one was no process write: a later test was missed. Also L1, discovery crashed with `RecursionError` on very deep nesting; L3, `m.__file__` on a name bound several ways ran only the primary module's file (latent). | fixed (leftovers): `_canonical_names` with every binding; callers that add dependencies take any name, callers that leave something out need all names (`_only`); discovery retries with a depth limit and reports a too-deep module as unparsed (incomplete) |
| Q24 | Found by the L4 follow-up: pass 1 (`uses.py` `_canonical`) did not know what a star import may bind, so a module handle, `sys.modules` install, unpickled object, `gc` list, `resolve_name` or `run_path` reached through a star-imported name (`from sys import *` then `modules[...]`) was not seen: 7 misses reproduced. Handing on a submodule of a library (`put(os.path)`) did not count for `getattr(os, n)` (plain imports too). | fixed (L4): `_canonical` adds each star module's name and a `?.<chain>` wildcard (any for adding, not known for leaving out); `@NAME` records are also `<star>.<NAME>` for stars outside the roots; a module handed on below a looked-up one counts. L5: the chain-length cost in `_handle` (9.9 s -> 0.28 s on a 3000-link chain) |
| Q25 | Found by the L4 follow-up (N1): a store into an in-scope container through a module handle (`sys.modules["pkg.reg"].TABLE["x"] = f`, `import_module(...).TABLE[...]`) gives no `mutated_by` edge, unlike `pkg.reg.TABLE["x"] = f`: the table's readers do not depend on the writer (plain imports too): miss. | decided (2026-10-10, the user): the precise sound rule. In-scope code writing through any module object gives `mutated_by` edges (bounded handles to those modules' variables, unbounded ones by the written name); a library writing into a module handed to it stays the documented limitation for arguments a library changes; a fixture's save/restore around `yield` counts as undone. The broad rule (an unbounded handle handed on writes every variable) measured strata evidence 958 -> 6231/6231, boltons and packaging static to all, pandas `mutated_by` edges 157 -> 45 343. Then (the user, same day): stores made by test code running inside one test reach only that test (the documented isolation assumption): with the precise rule strata's evidence plan was 955 -> 6174 of 6231, because about 130 tests store `strata.server._state` through a function-local import and the autouse reset reads it in every teardown. Module-level code of test modules and conftests, wider-scoped fixtures and project code stay writers for every reader. Fixed (L6, L7): precise rule as decided (strata 955, census identical but marshmallow 024d8d358a 646 -> 1, pandas `mutated_by` 164 -> 197); `planner.isolated_test_code` (entries in their own module, function-scoped fixtures, `setUp`/`tearDown`, test-module helpers referred to only by those) drops writer edges for other tests; `module_writers` are not filtered (Q11). Found along the way: L4's escape-below rule took `dt.timezone.utc` handed on as a module below `datetime` (marshmallow 024d8d358a 1 -> 646 on round4); a chain below counts only when the project imports it under that name. |

## Round 5: behaviour-first (R, 2026-10-11)

Six reviews from the outside, one feature at a time, each trying to make
diffcone miss: Python constructs, pytest features, the recorder, snapshots
and classification, the CLI-to-CI pipeline, and every rule that narrows
selection. Each finding is reproduced with a `FixtureRepo` scenario in
which the test's outcome changes between the snapshots.

| Id | Finding | Status |
|---|---|---|
| R1 | A git-ignored `.py` under the roots (`local_settings.py`, a generated `_version.py`, a `_pb2.py`) is invisible to `WORKTREE`/`INDEX` snapshots and to `run`'s mismatched-worktree check (both list `ls-files --others --exclude-standard`): editing it on disk selects nothing (the import stays unresolved both sides) and `run` exits 0 while the suite fails: miss. | fixed (C): `WORKTREE` reads ignored `.py` files in directories git does not ignore as a whole; `INDEX` stays staged content, the mismatch check covers it |
| R2 | `run`'s mismatched-worktree check keeps only `.py` differences, though the planner treats any changed file under the roots as reaching every target: a dirty `tests/data.json`, `pytest.ini`, `.pyx` or `diffcone.toml` lets `run` execute a plan made for other content. | fixed (C): `worktree_mismatch` applies the planner's rule (any file under the roots, `diffcone.toml`, runner configuration, build inputs, conftests anywhere, ignored `.py`, modes, dirty submodules) |
| R3 | `validate`/`corpus` report OK with 0 outcome changes when `addopts` has `-x`/`--maxfail` and a test fails at both snapshots: both suites stop early, the missed test is unrun on both sides; `_run_full_pytest` accepts exit 1 as a suite that ran. Oracle unsound; pass `--maxfail=0` and refuse when collected > reported. | fixed (C): `--maxfail=0` last on validate/corpus/collect/record runs; the selection plugin writes `collected-<pid>` and validate refuses a suite that reported fewer outcomes than it collected |
| R4 | pytest's rootdir below the repository root (an ini file in a subdirectory, the monorepo shape): the plan's ids are repository-relative, JUnit classnames and outcome node ids rootdir-relative, so `check` and `validate` report every failure as an unknown MISS; the record action confirms it (`other` -> stands), the job fails and `report` files an issue. False verdicts. | fixed (C): repository-relative node ids from the selection plugin; discovery records pytest's rootdir, configfile and `junit_prefix` (as `determine_setup` finds them); `check` maps through them, `--rootdir`/`--junit-prefix` override; a nested configuration pytest would use is an `unmodelled_runner_option` note |
| R5 | `check`: a directory collector error (a sub-`conftest.py` that fails to import) appears as `classname="" name="tests.sub"`, matched by file only exactly, so it is an unknown MISS on a change the plan covered; `--junit-prefix` makes every case unknown. False verdicts; the record action confirms them. | fixed (C): a collector case matches a file or any dotted directory above targets |
| R6 | The order of module-level and class-level statements relative to `def`/`class` statements is in no hash: the module body hash strips defs, imports and variables (variables are symbols hashed alone, `_import_layout` positions only imports, class attributes hash per name). Moving `X = 1` below a `@deco(X) def f`, `A = 1; A = 2; B = A` -> `A = 1; B = A; A = 2`, `def f` moved below a `try: from pkg.fast import f` fallback, two `@register` defs swapped, two subclasses swapped (`__subclasses__()[0]`), a conftest's `VALUES` moved below `@fixture(params=VALUES)`: the outcome changes, zero changed symbols, nothing selected, both modes. Any "move the constants to the top" refactor: miss. | fixed (A): `Symbol.statement_layout`; a reorder of statements both sides have is `definition_changed`, insertions and plain-def swaps are not; evidence escalates a class reorder |
| R7 | A docstring read through a value (`inspect.getdoc(fn)` in a helper, `describe(obj)`) or a module's `__doc__` (`mod.__doc__`, an unresolved `module_attribute` the docstring-reader map ignores) selects nothing when the docstring changes, both modes. | fixed (A): `Symbol.reads_any_docstring` for readers with an unrooted receiver, `mod.__doc__` resolved through the module attribute; decorators excluded (pandas' `@doc`) |
| R8 | Static: a Python child whose interpreter is not `argv[0]` or whose command is not a list literal is no dependency: `["env", "X=1", sys.executable, "-m", "pkg.tool"]`, `[launcher, "run", sys.executable, "-m", ...]`, `asyncio.create_subprocess_exec(sys.executable, "-m", "pkg.tool")` (varargs), `[os.environ.get("PYTHON", sys.executable), ...]`; `_is_interpreter` looks at `items[0]` only. Evidence mode selects. | fixed (B): the interpreter is the first `sys.executable`/`python` after launcher words or a non-literal followed by `-m`/`-c`/`.py`; varargs process calls and `+` chains read |
| R9 | File mode changes are invisible: `other_files` is keyed by blob id only, so `chmod +x tools/hello.py` (run by path) flips a test and selects nothing, commit and `WORKTREE`. Rare. | fixed (C): `file_modes` in snapshots and the index, compared as unanalysed changes |
| R10 | A tracked file replaced on disk by a directory crashes `WORKTREE` plans (`git hash-object --stdin-paths` on the directory): no plan. | fixed (C) |
| R11 | Static: `name in dir(builtins)` (and `hasattr`/`vars`/`__dict__` of an external module) is not a lookup site a test-code writer newly run at import reaches; `getattr(builtins, name, None)` is. Evidence selects (`lookup_site`). | fixed (A) |
| R12 | Static: text a test compiles and runs in-process (`exec('from pkg import ops\nres = ops.value()', ns)`, `exec(TEMPLATE.format(...))`, `eval('__import__("pkg.ops")...')`, `exec(compile(SRC, ...))`, `exec(gen.source())`) is bounded by the module's import closure, which is empty when the test module imports nothing of the project: miss. Pass 1 and `-c CODE` already parse literal text; pass 2 should, and non-literal text not from a file should be unbounded. | fixed (B): `scripts.code_texts` parses written-out text (literals, templates filled with literals, dedent/compile); a parameter hole is read per call site; text from elsewhere is unbounded (`RUNS_TEXT`); evidence keeps such sites `exec()`-class |
| R13 | Static: a test using `pytester`/`testdir` (the style of every pytest-plugin repository) runs text code with the session's plugins loaded; a change to a `pytest11` entry-point fixture, a `pytest_plugins` module or anything the inline test imports selects nothing. Discovery knows `pytester` only as a builtin fixture. | fixed (B): discovery adds every plugin-level fixture to a `pytester`/`testdir` test; the indexer reads `makepyfile`/`makeconftest`/`makeini`/`runpytest -p`/`plugins=` texts |
| R14 | Static: a test file run through a code runner (`[sys.executable, "-m", "pytest", "tests/inner/check_inner.py"]`, `-m unittest tests.inner.cases`, in-process `pytest.main([...])`, `unittest.defaultTestLoader.loadTestsFromName`) becomes one `imports` edge, which carries import-time changes only; the file's tests, fixtures and conftests are not followed; in-process runners are not modelled at all. | fixed (B): `out.test_runs`: a test file given to a runner runs whole with its conftest chain; in-process `pytest.main`, `unittest.main(module=)`, loaders and `doctest.testfile` modelled; directories and non-literals unbounded |
| R15 | Evidence (precision): `text_code` marks tests whose only text is a string `skipif` condition (pytest 9 evaluates it in `_pytest.skipping`) or an `autospec=True` mock (`unittest.mock` generates signatures): such tests are selected for every change. `TEXT_GENERATORS` excludes `_pytest.mark` but not these. | fixed (D): `unittest.mock` in `TEXT_GENERATORS`; a `_pytest.skipping` string is text code only when it loads a project name |
| R16 | `run` under `addopts = --lf/--ff` exits 3 with a message blaming discovery ("did not collect ... selected target(s)") when pytest's last-failed cache deselected them. Conservative; name the option. | fixed (C) |
| R17 | Static: `match` class patterns (`case C(x=1)`, positional through `__match_args__`) read attributes by name and nothing is recorded (`references.py` has no `visit_MatchClass`): a property or descriptor the pattern reads changes, nothing selected. | fixed (B): `visit_MatchClass`, `ClassScope.match_args` in pass 1 |
| R18 | Static: attribute and method names inside strings the stdlib consumes record nothing, not even a dynamic reference: `operator.attrgetter('v')`, `methodcaller('m')`, `'{0.v}'.format(C())`, `attrgetter('Inner.f')`; a non-literal name likewise. `getattr`/`hasattr` literal and dynamic forms are fine. rich, marshmallow, more-itertools use `attrgetter`; pandas tests use `methodcaller(name)` 257 times. | fixed (B): `attrgetter`/`methodcaller`/`.format` template fields read by name; non-literal → dynamic |
| R19 | Static: a project instance handed to code outside the roots that calls plain-named methods on it never escapes (`escaped_classes` comes from resolved in-scope call sites only): `json.dump(x, w)` / `print(file=w)` / `csv.writer(w)` / `logging.StreamHandler(w)` with `W.write` changed, `dict(C())`/`d.update(C())` with `C.keys`, `shutil.copyfileobj(R(), ...)` with `R.read`, `contextlib.closing(r)` with `R.close`: nothing selected. Dunder protocols are fine. `docs/limitations.md` ("every member of a class whose instances it passes on") promises more than the code does. | fixed (B): `handed_instances` → `escaped_classes` for instances handed to code outside the roots; dunder-only builtins excluded; selections identical on the six corpus repos though escaped classes roughly doubled |
| R20 | Static: `__init__`/`__new__` run by class-creation machinery and consumed through `Class.MEMBER` reads: `enum.Enum`/`IntFlag` member `__init__`/`__new__` body changes read as `Color.RED.code`, a project metaclass doing `cls.inst = cls()`; `_EXPLICIT_SPECIAL_METHODS` keeps `__init__`/`__new__` out of `special_method`/`external_base` edges and the `constructor` edge needs a bare class reference. | fixed (A): `Resolver.class_creation` |
| R21 | Static: a module-level `__dir__` body change is not reached by `dir(mod)` (`__dir__` is nobody's dependency; E9 covered only adding it in evidence mode). Rare. | fixed (A) |
| R22 | Evidence: caches not held in a module variable hide the changed function from later tests' records: `functools.cached_property` on a shared instance (module singleton or session fixture), a class-attribute memo, an instance memo on a singleton, a function-attribute memo; `--reverse-check` rescues two tests, not three. W7's writer rule covers module variables only; `collect.py` clears only `lru_cache` wrappers. strata `metadata_cache.py`, flask/pluggy `cached_property`. The recorder review found the same class with a closure memo decorator (`@memo` with `cache = {}`) and three readers in both orders; `_stored_results` follows `mutated_by` edges to variables only. docs/limitations.md promises "a cache a test fills included". | fixed (D): `SourceIndex.stores`/`computed_passes`/`handed`/`memoized`/`decorations`; a chain member storing a value derived from the chain is a cache filler and the readers of that state are observed. Two documented narrowings keep strata at 955 (the sound-by-default version selected 6231): a method on an untyped receiver is not every method of that name here, and an unknown external decorator keeps nothing |
| R23 | Both modes: a module doctest target's entry is the project function itself (`pkg/mod.py::pkg.mod.fill`), and `isolated_test_code` isolates every entry whose file is the target's file, so with `--doctest-modules` every doctested project function becomes isolated test code and its writer edges are dropped for every reader: a registry filler with a doctest, run at import by a test module or by a session fixture, no longer reaches the tests reading the registry. Same for a manifest whose entry is project code. Contradicts "all project code stays a writer" (docs/limitations.md, design.md). | fixed (A): an entry is isolated only when defined in a test module (a lifecycle module) |
| R24 | Both modes: an import added (or removed) that starts (or stops) running a registering module body: `_module_additions_matter` yields `dependencies_added` on the importer only, and `_import_call_effects` never enters a module, so `REG`'s `mutated_by` edge to `pkg.plug`'s body is not followed since `pkg.plug` is unchanged. `import pkg.plug` added to a test module, to a project module the reader does not import, or function-locally, and the reverse removal: the reader's test fails, only the importer is selected. (A newly imported module writing `os.environ` does reach every test.) Fix without reopening the additive-import trade-off: a module newly (or no longer) imported counts as a newly run writer of what its import-time code mutates or installs. | fixed (A): `planner._newly_run_modules`, roots of `_import_call_effects` with all writer marks; evidence `_newly_run`; additive imports stay non-structural |
| R25 | Static: a module obtained from a literal `import_module('pkg.sub')` / `__import__('pkg.sub').sub` / `import_module('pkg').sub` and handed on (directly, via a local, a dict display, a helper's return) is not an escaped module: `read(m, n): getattr(m, n)()` binds only classes handed on. Handing on by import name works. Plugin loaders `register(import_module("pkg.plugins.x"))`. | fixed (B) |
| R26 | Static: isolation ignores who refers to an entry or to a method named `setUp`/`tearDown`: a `setUp` method called by a session-scoped autouse fixture, or a test function also called at module level (`test_a()`), is isolated though its store outlives the test. Contrived. | fixed (A): entries and per-test hooks isolated iff every referrer is; a class's implicit `external_base`/`special_method` edge to its own method is no referrer (fix-up after a W26 regression) |
| R27 | Static: `monkeypatch` is taken for pytest's by name in any `fixture`-decorated function, without the fixture's scope or an override: a session-scoped `monkeypatch` override plus a session fixture storing through it is treated as undone. Low realism; discovery resolves the fixture chain and knows the override's scope. | partly (A): `monkeypatch`/`mocker` count as pytest's only in a test or function-scoped fixture and not when the module overrides the name; an override in a parent conftest used by a plain test is still assumed undone (would need an index-wide input into pass 1) |
| R28 | Evidence: project code imported before the recorder attaches is never recorded at import, so body changes to import-time-only code (a `@register`-decorated function whose result is stored at import) are missed while the static plan selects them: `-p tests.plugin` in `addopts`/`PYTEST_ADDOPTS`/the user's `--command` (`_collect_argv` appends diffcone's `-p` after the user's), a project `sitecustomize.py` on a root. pytest loads argv `-p` plugins before entry points, `PYTEST_PLUGINS` and conftests. Fix: at start, note every module already in `sys.modules` under a root; treat any change inside a preloaded module as a module-level (escalated) change, or refuse the recording naming the cause; put diffcone's `-p` first. | fixed (D): the recorder starts from `sitecustomize` before any `-p` plugin; modules already imported are `preloaded` (store format 3; old stores rejected) and any change in them is planned from the code; `collect` warns |
| R29 | Evidence: file touches the recorder cannot see or records under the wrong path: `os.DirEntry.stat()/is_file()` (only the listing is recorded), `os.stat`/`os.open` with `dir_fd` after `chdir` (resolved against the wrong base), `os.listdir(fd)` (`_touch(int)` returns), `os.readlink`, `sqlite3.connect("file:...?mode=ro", uri=True)` (recorded verbatim), a tracked symlinked directory (recorded under the alias, the real file changes). Cheap sound fixes: fd-based or `dir_fd` calls flag the window as unbounded; parse `file:` URIs; record the real path below the root too; wrap `readlink` and `DirEntry.stat`. | fixed (D): `install_fd_tracking` (fd → path) shared by both recorders, `DirEntry`/`readlink`/`file:` URIs/real paths; an unresolvable descriptor flags the window |
| R30 | Docs: `docs/guides/evidence.md` ("code that runs on import is planned from the code instead") and `docs/limitations.md` ("a cache a test fills included") promise what R22 and R28 show the code does not do; the guide says nothing about plugin load order or a project `sitecustomize` in the recording process. | fixed (D) |

## Found by the corpus evaluation (E, 2026-10-12)

| Id | Finding | Status |
|---|---|---|
| E1 | A module bound to another name and read through it (`m = ops; m.value()`, module level or local, `m = pkg.ops`, `helpers.m.value()`) missed a body change: W20 (cfed684) matched attribute reads on unknown values only in modules "passed around", and a plain name binding was not one. Never released (0.3.0 selected it). Found while testing the pytest-asyncio refinement. | fixed: `indexer/aliases.py` follows names bound only to modules like the import (`references` edge); any module bound to a name is a handle for W20's set |
| E2 | Discovery: pytest-twisted's `@async_yield_fixture` (any decorator not named `fixture`/`yield_fixture`) made no fixture, and its name `client` was then taken for pytest-django's well-known fixture, dropping what it requested: scrapy, 3 tests (fixture_check). | fixed: decorators whose name ends in `fixture` make fixtures; the well-known table never shadows a definition discovery can't read (`fixture:<name>`) |
| E3 | `validate`/`corpus` reported tests outside the target list as MISSED even when the plan's discovery was incomplete (exit 3): alembic, itsdangerous, a scrapy Sybil doc example. | fixed: listed as outside the target list, don't fail; a non-target in a plan claiming complete discovery is `MISSED (not a target)` |
| E4 | `validate`/`corpus` could not run on coverage.py, tox, virtualenv (pytest usage error), urllib3, pytest-xdist (outcome plugin not loaded), and with `--coverage` on projects that configure coverage themselves (no database); pre-commit timed out. | fixed: the suites never ran. Generated `_version.py` missing from checkouts (copied in now, never a conftest or test file); diffcone's `-p no:cacheprovider` broke `--failed-first` (the plugin stays, on an empty cache of diffcone's, and a run whose cache couldn't be moved is refused); the exit code is checked before the coverage database, whose place is diffcone's config, not `COVERAGE_FILE`. dateutil's one error is genuine; pre-commit's hang is its own Ruby build. |
| E6 | `collect` and `run --collect` still pass `-p no:cacheprovider`, so recording a project whose addopts carry `--failed-first` (coverage.py) is a usage error. Loud, not a miss. | fixed: the cache plugin stays on an empty cache of the recording's own (`DIFFCONE_CACHE_DIR`, set and checked by `collect.py` as by `selection.py`); store format 4, so recordings are made again |
| E5 | Precision (pytest-asyncio): `pytester` text whose f-string field a parametrized argument fills counted as text built at run time. | fixed: literal parametrize values on the test itself are substituted and the text read once per value |
| E7 | Precision, Python 3.14: annotations are evaluated lazily by a code object of their own (`__annotate__`) on the `def` line, which the recorder credited to the function. When pytest reads a signature while collecting, or a decorator does at import (FastAPI, dataclasses, pydantic), a body edit of that project function selected every test or every importer of its module: strata recorded on 3.14 planned 6231 of 6231 (961 on 3.13). Found while testing the audit skill's strata scripts. | fixed: symbols whose only code outside the tests was `__annotate__` are stored (`annotations_only`); a body or docstring change of one is not a change to code run at import or in a hook, and as a reader it read the change there only if its annotations use the name read or call something (`Symbol.annotation_names`, index format 48). strata on 3.14: 656 of 6231 |
| E8 | Precision: each test's recorded run stats its own test file, so editing a test file selects every test in it that ran (on 3.13 and 3.14). | fixed: pytest's `item.location` (`getfslineno`, `Path.exists`) as it logs the test's start; computed first in the window with touches quiet |
| E9 | Precision, Python 3.13 and earlier: `inspect.signature` evaluates string annotations (`from __future__ import annotations`) with `eval` in `inspect.get_annotations`; `inspect` is not among the recorder's text generators (`typing` and `annotationlib`, where 3.14 does the same, are), so those tests count as running text code and are always selected: strata 961 of 6231 on 3.13 against 656 on 3.14 (335 against 11 `text_code`). | fixed with E10: exempting `inspect` like `typing` would have been a miss (E10); all three evaluators now credit what they read |
| E10 | Miss, Python 3.13 and earlier: annotations evaluated from text by a library (`typing.get_type_hints` in pydantic and FastAPI; `annotationlib` on 3.14 for string annotations) were exempt from the text-code flag and ran no project code object, so a test that depended on them (a changed annotation, a deleted class it names) was not selected. Found checking E9's soundness: a framework module outside the checkout calling `get_type_hints` on a module's functions. | fixed: `child.annotation_reads` credits the annotations' owner (from the evaluator's `obj`/`base`/`owner`) as its annotations row, the project functions and classes the text's names resolve to (module namespaces read, never `getattr`), or, with no owner, the file of the namespace evaluated in |
| E11 | Possible miss (recorder): pandas tests driven through the `ip` IPython fixture (`run_cell`, the completer, display formatters) record two symbols and no flag; the pandas code they run is not in their record, so a change to it would not select them. Found by the item 18 spike (7-test recording `store-ip.sqlite`); E12 currently masks it on pandas. | fixed: not the recorder. diffcone ran under `uv run` from its own environment, whose `VIRTUAL_ENV` reached pandas' suite; IPython then warns (\"Attempting to work in a virtualenv\") and pandas' `filterwarnings = error` errors those tests in setup, so their records stop there. The general gap: a test that failed or errored while recorded has a record that stops where it failed, and passes where the environment differs undetected. Now `FLAG_FAILED` (any failed report) selects it always (`no_evidence`), and `VIRTUAL_ENV` is in the recorded environment; store format 5 |
| E12 | Precision (pandas): a hook in `pandas/tests/config/test_localization.py` starts a process the recorder cannot follow during collection, so `import_flagged` holds "" and the evidence planner selects every test for any change. | fixed: the process is pandas' `get_locales` (`subprocess.check_output(["locale", "-a"])`), which `test_localization` calls at import while pytest collects. `child.system_query` now counts `locale`, `locale -a` and `locale -m` as system queries (no Python, no file of the checkout), like `uname` and `ldconfig -p` |
| E13 | Precision (pandas): with E12 fixed the recording still flagged collection: pandas is an editable meson-python install, whose import hook runs `ninja` (to rebuild compiled extensions that are out of date) the first time pandas is imported, outside every test. A non-Python process no record follows, so every change selected every test. | fixed: `ninja` is among `child.TOOLS`: it runs no Python of the project, compiled-source changes are planned by their own rules, and a Python process it starts (Cython) is followed |

## Found by the evidence corpus (E, 2026-10-13)

Eight small suites recorded and replayed (`internal/evaluation.md`,
"Evidence corpus: eight small suites"). No miss; these are precision and
usability findings. Each narrowing gets a roadmap sketch and a
regression test before it lands.

| Id | Finding | Status |
|---|---|---|
| E21 | Precision (pluggy): pytest-benchmark's `pytest_addoption` runs `git rev-parse`, `git config --local remote.origin.url`, `git describe --dirty --always --long`, `git show -s --pretty=...` to tag benchmarks, outside every test in every run. A non-Python process no record follows flags the hook phase, so every change selects every test in any project with pytest-benchmark installed. Candidate: read-only `git` queries among `child.system_query` (they run no Python; what they report is the commit, which no plan models anyway). | fixed: `child.git_query` (rev-parse, describe, `show -s`, config reads; never a revision naming a path, a diff, a write or `-c`) among `child.system_query` |
| E22 | Precision (flask): werkzeug's `Rule._get_func_code` compiles each URL rule's builder from generated code; nearly every flask test builds or matches URLs, so 332 of 372 tests count as running text code and are always selected (a docstring edit selects 89 %). Jinja's `Template.from_code` adds a few. Candidate: werkzeug's routing among `TEXT_GENERATORS`, as `dataclasses` and `attr` are (a library's own template; project code it calls, a custom converter, is recorded as it runs); templates need a separate look (their text is a checkout file the record already holds as read). | fixed: `werkzeug.routing` among `child.TEXT_GENERATORS` |
| E23 | Precision (jinja, boltons): code that introspects whatever it is handed is taken to see any change, anywhere: `jinja2.utils.internalcode` (`internal_code.add(f.__code__)`, a decorator run at import) "sees" a constant changed in `docs/conf.py`, and `boltons.typeutils.make_sentinel` (`sys._getframe(1)`) sees `docs.conf.release`; each escalates the module it ran in at import, and with it every test. What such a reader can see is bounded by what reached it: the function it was handed, the frames on the stack when it ran (the module being imported and its importers). | fixed: in evidence plans, no lookup site sees a Python module of the index (not test code) whose top-level code the recording never ran (`EvidencePlanner._never_imported`); the frame and `__code__` readers stay sites for every imported module |
| E24 | Precision (marshmallow): a docstring change of a `@classmethod` (`SchemaMeta.get_declared_fields`) counts as read by its decorator at import ("its decorator reads it at import"), so it escalates as changed import-time code. `classmethod` and `staticmethod` read nothing of the function at decoration. | fixed: `classmethod`, `staticmethod`, `property` (the builtins, resolved through the module's imports) are not decorators that may read the docstring (`definitions._copies_docstring_only`); static and evidence plans |
| E25 | Precision (typer): `tests/utils.py` calls `typer._completion_shared._get_shell_name()` at import (shellingham's shell detection, which starts a process on macOS), so the recording flags that import and every test importing `tests.utils` (all) is planned statically on any change. Check which process it is; if a system query (`ps`), as E12. | fixed: the process is `ps -ww -o pid= -o ppid= -o args=`; `ps` is a system query |
| E26 | Precision (arrow): with coverage measuring the run (arrow's `addopts` carry `--cov`), coverage.py's tracer resolves each source file (`canonical_filename`, `realpath`) as its code first runs, while the importer is still importing it; the recorder credits the stat to the import as a read of the file, so every source edit escalates its importer and selects every test. Coverage's own frames (and other tracers') read nothing on the project's behalf. | fixed: a touch under coverage's `InOrOut.should_trace` (`child.tracer`) is nobody's read, in a test or out, in both recorders |
| E27 | Precision (boltons, typer, more-itertools, arrow, flask): an edit to a test module (a test added or deleted, a test class's body, a conftest added) escalates to static planning, whose import rule reaches every test (every target depends on every collected test module), and `test_scope` selects the module's tests. The import trade-off (CLAUDE.md, `internal/design.md`); the same decision as roadmap item 19, "a module change that only changes the names it binds". | measured (evaluation.md, "The import trade-off under evidence: measured"): loosening saves 127 of 19 431 and breaks 6 regression tests; recommended: keep; the user's decision |
| E28 | Precision (jinja, more-itertools, flask): release and maintenance commits edit `.github/workflows/*.yml`, `pyproject.toml` and `uv.lock`, each `unobserved_file_changed` (select everything). A workflow file is not read by a local test run; a dependency edit is, through the environment it installs (check whether the recorded environment hash covers that before narrowing anything). | measured with E27: CI files as ordinary files save nothing in the corpus (the same commits touch `pyproject.toml` or `uv.lock`); recommended: keep; the user's decision |
| E29 | Usability (black): its test data holds deliberately invalid `.py` files under the source root; `collect` refuses with analysis errors, so black cannot use evidence at all. Static plans degrade instead (exit 1). | fixed: `diffcone.toml` `[index] exclude` keeps matching `.py` files out of the analysis, read as other files (`snapshot.read_snapshot`) |
| E30 | Usability: when pytest's rootdir is above the repository (a configuration file in a parent directory, the repository having none), recorded node ids carry the extra prefix and match no target; `collect` reports success and every plan has no evidence for any test. `collect` could warn when the recorded ids miss the discovered targets. | fixed: `collect` runs `plan`'s discovery at the commit and warns when no recorded test is a discovered target (`cli._records_matching_no_target`) |
| E31 | Precision (jinja, boltons, flask, marshmallow), what E23 left: a reader of whatever it is handed that runs at import, where the module it runs in is imported by every test, still sees every imported module. `jinja2.utils.internalcode` (`f.__code__`) "sees" a body change of `jinja2.__getattr__`; `boltons.typeutils.make_sentinel` (`sys._getframe(1)`) sees `boltons.__version__`; in flask and marshmallow the chains run through `flask.globals.__getattr__` and `class_registry.register`. Each escalates its module to static planning, and the import rule reaches every test. What such a reader sees is bounded by what reached it (the function it was handed, the frames on the stack when it ran), which the recording could keep. | measured: bounding `f.__code__` (a function's definition) and `name in sys.modules` (which modules are imported) is sound but changes no plan (branch `e31`, not merged): the chains move on to `namedlist`'s `exec` and to code run at a test module's import |

## Found on strata's CI after 0.4.0 (E, CI, 2026-10-18)

No miss. Pull-request plans select everything for any Python change; a
re-run fails before planning.

| Id | Finding | Status |
|---|---|---|
| CI-10 | The `run` and `record` actions deepened a shallow checkout with `git fetch --deepen=1 origin <sha>`, which GitHub serves only from a commit a ref still points at: a re-run, or a run whose pull request moved on, checked out a merge commit since replaced, the base never resolved and every job failed with no plan, the report blaming the recording (strata #1215). | fixed: an absolute `--depth`; a missing base is not retried as an unusable recording |
| E32 | Precision (strata): `_stored_results` takes a decorator's in-place write (`@router.put(...)`, attributed to the function) for a store of what the function computes, so a body change of anything an endpoint can run reaches every reader of the router and every test. 6580 of 6580 for one `DiskCache.put` edit. | open: roadmap item 21 |
| E33 | Precision (strata): an AS record's `>module` source (the module a literal file is) is checked as a table name; once any code may change any table, `_load_local_module`'s installs become `escape *` and every getattr on a library module sees everything. | open: roadmap item 21 (no plan changes alone) |
| E34 | Precision (strata): code other threads run while no test window is open is hook-phase code; which lands there depends on timing, and a change to it selects everything (`pytest_hook_changed`). | open: roadmap item 21, after E32 |


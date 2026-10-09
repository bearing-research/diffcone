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
| CI-1 | `check` folds baseline failures by target: `test_x[a]` failing at the baseline hides a new failure of `test_x[b]`, reported as already failing (record's push check and the check action). | open |
| CI-5 | Re-running a push job that found a miss restores the newest recording (`restore-keys: <prefix>--`), not the push's own, so its kept verdict is ignored and the re-check against the later baseline passes green. | open |
| CI-4 | An uncaught exception in `check`/`report` exits 1 (outside `main`'s try); the record step's verdict parsing then fails silently and records the push as checked with 0 misses. | open |
| CI-3 | A re-run whose JUnit cannot be read leaves `again` empty and `$((confirmed + again))` aborts the step before `check.json`; the context says checked, 0 misses. | open |
| CI-6 | A `workflow_dispatch` report with `post: false` succeeds and so starts the next report's window: misses before it never get an issue. | open |
| CI-2 | `diffcone report` accepts `record` contexts from fork pull-request runs, and does not validate field types: a forged artifact files an issue with attacker text, and a malformed one (`"misses": "lots"`) crashes every report until it expires. | open |
| CI-9 | A pull-request run whose pytest did not collect selected tests (`run` exit 3) is neither refused nor failed in the report. | open |
| CI-7 | The record re-run's argument filter keeps a path after a flag (`-ra tests`): the whole directory re-runs, an unrelated flaky test becomes a confirmed miss, and the flaky count can go negative. | open |
| CI-8 | `resolve_command` re-joins with POSIX `shlex.join`, which Windows' argv parsing reads literally (plausible: no Windows run). | open |

## Static planning: holes in the narrowings since 0.1.0 (PLN, IDX)

| id | finding | status |
|---|---|---|
| PLN-1 | A class served by a bounded lazy-export table (or `getattr(import_module(...), 'Alt')`, or `m.Alt()` on an unknown receiver) is reached by name match only, which does not reach `__init__`/`__new__`/`__call__`: a constructor body change selects nothing. Regression for the lazy table. | open |
| PLN-2, IDX-2, IDX-3 | A literal table or dict stays bounded although it is changed through an alias, a helper it is passed to (`fill(T)`), `dict.update(T, ...)`, `globals()[...]`, `setattr(pkg, '_LAZY', ...)`, `sys.modules[...]`, `vars(mod)`, through its module (`core.TABLE[k] = ...`, `pkg.core.TABLE`, `monkeypatch.setitem`, `mock.patch.dict` with an object or a string), `+=` on an imported list, a tuple target, or `symmetric_difference_update`. The `.get` and lazy-table forms are regressions. | open |
| PLN-3, IDX-1 | A write onto an external module that does not name it directly (an alias, a helper taking the module, `for m in (logging,)`, `MOD = logging`, `object.__setattr__`, `logging.__setattr__`, `exec`, `MonkeyPatch().setattr`, `operator.setitem(vars(...))`, `mock.patch.dict(logging.__dict__)`) is not seen, so `getattr(logging, name)` stays bounded. Regression; `design.md` calls aliases outside the model, which the never-miss rule does not allow. | open |
| EVP-8 | A test's decorator or default newly running a test-code writer at import is missed statically (evidence handles it). | open |

## Static planning: other misses (PLN, IDX, EVP)

| id | finding | status |
|---|---|---|
| PLN-4 | Dependency files missed: `dev-requirements.txt`, `test-requirements.txt` (the prefix must start the name), case (`Requirements.txt`), `requirements.pip`, pandas' `ci/deps/*.yaml`, `hatch.toml`, `sitecustomize.py` outside the roots; a nested `setup.py` under a root; any lock-file change between `INDEX` and `WORKTREE` (neither side committed returns `[]`). | open |
| PLN-5 | A pytest plugin in the repository outside the roots (`pytest_plugins = ["support.plugin"]`, `-p support.plugin`) only gets a `plugin_out_of_scope` note: its autouse fixture changes, complete plan, nothing selected. | open |
| EVP-5 | `pytest_plugins` in a test module registers the plugin for the session (D17), but a change to or addition of it reaches only that module's tests, in both modes. | open |
| EVP-6 | Changing the argument of an import-time call into a mutator (`X = set_mode("slow")` -> `"faster"`, a class attribute, a `parametrize` argument, a test-module variable) reaches no reader of the mutated state, in both modes. | open |
| IDX-4 | PEP 695/696 type parameters (bounds, defaults, count) are not hashed: no changed symbol. | open |
| IDX-5 | Reflection over a module (`mod.__dict__['f']`, `.get`, `sys.modules[...].__dict__`, `inspect.getmembers(mod)`, e.g. parametrizing over every function of a module) is not a dependency; only `vars(mod)` is. | open |
| IDX-6 | WORKTREE trusts `ls-files -m`, which skips assume-unchanged and skip-worktree entries: edits to such files are invisible. | open |

## Discovery (DSC)

| id | finding | status |
|---|---|---|
| DSC-1 | An autouse fixture imported into a conftest or test module (`from x import auto`, `from x import *`, `auto = x.auto`, a two-level star chain, a `pytest_plugins` package re-exporting two levels deep) is never a dependency: nobody requests it by name. | open |
| DSC-2 | ASV: `setUp`/`SetUp`/`TearDown` (asv_runner matches case-insensitively) and the benchmark class's `__init__` (built for every benchmark, inherited too) are not dependencies. | open |
| DSC-3 | pytest loads `<initial path>/test*/conftest.py` at startup even when the directory is `--ignore`d or in `norecursedirs`; discovery drops it (the `--ignore` half is a regression). | open |
| DSC-4 | `norecursedirs` is applied to an initial path's own components (`-- integration` with `norecursedirs = ["integration"]`): its conftests and doctests are dropped. | open |
| DSC-5 | `PYTEST_ADDOPTS`, `PYTEST_PLUGINS` and pytest options inside `--command` never reach discovery: `run` says "0 of 1 selected (plan complete)". | open |
| DSC-6 | A conftest `pytest_ignore_collect` returning `False` overrides `--ignore`; discovery still drops those tests with no note. | open |
| DSC-7 | `.` as a run argument is not an initial path, so `testpaths` stays in force with no note. | open |
| DSC-8 | A `.rst`/`.txt` file named as a run argument is a doctest whatever `--doctest-glob` says; not listed, no note. | open |
| DSC-9 | `--ignore DIR` (and `--ignore-glob`, `--deselect`, `--doctest-glob`) as two tokens is reported as unmodelled: exit 3, `run` refuses (conservative). | open |
| DSC-10 | An absolute `--ignore` path is not matched: ignored tests stay targets and `run` errors "did not collect" when they are selected. | open |

## Evidence recorder (REC)

| id | finding | status |
|---|---|---|
| REC-1 | A subprocess started outside every test window (conftest import, `pytest_sessionstart`, a session server) sets only `import_subprocess`, which nothing reads, and its record is never folded: a test reading what it produced is not selected. Predates 0.1.0; an xdist controller spawning workers must not trip the fix. | open |
| REC-2 | A process pool reused by a later test (`ProcessPoolExecutor`, the forkserver after first use) flags only the test that started the workers. | open |
| REC-3 | `_actor` returns "import" at the first importlib frame even when a third-party module's top level did the read (matplotlib's `matplotlibrc`): the touch is dropped. | open |
| REC-4 | `ENVIRONMENTS` (item 16) excludes everything under a `sys.prefix` that is also a source directory (`cd svc && python -m venv .`): source goes unrecorded, or `collect` fails with a false installed-copy error. Regression. | open |
| REC-5 | A child whose entry is a module outside the project (`-m doctest`, `-m timeit`, a kernel) or a console script runs text the index does not hold but is not flagged like `-c`; in-process `doctest.testfile` misses the same way. | open |
| REC-6 | A file opened with other casing on a case-insensitive filesystem (`tests/Data/Expected.TXT`) never matches the indexed path. | open |
| REC-7 | The inert-probe rule is bypassed by `posix.posix_spawn` or an assembled `__import__` in a `-c` snippet (contrived; item 10 accepted a deny-list). | open |
| REC-9 | `child._write` swallows `OSError`, so a record can be cut short silently (plausible). | open |

## Planning from evidence (EVP)

| id | finding | status |
|---|---|---|
| EVP-1 | Item 13: a fake reached through a test-code base's `__subclasses__()` is guarded away (holders only include code naming the fake). | open |
| EVP-2 | A function body run only during another module's import that mutates a third module's state escalates only the importing module (`_import_effect`, `_cython_import_effect`). | open |
| EVP-3 | Library lookup sites are dropped for every test-code namespace, but library code can `import_module("tests.test_a")` and `getattr` into it. | open |
| EVP-4 | `run --collect` writes records that were never order-checked yet keeps `reverse_checked=True`: new tests reading a module-level cache miss a change to the cached function. | open |
| EVP-7 | The Cython body hash drops blank and `#`-leading lines inside multi-line strings. | open |

## Not misses (fix while there)

- Over-selection: a project `sitecustomize.py` on a source root shadows the child recorder (every child-spawning test always selected; REC-8); a submodule under the roots makes every WORKTREE plan select everything (IDX-7).
- Crash: a `.py` path containing a newline breaks `cat-file --batch` (IDX-8).
- Silent: an `always_run` entry with a misspelt runner or pattern only shows `matched: 0`, though declarations.md says a typo can't silently remove tests (PLN-6).
- Stale text: item 15 promises an exit line at `atexit` and says outside-window spawns "flag as today"; item 10's mechanism describes the old word-list rule (REC-10); the `pytest_static.py` docstring omits `--ignore`/`--ignore-glob` and run-argument paths, and `collection_check.py` discovers without the command's pytest arguments (DSC-11); evidence_design.md says `plan --evidence auto` picks a store whose environment matches, but `find_store` ignores the environment and `plan` never checks it (EVP-9); `release.yml`'s version regex leaves `.` unescaped.

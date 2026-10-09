# Roadmap

Implemented today: `diffcone plan` over two snapshots (commits, the staged
index or the working tree), with targets from a manifest and/or static
pytest and ASV discovery and dependencies the project declares in
`diffcone.toml`; `run`, `validate` (outcome and coverage based) and
`corpus`; recall validated on thirty-four public repositories, discovery
checked against what pytest and ASV really collect in 48, and a
planning-only census of 42 (see
[design.md](design.md) and [evaluation.md](evaluation.md)).

What is left is short, and three of the items below are closed by
measurement rather than by code: the census and the corpora were asked
whether a rule would pay, and said no. Each item states the mechanism,
the trade-off and what "done" means, so the implementation can be checked
against it. Any item that can narrow selection needs a regression
scenario (AGENTS.md).

## The governing rule

A plan may run more tests than needed; it must never miss one. Recall
work comes before precision work, and a selection-rule change is
re-planned on every recorded corpus and re-validated where selections
move. Two checks carry this now, and the cheap one comes first:
`scripts/collection_check.py` compares static discovery against what the
runner really collects in seconds and needs no commit range, while the
corpora run whole suites at both snapshots under coverage. Every miss
found since the third batch was a discovery gap the first check would
have caught, which is why it is the thing to run on a new repository.

## 0. Dynamic references: measured, and left alone

The classification this item once asked for is done (evaluation.md, "What
the dynamic references actually are"), and it closes the item rather than
sharpening it. 71 % of dynamic-caused selections are a `getattr` on a
receiver with no known type and 22 % an `import_module` with no receiver
at all; both need type inference or the caller's configuration, which are
out of scope (AGENTS.md). The heaviest seeds are by-name import utilities
(`import_string`, `load_object`, `import_from_string`, `_load_plugin`)
and plugin dispatchers, called with names that come from a user's
configuration.

That leaves the 5.5 % whose receiver resolves to a module or a class,
where the candidates could be the receiver's members instead of the
select-all fallback. It is not worth building: the bound is sound only
while nothing may have attached an unseen attribute to that receiver, and
a `setattr` with a name the indexer cannot bound -- `monkeypatch.setattr`
in a test suite, almost always -- exists in 34 of the 44 measured
repositories, so the rule would stay switched off in three quarters of
them for a share of selections that is already small. Reopen this only
with a repository where the receiver-bounded shape is measured to
dominate, or after the pool of reflectively written values is tracked
(the values, not just the names, of unknown-receiver writes), which is
the piece that would make the guard cheap.

Instance-attribute tracking (implemented) bounds none of the census's
`self`-attribute cases (present in 11 repositories, causing selections
only in networkx); it is kept because it is sound and tested, with its
evidence limited to hatch.

## 1. Name matches: measured, and left alone

9 % of census selections alone, through a few attribute names on untyped
receivers (`app`, `callback`, anyio's task-group methods,
`load_cert_chain`, `get`, `headers`). Any bound needs receiver types,
which are out of scope, so this item asked for a measurement first. It is
done (evaluation.md, "Are name-match selections worth their cost?") and it
closes the item: in the six repositories measured the name-match bucket is
a handful of selections, the volume sits in `dependency` and in `dynamic
or name match` -- and the latter means the target is reachable both ways,
so bounding name matches would deselect none of it. Reopen only with a
repository where name matching alone is measured to cause selections that
coverage says were not needed.

## 2. Discovery completeness (rare in the census)

Unknown fixtures cause 1 % of census selections.

* pytest: names supplied
  by `pytest_generate_tests` (currently reported as unresolved, so
  conservative), `conftest.py` outside the source roots, and tests a
  runner plugin collects by its own rules (reported as
  `uncollected_test_class`; SQLAlchemy's plugin collects alembic's 2387
  tests, of which pytest's documented rules find 23).
* ASV: `params` expansion as parameter cases, benchmark
  directories outside the source roots, and `validate` for ASV (run `asv
  run --bench` at both snapshots and compare which benchmarks ran).
(The collection-based validator this item asked for is implemented:
`scripts/collection_check.py`, evaluation.md, "Static discovery against
real collection".)

## 3. Other resolution work

* **Declared bounds** (deliberately left open, not forgotten): the dangerous
  half of `diffcone.toml`. telling
  diffcone that a dynamic seed reaches *only* certain modules is the only
  lever that moves sphinx, pip, networkx or scrapy off select-all
  (evaluation.md, "What the dynamic references actually are"), and it lets
  a project narrow selection on its own authority. Not designed yet, and
  not to be built without deciding how a plan that trusted a bound says
  so.
* A `watch` loop for the developer inner loop once incremental analysis
  exists.

## 4. Planner cost on large trees

**Status.** On pandas (2026-10-06) a warm evidence plan took 46-56 s, and
two thirds of it was Python's cyclic garbage collector walking the two
indexes' heap while planning allocated (`cProfile` overstated discovery
instead: its overhead grows with Python calls). `plan()` now runs with the
collector suspended: 21 s, the same selection, and a lower peak RSS
(1.7 GB against 2.5 GB). What remains of that plan: discovery of both
sides 7.6 s CPU, evidence planning 4.1 s, the index cache 3.7 s, static
escalation 2.2 s. Discovery of committed snapshots is now cached per commit
(`DiscoveryCache`), and a cached head lets the head index come from the
index cache too. The machine never got quiet enough for wall times, so in
CPU time, interleaved, two rounds each: replanning the same pair 15.4-16.0 s
against 33.7-36.0 s with the discovery cache off, and a new head on a
cached base (the next commit of a chain) 26.6-31.6 s. Under lighter load
the same cached replan took 10.9 s CPU: evidence planning 8.1 s (5.0 s of
it the static escalation, twice), loading the two indexes 2.2 s,
classification 1.1 s, discovery 0.1 s.
Earlier: with the module cache (design.md, "Module cache") a warm
working-tree plan on a synthetic 2 501-module tree spends 0.27 s indexing
and 0.4 to 0.55 s in `plan_from_indexes`: unioning the two indexes' edges
into one graph (0.16 s), dependency signatures for classification and
per-target explanation paths (about 0.1 s for 1 000 selected targets).

**Mechanism.** Profile first (`cProfile` around `plan_from_indexes` on
the synthetic tree); the candidates the current profile suggests are
building the union graph without re-adding every edge of both indexes,
and producing explanation paths without a sort per selected target.

**Trade-off.** Both touch determinism-sensitive code (sorted adjacency,
breadth-first order); the byte-identical-report tests and the recorded
corpora are the guard. Not worth doing before a real repository of that
size is in the evaluation set.

**Done when.** The synthetic tree plans warm in under 0.7 s wall with a
pending edit, and every recorded plan is byte-identical.

## 5. Execution evidence: per-test coverage as a planning input

**Status.** Designed in [evidence_design.md](evidence_design.md); the
pandas spike passed its go/no-go. On 79 pandas commits the median plan
selects 6.4 % of tests (static: 100 % on every one), and recording costs
1.25–1.45× a plain run with identical outcomes. Stages 1–3 are
implemented (2026-09-25). Stage 4's pandas check passed (2026-10-05):
100 % recall on 20 commit pairs, with evidence selecting 10.7-90 % on half
of them where static selects everything (evaluation.md, "pandas: recall
of evidence plans"); the corpus re-plan is what remains. Not
built yet: advancing a store to head from a partial run (item 6).

**Mechanism.** A stdlib pytest plugin records which symbols each test
executed (plus the files it opened, stat'ed or listed, which import ran
what, and whether it spawned a subprocess) in a real run at commit C.
Names looked up dynamically cannot be recorded without changing outcomes,
so tests that executed an unbounded lookup or reflection site stand in for
them. The plan selects a test when it executed a changed symbol, or a
one-hop static reader of a non-body change. Changes that run at import
escalate to static planning.

**Stages** (each lands with its tests and is pushed on its own):

1. *Recorder and store.* `src/diffcone/collect.py` is the plugin, loaded
   with `-p diffcone.collect`; it writes raw per-process records.
   `diffcone collect` (in `execution.py`, the only module that runs project
   code) runs the suite at a clean HEAD or at `--rev` in a temporary
   worktree. It maps code objects to symbols against diffcone's own index
   of C and writes `.diffcone/evidence/<commit>-<env>.sqlite`. `diffcone
   evidence` lists the stores. Refusals: Python older than 3.12, a
   monitoring tool id already in use, a dirty tree, a suite that ran an
   installed copy of the project, and any exception inside the plugin.
2. *Planning.* `plan --evidence auto|PATH` (`src/diffcone/evidence_plan.py`)
   computes E per change kind and selects on it. It escalates the rest
   through `plan_from_indexes` restricted to explicit seeds without
   dynamic pseudo-seeds. Report `schema_version` 3 adds
   `analysis.evidence` and the reason rules in the design. Evidence taken
   at C plans C → base and C → head and selects their union. A test that
   runs identically at C and at both snapshots cannot differ between them,
   so any C works, and C = base is the precise case.
3. *Running.* `run --evidence` pins `PYTHONHASHSEED` to the recorded
   value and loads the plugin in check mode, which compares the
   environment fingerprint before any test runs. On a mismatch, `run`
   re-plans statically and runs that instead. `validate` and `corpus`
   take `--evidence` so recall is measured with the same machinery.
4. *Recall.* The pandas check (done) and the corpus re-plan below.

**Trade-off.** Sound only under determinism, test isolation and an
unchanged environment. Each has a guard or a detector, and the residuals
are stated in the design. Opt-in; static stays the default.

**Done when.** Recall is 100 % on at least 20 pandas commits (evidence
collected at an older commit, whole suite run at each commit under
`validate --coverage`), and the corpus re-planned with evidence shows no
miss and states its savings against static.

## 6. Advancing an evidence store (`run --collect`)

**Status.** Done (2026-10-06): `run --collect`, `evidence.advance`. On
pandas, 24 commits planned in a chain from advanced stores have 100 %
recall, mean selection 53 % against 76 % from stores up to five commits
old, and the advanced stores match full recordings within a full
recording's own noise (evaluation.md, "pandas: advancing the store").
The pandas recall run
showed why it matters: planned from a store up to five commits old, one
compiled-source edit or root conftest change kept every later pair of its
window at 100 %, and selection drifted from 71 % to 90 % inside a window.
A store at the base of every plan avoids both, but a full recording per
commit costs a full suite run.

**Mechanism.** `run --evidence auto --collect` runs the evidence plan's
selection with the recorder in check *and* record mode, then writes a
store for head:

* *Unselected tests keep their records.* The plan from the store's commit
  C selects the union of C → base and C → head; a test outside it runs
  identically at C and at head, so its C record is its head record
  (evidence_design.md, "Advancing without a full run"). A record of a test
  that is no longer a target at head (deleted since C) is dropped, which
  only ever widens a later plan.
* *Selected tests get the fresh record*, folded against head's index. A
  selected test that produced none (not collected, an error before its
  protocol) is dropped from the store, so the next plan selects it as
  `no_evidence`. Its `unstable` flag is carried over: a partial run cannot
  re-check order dependence.
* *Process-wide data is the union* of C's and the run's (import phase,
  hook phase, importing modules, paths read at import, subprocesses at
  import): the run imported only the modules its tests needed, and a
  stale entry only escalates more.
* *Symbol and path tables* are rebuilt from the names, so records of both
  commits share one table.
* *Nothing selected*: no run, and the store is C's relabelled to head
  (every test runs identically). The store records the commit it was
  advanced from and the commit of the last full collection in its line,
  and `diffcone evidence` shows both.

Refusals, all before anything runs: a dirty tree or a head that is not the
checked-out `HEAD` (the store names a commit); a pytest command or
arguments that differ from the store's (the same arguments deselect the
same cases, so a fresh record covers what the old one did); `--dry-run`.
No store is written when the environment check fails (the static plan
runs, as today), when pytest exits with anything but 0 or 1 (interrupted,
collection error, usage error), or when folding refuses the records.

**Trade-off.** Correctness rests on the plan being sound, the assumption
evidence planning already makes; an unsound plan would now also leave a
stale record behind, so a miss can outlive its commit. A full `collect`
resets the line, and the union of process-wide data only grows until
one does (more escalation, never less). The run uses
`-p no:cacheprovider`, as `collect` does.

**Done when.** On pandas, the 24 commits of the recall range are planned
in a chain, each from the store advanced at its parent, with 100 % recall
against the recorded full-suite coverage runs; and at the commits where a
full collection exists, every test's advanced record matches the full
one (differences explained).

## 7. Compiled sources under evidence (Cython)

**Status.** Done (2026-10-06): stages 1-4 below. With evidence recorded on a
`profile=True` build, a Cython body edit selects the tests that executed
the function; on 25 pandas commits recall is 100 % at a median of 19 %
(evaluation.md, "pandas: Cython edits planned from a profiled build").
What remains is in "Not covered" below. Before this, any
change to a compiled source or build file selects everything under evidence
(`unobserved_file_changed`). In pandas that is 86 of the last 500
first-parent commits before `3f57341` (17 %), 13 of them touching nothing
else. Edits concentrate in a few modules: `parsers.pyx` 14, `timedeltas.pyx`
12, `offsets.pyx` 10, `testing.pyx` 8, `tzconversion.pyx` 7.

**What a change reaches, statically.** A `.pyx` edit changes its module and,
through the C functions its `.pxd` declares, every module that `cimport`s
that `.pxd`, transitively. `.pxi`/`.pxi.in` files reach the modules that
`include` them. Over pandas' 41 extension modules: `parsers`, `testing`,
`window.aggregations` and `tslib` reach only themselves; `timedeltas` and
`tzconversion` 6; `conversion` 7; `offsets` 12; `np_datetime` 18. Anything
reaching `lib` or `index` (`offsets`, `conversion`, `period`, `np_datetime`)
is probably reached by nearly every test, so the gain is in the narrow
modules.

**Mechanism (to be validated): function level, like Python.** No branch or
line analysis: the question is which Cython functions changed, and which
tests executed them.

* *What changed.* A Cython indexer finds `def`, `cdef` and `cpdef`
  functions, methods and `cdef class` blocks in `.pyx`, `.pxd` and `.pxi`
  files and hashes their bodies, as the Python indexer does. diffcone is
  stdlib only, so it is a tolerant, indentation-based block reader, not
  Cython's parser. An edit outside every function (`cimport`, `ctypedef`,
  structs, module constants, `include`) is a module-level change that
  reaches other modules through the `cimport` graph above. A `.pxi.in`
  template, a `meson.build` file or a hand-written C source keeps selecting
  everything.
* *Who executed it.* Evidence is recorded against a build with Cython's
  `profile=True` directive. Checked on a toy extension (Python 3.13, Cython
  3.3): every profiled function then raises the same `sys.monitoring`
  `PY_START` the recorder already handles, with the `.pyx` file and the
  function's first line. That covers `def`, `cpdef` and `cdef` functions,
  and slots reached without a call: an `__add__` run by `+`, a property's
  `__get__`, a `cdef public` attribute. (`co_qualname` lacks the class, so
  symbols are matched by file and line.) Runs keep the ordinary build:
  profiling does not change what executes.
* *The gap.* `nogil` functions emit nothing. They run only when a traced
  function calls them, so a change to one selects the tests that executed
  any of its Cython callers, through a static call graph over Cython
  function names (within the module and through `cimport`ed `.pxd`
  declarations). `inline` functions in `.pxd` files are still to be
  checked.

**Spike.**
1. Build pandas with Cython line tracing and run the suite under
   coverage's Cython plugin with per-test contexts: the oracle of which
   tests executed which `.pyx` lines. Checked on a toy extension (Python
   3.13, Cython 3.3, coverage 7.16): it works only with Cython's legacy
   tracing (`-X linetrace=True`, `-DCYTHON_TRACE=1
   -DCYTHON_USE_SYS_MONITORING=0`) and coverage's `ctrace` core
   (`COVERAGE_CORE=ctrace`; the plugin is unsupported under `sysmon`, the
   default from 3.14). It attributes slot-based execution (an `__add__`
   reached by `+`, a property) to the test. pandas' meson files take both through `add_project_arguments`
   (`language: 'cython'` and `'c'`).
2. Record evidence against a `profile=True` build of the same commit.
3. For each compiled edit among recent commits, compare the tests the rule
   would select with the tests whose oracle shows they executed a changed
   function (lines mapped to functions, as coverage validation does for
   Python). Report misses and size.
4. Cost: recording against the profiled build, against today's recorder.

**Spike results** (pandas `3f57341`, one profiled and one line-traced
build, the whole suite under `-n 8`; the analysis scripts,
`scripts/cython_spike/analyse.py` with its prototype reader
`cyblocks.py`, and `rerun_check.py` were removed once `diffcone.cython`
replaced them and are at commit `689bfc7`):

* *Soundness.* Of 1 254 Cython functions the oracle saw run, 1 075 are
  covered by their own start events and 105 through their Cython callers.
  155 (function, test) pairs remain missed, spread over a handful of tests
  that parametrize over many indexes. Rerunning just those tests in the
  same order in both builds, all but 10 disappear (order noise, as between
  two full Python recordings), and the 10 left (`BlockPlacement.__iter__`,
  `Interval.__richcmp__`) are seen when their test runs alone.
* *Three refinements it took*, each found as a miss: (1) a function's span
  starts at its first decorator, where a code object's first line is;
  (2) nested functions belong to their parent, as in the Python index (a
  nested `def` line runs with the parent); (3) the caller rule covers
  `cpdef` as well as `nogil`: a `cpdef`'s C body raises no start when
  called with `skip_dispatch`, which an explicit `Base.method(self, ...)`
  does (pandas' engines' `get_loc`), and callers are every function that
  *names* it, which also covers functions taken as pointers (`period.pyx`'s
  `get_asfreq_func`).
* *Profiling can crash.* `profile=True` makes `parsers.pyx` segfault on
  constructing its `TextReader`; it had to be excluded
  (`# cython: profile=False`). A module that cannot be profiled has no
  evidence, so its edits keep selecting everything, and `parsers.pyx` is
  the most edited Cython file.
* *Size.* Of 86 commits in the last 500 that touch a compiled source or
  build file, 33 change only Cython function bodies; the rule selects a
  median of 607 tests (2.7 %) on them, with 1 oracle miss. 15 change only
  C, build files or templates and 38 change Cython outside function bodies
  (class attribute declarations, `cimport`s, constants), which the spike
  counted as select-all; telling additive module-level edits apart is the
  next gain.
* *Cost.* The profiled build ran the suite with the start probe in 252 s
  and the same 13 failures as the ordinary build.

**Stages** (each lands with its tests and is pushed on its own):

1. *Reader and changes* (done 2026-10-06). `src/diffcone/cython.py` reads `.pyx`, `.pxd` and
   `.pxi` files under the source roots into functions (qualified name,
   span from the first decorator, body hash, `nogil`, `cpdef`, the names
   the body mentions; nested functions belong to their parent) and a hash
   of everything outside them, comments excluded. Snapshots carry those
   files' content, the index carries the result (`INDEX_FORMAT` bump), and
   `cython_changes` diffs two indexes. Static planning is unchanged: a
   compiled edit still selects everything, since nothing static connects
   a Python test to a Cython function.
2. *Recorder and store* (done 2026-10-06). `fold` maps a code object whose file is a Cython
   source to the outermost function holding its first line, named
   `<path>::<qualname>`. Collection needs a build with `profile=True`;
   diffcone does not build, so that is documented, not enforced.
3. *Planning* (done 2026-10-06). With evidence, a Cython edit that changes only function
   bodies selects the tests that executed a changed function, and for a
   `nogil` or `cpdef` function also those that executed any Cython
   function naming it. It still selects everything when the edit changes
   anything outside function bodies, adds or deletes a file, or touches a
   module the store holds no Cython record for (a module built without
   profiling looks exactly like that).
4. *Recall* (done 2026-10-06). The pandas compiled edits replayed against a store collected
   on a profiled build, checked against the line-traced oracle.

**Not covered.** `.pxi.in` templates, hand-written C and build files still
select everything (edits outside function bodies are item 8). A module the
store holds no Cython record of (built without profiling) selects
everything for its edits.

**Trade-off.** Any miss here is a miss in compiled code, which coverage
validation does not see. The rule ships only if the spike shows no miss,
or a guard that closes each one (the `nogil` caller rule is the first).

**Done when.** The spike has run, and either the rule is shown sound on
the recent compiled edits with its savings stated, or the gap is measured
and the item is closed as measured and left alone, like items 0 and 1.

## 8. Cython edits outside function bodies

**Status.** Done (2026-10-06). Of pandas' last 500 first-parent commits, 46
change Cython outside function bodies or add or delete functions; 37 are
now bounded by the names they change (5 of them still select everything
for C or build files they also touch), and 9 stay file-level (a deleted
name Python can see, a class docstring, a bare call). Replayed at the
evidence commit against the line-traced oracle: no miss, 16 of 28 planned
edits narrow at a median of 13 % (evaluation.md, "pandas: Cython edits
outside function bodies"). Reviewing the replay found that a `.pyx` global
its `.pxd` declares reaches cimporters, now handled. Before this, such an
edit selected everything: 38 of the 86 compiled commits by the spike's
count. A survey of their diffs (pandas `3f57341`):
names added to or dropped from `cimport` and `import` lists, new module
constants and `cdef` globals, `.pxd` signatures and `ctypedef`s, `cdef
class` attribute declarations, structs and enums, extern blocks moved to
a shared `.pxd`, helper functions added or deleted, and class docstrings.
One bare call at module level (`_fill_safe_years()`).

**Mechanism: the names a statement binds.** The Python rules (evidence_plan
docstring) say a variable is observed by its readers, an added or deleted
name also by the lookup and reflection sites, a deleted one also by its
importers, and code that runs at import escalates. The Cython equivalent:

* *Statements.* Outside function spans, the reader splits the file into
  logical statements with Python's `tokenize` (it reads all 106 pandas
  Cython files on 3.11 and 3.14; a file it cannot read keeps today's
  file-level change). Each statement has a scope (the module or a class),
  the names it binds, whether those names are visible from Python, and a
  hash. Bindings: one per item of an `import`/`cimport` list (so
  reformatting a list or adding a name changes only that name); the
  declarator of a `cdef`/`ctypedef`/`DEF` declaration or of an extern or
  `.pxd` function declaration; assignment targets; a class attribute
  declaration or assignment; the name of a struct, enum, union or fused
  type together with its members, as one statement (member order sets
  enum values and layout); a class header's class name.
* *What changed.* A name changed when the ordered list of hashes of the
  statements binding it differs between the two sides, so redefinition
  order counts. Added and deleted functions become changed names too
  (`def`/`cpdef` visible, `cdef` not). Order among the other non-import
  statements is compared as well.
* *Who notices.* A C name is compiled into the code that mentions it, so a
  changed name is observed by every Cython function (in any file: a
  `.pxd` reaches its cimporters) whose header or body mentions it, through
  the `nogil`/`cpdef` caller rule, and through the import effect of
  item 7. A `.pyx` file's C names stay in it, unless its `.pxd` declares
  them too (a C global the `.pyx` initialises and cimporters read). A name visible from Python is also observed by the Python
  readers that look it up by that name (an attribute reference nothing
  resolves: the index records Python code reading names off compiled
  modules exactly so), and when added or deleted by the lookup and
  reflection sites. A class attribute declaration also reaches the
  class's methods and every function naming the class: instance layout and
  generated pickling change for every instance, and anything holding one
  got it there.
* *Still everything.* Code that runs at import or that nothing names: a
  bare expression statement, a module or class docstring (`__doc__`, a
  special name; Python escalates these too), a special attribute or an
  added or deleted special method, a changed class header (bases),
  `include`, compile-time `IF`, star imports, a reordered statement, and a
  deleted name visible from Python (a Python module importing it fails at
  import, and the index does not record imports of names from compiled
  modules). A reader in a module the store holds no Cython record of
  selects everything, as for body edits.

**Trade-off.** It narrows: each rule needs a regression scenario on the
profiled fixture extension, and the risk is a binding the reader
misattributes. The fallback for anything unparsed is the file-level
change, never a smaller set.

**Done when.** The rules land with scenarios; pandas' 38 commits are
re-planned with the count that leaves select-all and the median size,
and real outside edits replayed at the evidence commit (their hunks
reverse-applied there) are checked against the line-traced oracle: every
test whose traced lines fall in a function mentioning a name on a
changed line must be selected, and any flagged name that the rules
dropped is explained.

## 9. GitHub Actions: record at night, plan during the day (pandas trial)

**Status.** In progress (2026-10-06). Done: `diffcone check` (1 below),
discovery completeness settled by the recording (2): a pandas evidence
plan no longer exits 3, with its one note settled against a recording of
22 566 collected tests, none of them outside the targets; `diffcone
prune` (3): the scratch pandas cache went from 5.8 GB to 167 MB, 29 MB
with the store under zstd, and a plan from it to a new head took 18.6 s
(15.3 s CPU, 2.2 GB peak RSS) on an otherwise quiet laptop;
`collect --env-var` (4); errors that name the commit to fetch when a
shallow checkout lacks it (5); and the composite actions with a pandas
example (7, `actions/`, docs/ci.md), run locally step by step but not yet
on GitHub. Left: running them on a fork, which gives the runner timings
(6).
The first step toward real use: run diffcone beside an unchanged CI and
measure it against what CI finds.
Decided: a separate job runs alongside the existing ones and is monitored,
not trusted; environments are pinned (pandas installs from `pixi.lock`);
the trial covers one Linux job (`ubuntu-24.04`, `py313`, the `not
single_cpu` step), on a fork of pandas first, never upstream without
asking.

**Mechanism.**

* *Nightly recording* (a scheduled workflow on `main`, plus a manual
  trigger). Check out `main` at C, set up the job's pixi environment, build
  pandas the ordinary way (no `profile=True`: Cython edits then select
  everything, which is rare and safe), and run `diffcone collect` with the
  job's pytest command and markers. Plan C -> C once so the index and
  discovery caches for C exist, prune every cache entry not for C, and save
  `.diffcone/` with `actions/cache` under `diffcone-<job>-<C>`. A broken
  recording saves nothing, and PRs keep yesterday's. `actions/cache` fits:
  pull requests, forks included, read the default branch's caches but cannot
  write them, so a PR cannot plant evidence; unused entries expire after
  seven days; a pandas store is 20 MB and C's caches about 450 MB before
  compression.
* *PR job* (beside the existing jobs). Restore the newest
  `diffcone-<job>-` entry by prefix, set up the same environment, and
  `diffcone run --base <merge-base> --head HEAD --evidence auto` with the
  job's command and `--junitxml`. A PR that changes `pixi.lock` no longer
  matches the recorded environment and runs everything, as it should. Plan
  JSON and JUnit are uploaded.
* *Verdict.* The existing `py313` job also writes `--junitxml` (the one
  change to it). A job that needs both compares them: every test that
  failed or errored in the full run must be in the plan, and a selected
  test's outcome must agree in both runs. It writes the verdict and the
  time saved to the step summary and never fails the workflow during the
  trial.

**What diffcone needs.**

1. `diffcone check --plan plan.json --full full.xml [--baseline
   nightly.xml] [--run NAME=run.xml ...]`: read a plan and JUnit XML
   (pytest's `classname` and `name` mapped back to node IDs as pytest's
   junitxml builds them, parameters folded as targets are, a collection
   error standing for its file) and report every test that failed or
   errored in the full run but was not selected. A failure the nightly run
   at C also had is reported as already failing, not as a miss. Each
   `--run` is a selective run read from its JUnit (the tests in it are the
   tests it ran): diffcone's own, whose outcomes must agree with the full
   run's, and **pytest-testmon's** (`pytest --testmon`, its data recorded by
   the same nightly job), so the two selectors are compared on the same
   pull requests by misses, tests run and test time. Text, Markdown (for
   the step summary) and JSON; exit 1 when the plan missed a failure.
2. *Discovery completeness settled by the recording.* A static pandas plan
   exits 3, so `run` refuses: one `uncollected_test_class` note
   (`TestPandasDelegate.Delegator`, a helper class with a method named
   `test_method`). The note exists because a plugin may collect such a
   class (SQLAlchemy's collects `<Name>Test`), and discovery cannot know.
   The recording can: it ran pytest's real collection at C, with the
   environment `run` checks before any test. So the recorder keeps every
   node ID pytest collected (before marker deselection, parameters
   folded), and in evidence mode a note stops counting as incomplete when
   the same note was there at C, its file is unchanged since C, and pytest
   collected nothing at C that was not a discovery target. A test pytest
   collected that is not a target keeps the plan incomplete and is named.
   When `run` falls back to the static plan (the environment differs), an
   incomplete static plan runs the whole suite instead of its selection.
   Narrowing (exit 3 to 0): regression scenarios for each condition.
3. Cache pruning to one commit's index, discovery and module entries
   (`diffcone prune --keep C`; entries an older diffcone wrote go too).
4. Project variables in the environment fingerprint: pandas' behaviour
   depends on `PANDAS_FUTURE`, which the recorder does not read today. The
   store must say which variables it recorded, so planning compares the
   same ones.
5. A plain error naming the commit to fetch when the evidence commit is not
   in the checkout (the pandas job fetches full history, but a shallow
   checkout needs only `git fetch --depth=1 origin <C>`).
6. Cold and warm plan times on a 4-core GitHub runner.
7. The workflows themselves, as composite actions in this repository and
   documented examples.

**Trade-off.** Nightly compute (the suite under the recorder) and about
0.5 GB of cache a day. The store ages through the day: the later a PR's
base, the more C -> base adds to every plan.

**Done when.** On the fork, the workflows run on real pull requests (how
to get PR traffic onto a fork is open: replaying upstream PR heads means
pushing to the user's GitHub account, which needs the user's approval) for
long enough to see failures: each miss is fixed or explained, and the
selection size and time saved are recorded in evaluation.md.

## 10. Recorder precision: subprocesses and threads that hide nothing (strata trial)

**Status.** In progress (2026-10-07). A recording of strata's unit suite
(6 225 tests) flags 2 658 tests as always selected. A diagnostic run
attributed the flags: 671 tests start a real subprocess (the notebook cell
harness, `uv run`) and some 600 more use a shared fixture that does, which
is right; 576 only run `python -c` version probes (`import sys;
print(sys.version_info...)`); 866 only leave a thread running (strata's
`MetricsWriter`, executor threads).

**Mechanism.**

* *Inert interpreter probes.* A subprocess whose command line is a Python
  interpreter (its name starts with `python`, or it is `sys.executable`),
  optional single-letter flags (`-I`, `-E`, `-S`, `-s`, `-B`, `-u`, `-O`),
  then `-c CODE`, where CODE names none of the project's top-level packages
  (`DIFFCONE_COLLECT_PACKAGES`, as a word) and none of `exec`, `eval`,
  `open`, `runpy`, `import_module`, `__import__`, `compile`, runs no project
  code: it does not flag the test. Nor does a short list of other programs'
  queries that run no Python of the project (`uv python list|find|dir`,
  `uv --version`): strata caches `uv python list` with `lru_cache`, the
  recorder clears project caches before each test so that cached work is
  recorded, and so every test reaching it ran, and was flagged for, the
  query (1 852 tests). Anything else (`-m`, a script, `uv run`, `git`,
  whose hooks may run project code, a shell, `os.system`) still does.
* *Threads.* The E15 flag (a test that leaves a thread running is always
  selected) protects the wrong test: code the thread runs during that test
  is already recorded for it. The gap is a later test during which a
  long-lived thread sits in one frame (a `while` loop): no new event fires,
  so the loop is credited to nobody after the first test. So every window
  that opens (a test, a shared fixture's setup) is credited with the
  project code on every other thread's stack (`sys._current_frames()`),
  and the flag goes. A background loop is then credited to every test that
  runs beside it, which is what it can affect.

**Trade-off.** Both narrow selection: a probe that does load project code
by a route the word list misses would be missed. The list is the code
loading Python offers; `PYTHONSTARTUP` only runs in interactive sessions,
and a `.pth` file runs installed code, not the checkout's. Threads trade a
flag on one test for credit on many, which widens per change but only
where a loop really runs.

**Done when** the strata recording's always-selected share falls by the
probe and thread-only tests, with scenarios: a probe does not flag, a probe
naming a project package or running a script does; a later test running
beside a looping thread is selected when the loop body changes (missed
before), and the thread's starter is no longer always selected.

**Measured on strata (2026-10-07)**, fresh recording at each commit's
parent, CI arguments passed: 3-4 file commits select 13-15 % (the
subprocess floor is 10 %); a 6-file notebook commit 28.7 %; an 8-file
commit touching `server.py` 53.8 % (100 % before a stat of a source file
stopped counting as a read). Static planning selects 100 % on every one.

**Next, each needing its own sketch before code:**

* *Readers of a test fake's member*: item 13.
* *Lookups on an external module the project writes to*: item 14.
* *Recording inside child processes* (the subprocess floor): item 15.

## 11. A report over CI runs (`diffcone report`, `actions/report`)

**Status.** Implemented (2026-10-08): `diffcone report`
(`src/diffcone/ci_report.py`), `actions/report`, and `context.json` in the
results `run` and `record` upload; tested on fixture artifact trees and by
running the actions' context snippets, and the action's download and
report steps run against strata's artifacts. Open: strata's nightly
workflow, once a release carries the action. Drop this item then.

**Mechanism.**

* *Context.* `run` writes `context.json` beside its plan: event, commit,
  base, the recording used (its commit) or why none was (none restored, not
  fetchable, unusable), whether incomplete discovery was allowed, and the
  plan's and run's exit codes; `-o ran.json` is the plan that ran (the
  static one when the environment differs from the recording's). `record`
  writes it in a last `always()` step, so a recording that failed is
  described too: event, commit, whether it recorded, whether the push was
  checked and, if not, why, the recording compared with, misses, flaky
  re-runs, whether the check itself failed, and whether the verdict was
  kept from an earlier run of the commit (`check.json` now keeps `checked`,
  `check_note` and `check_error`, so a kept verdict says what it was). The
  report reads facts the actions wrote, never logs.
* *`diffcone report --dir DIR [--format markdown|json] [-o FILE]`.* Reads
  files only and runs nothing, like `check`. `DIR/<run id>/run.json` (id,
  URL, event, created time, head commit; optional: without it the run is
  its directory's name and the event the context's) and
  `DIR/<run id>/<artifact>/` with `context.json`, `ran.json` or
  `plan.json`, `verdict.json`, `rerun-verdict.json`; an artifact's files
  directly in a run's directory (`gh run download -n`) are an error, not
  a run without results. Per artifact name
  (one job, or one matrix cell): pull-request runs (count, selected share
  median and max, plans made from the code and why, refusals over
  incomplete discovery, failed plans) and pushes (checked or not and why,
  new failures, flaky, misses, failed checks). Then each confirmed miss:
  commit, test, kind, the plan's unselected reason. Misses are counted as
  the action counts them: what is not a test stands, and of the tests the
  re-run's own misses (all of them when there is no re-run verdict). A
  kept verdict carries a count only and is reported as such; a count its
  uploaded verdicts do not name is reported too. Exit 1 on any miss or
  failed check, so a caller can act on it.
* *`actions/report`.* The window starts where the report workflow's last
  successful run started (`hours` before now for the first): completed
  runs whose last update is in it, listed from three days earlier, so a run
  still going at one report, or a schedule GitHub delayed, falls in the
  next. Each run's unexpired artifacts with the prefix (paginated) are
  downloaded by name, with a `run.json`; a listing or download that fails,
  or a window over `gh run list`'s 1000 runs, is said on top of the report
  and fails the job, so the next report covers the window again (and no
  `fail-on-miss`: a red report would hold the window open). The report goes
  to the job summary and as a comment on the open issue with
  `report-label` (created when missing); one issue per confirmed (commit,
  test) with `miss-label`, listing the cells, deduplicated by a marker in
  the body, titles cut to fit, one failed issue not stopping the others.
  Inputs: `hours`, `workflow`, `artifact-prefix`, `report-label`,
  `miss-label` (empty skips either), `github-token`. Needs `actions: read`
  and `issues: write`.

* *Elsewhere* (2026-10-08). `repository` reads another repository's runs
  (a public one with the workflow's own token), `issues-repository` and
  `issues-token` post elsewhere; issues posted elsewhere name the source
  (report issue found by its title, so several projects share a tracker;
  miss markers carry the source). Miss issues outside diffcone's tracker
  carry a pre-filled link to a diffcone issue: a human decides what
  leaves a project. `comment: on-problem` comments only when the report is
  not ok. diffcone's own `report-strata.yml` reports strata's runs into
  diffcone's tracker this way.

**Trade-off.** Artifacts expire (90 days by default) and the window is
what was uploaded; a run that uploaded nothing (cancelled before the
upload step) is missing from the report, which counts runs per cell so the
gap shows. Artifacts from actions older than the context show as such
rather than being guessed at.

**Done when** strata's nightly report runs on a release carrying the
action.

## 12. A test run that changes its own environment (strata trial)

**Status.** Implemented (2026-10-08), unreleased; drop this item once a
release carries it. Two of strata's test files ran
`uv run` in a notebook whose interpreter was the test interpreter, and
`uv` synced the notebook's `orjson>=3.10` into the test environment:
orjson 3.12.0 (the lock) became 3.13.0 during every recording. The
recorder fingerprints the environment at the end of the run, so no pull
request (orjson 3.12.0, fresh from the lock) ever matched a recording, and
every Linux and macOS PR planned from the code. Nothing said why beyond
`run`'s stderr; strata found it by reading job logs (fixed there in #1082).

**Mechanism.**

* *Recorder.* Each recording process also computes `environment()` when
  it starts (`_start`, before monitoring) and writes it as
  `environment_at_start`. The fold keeps the first process's start
  environment beside the end one when they differ (`Evidence.
  environment_at_start`, in the store's metadata; `None` when equal), and
  `collect` warns, naming the differences (`environment_differences`):
  the test run changed its own environment, and a run in the environment
  it started from will not use the recording.
* *Keyed by the end, as now.* Tests after the change ran under it; keying
  by the start would let a fresh environment use evidence partly recorded
  under another one. The mismatch keeps falling back to the static plan,
  which over-selects and never misses.
* *`run`.* On a mismatch, `-o` adds `evidence_not_used` to the plan it
  writes: `reason` (`environment`), `differences` (the lines `run` prints)
  and `recording_changed_its_environment` (the environment now equals the
  recording's start environment). `run` prints the same.
* *Report.* A plan made from the code with `evidence_not_used` counts
  under its differences ("the environment differed: orjson==3.13.0
  recorded, not installed now, ...", or "the recording's own test run
  changed its environment: ..."), so the nightly report names the
  package.

**Trade-off.** One more `importlib.metadata` scan per recording process
(milliseconds). A run that changes the environment and changes it back is
not seen, and does not need to be: the fingerprint matches.

**Done when** a recording whose test installs a distribution warns and
names it, `run -o` against that recording says the recording changed its
environment, and the report shows it per cell.

## 13. Readers of a test fake's member (strata trial)

**Status.** Implemented (2026-10-08), unreleased. On `0da0faf7`, with
the recording at its parent, evidence selects 2 384 of 5 827 tests (40.9
%, from 3 135, 53.8 %). The name-match readers fell from 1 849 to 612: 588
of those come from a library change in `server.py` and 4 from test-code
lookup sites that can see the fake. strata #1055 (`0da0faf7`) added
`_FakePipe.read` to a fake in `tests/notebook/test_remote_console_stream.py`.
Under evidence, an added method's readers join E, and its readers include
every name match: every `x.read()` on a receiver the index cannot type. So
every test that ran any library code calling `.read()` was selected (1 849
tests), while the only code that can land on the new method is code
holding a `_FakePipe`, and one test builds one (through
`_FakeProcess.__init__`; `_FakeProcess.wait`, added with it, is the same
case).

**Mechanism.** In `_Observers._readers` (and `_class_body`, which reads
attribute names the same way), when the changed symbol M is a non-dunder
member of a class C, the name-matched readers of M that are functions or
methods become *guarded*: a test is selected through one only when its
record also holds a symbol of G(C), the code that can have handed it an
instance. Static readers (`C.read`, `self.read` resolved through the MRO)
stay unguarded, and so does everything else the change reaches.

* *F*, the classes whose instances carry M: C and its subclasses.
* *G(C)*: every function or method that refers to a class of F (an edge,
  or an unresolved name equal to the class's name), the members of F's
  classes (a running method holds `self`: this covers pytest's own test
  classes), and the lookup and reflection sites that can see the classes'
  namespace (the ones `_sites` would observe for them).
* The guard is applied per test in `_evidence_decision`: `executed`
  meets the guarded readers and meets G. The reason names both
  ("executed drain, which reads _FakePipe.read by name, and
  _FakeProcess.__init__, which can hand it a _FakePipe").

The rule applies only when holding an instance needs one of those
symbols to run in the test. Otherwise M's name matches stay unguarded, as
now. It needs all of these:

1. Every class of F is test code (`_TestCode.is_test_code`: a module
   holding targets, or a conftest), and every ancestor of each is too. A
   fake of a library base can be found through the base
   (`__subclasses__()`, a registry), and a base the index cannot see may
   register it.
2. No class of F runs code when it is created (`_runs_on_creation`:
   decorators, keywords, an ancestor's `__init_subclass__`).
3. Every member of G is a function or method. A reference from module or
   class top-level code (`PIPE = _FakePipe()`, a decorator, a default:
   the indexer attributes those to the module or class as well) means an
   instance built at import that any test may reach.
4. No member of G ran during an import or outside every test window
   (`Evidence.import_phase`, `import_by`, `hook_phase`): an instance built
   in `pytest_generate_tests` or at import reaches a test that never runs
   the code that built it.

An instance only exists after code that names the class (or one of the
sites) runs, and test code is not imported by the library. A test that
reaches M through a library reader therefore also ran the code that built
or received the instance: the test itself, a helper it called, or a
fixture, whose setup is credited to every test that uses it.

**Trade-off.** This narrows selection. What it does not see is an
instance one test leaves behind for a later one: stored in a library
global, or held by a thread that outlives its test. That is the evidence
argument's isolation assumption (evidence_design.md, "test isolation"),
and the reverse-order collection (`--reverse-check`) is its detector: the
later test's record then depends on order, so it is flagged unstable and
always selected. Unpickling a fake from a file is the same case. The rule
is not applied to fakes in helper modules that hold no tests
(`tests/helpers.py` is not test code by `_TestCode`'s definition); that is
a later widening once this one is measured.

**Done when** scenarios cover the cases below, and strata's `0da0faf7`,
planned on a recording at its parent, selects the tests that build a
`_FakePipe` rather than the 1 849 name-match readers, with `diffcone
check` against a full run at `0da0faf7` showing no miss.

* *Narrowed (selected before):* a test that runs the library reader but
  never builds the fake is not selected. Its fail-first scenario is the
  narrowing itself.
* *Still selected:*
  * a test building the fake directly;
  * a test building it through a helper's `__init__`, or through a
    fixture;
  * a test building a subclass defined in another test file;
  * a pytest test class whose method passes `self` to the reader;
  * a deleted method, for the tests that built the fake at the recorded
    commit.
* *Rule off (as now):*
  * a module-level instance;
  * a fake used in a `parametrize` decorator;
  * a fake subclassing a library class;
  * a fake whose builder runs in `pytest_generate_tests`;
  * a decorated fake.

## 14. Lookups on an external module the project writes to, under evidence (strata trial)

**Status.** Implemented (2026-10-08), unreleased. With item 13, `0da0faf7`
selects 1 921 of 5 827 tests (33.0 %). `diffcone check` against full runs
at `0da0faf7` and its parent finds no miss, but that commit has no new
failure, so it could not have shown one. A lookup by a name nothing bounds on an
external module (`dir(builtins)`, `hasattr(os, name)`, `getattr(httpx,
name)`) is bounded, unless project code writes to that module (or one
above or below it). strata's tests do: `monkeypatch.setattr(builtins,
"open", ...)`, `builtins.__import__`, `patch.dict(os.environ, ...)`. So
`_external_lookups` makes five library sites full dynamic or reflection
sites: `analyze_cell` and `_BUILTIN_NAMES` (`dir(builtins)`), `_cpus` and
`_memory_mb` (`hasattr(os, ...)`), and `RemoteStore._post`
(`getattr(httpx, ...)`). Under evidence, a reflection site sees every
namespace. So any added or deleted name, or any changed variable, anywhere
selects every test that ran `analyze_cell`. On `0da0faf7` after item 13,
lookup sites select 1 524 tests: `analyze_cell` is among the sites for
1 050 of them and the only reason for 595.

**Mechanism (evidence only; static planning is unchanged).** What such a
site can find of the project's is only what project code stored on the
module. When every writer of that store is a function or method that ran
only inside test windows at C, the writer is in the record of every test
where the store was visible:

* a write in a test or a fixture (`monkeypatch.setattr`, `mock.patch`)
  ran in that test's window, or in a shared fixture credited to every
  user. A change to the writer, or to a value it stores (whose readers
  include the writer), selects those tests;
* a value the site hands on is then used by code that reads it by name
  (`getattr(builtins, n).attr`), which is a reader in its own right.

So under evidence such a site does not join E for a change elsewhere,
except a change to code that runs at import. Escalations, variables,
class bodies and added or deleted modules may now run a writer at import,
leaving the store for every later test. Such a change reaches the site
wherever it is: the filter that keeps library sites away from test-module
namespaces is about seeing names, not about stores. That is narrowed only
when test code alone can run the writers (`_callers`). Every writer, and
everything that can call one, transitively, must be test code. Callers
are references, name matches and lookup sites that can see it, and
top-level code ends a chain. None of it may be used as a value: a
callback, a registry entry. Then only a change to a symbol of that set
reaches the site. A dataclass change in a library module, or a new class
of methods in a test module, does not. A helper module (not test code)
importing a test's writer, library code calling it by name, or a
registry holding it turns this off. So do a writer at module or class
top level, or one that ran during an import or outside every test at C.

* *Index.* The module facts record each write's writer with the module
  (`external_writes` becomes (module, writer) pairs), and the index keeps
  the values used as values (`escaped_values`). `_external_lookups`
  marks the sites it turns dynamic with "on an external module in-scope
  code writes to" in the detail. The marker has no "import" in it, which
  static planning and `_site_kind` read. It also records each marked
  site's writers in `SourceIndex.external_sites`. `INDEX_FORMAT` is
  bumped (26).
* *Evidence.* `_Observers` keeps the sites whose writers pass apart from
  `self.sites` (`import_sites`, with their callers), and `_seeing_sites`
  adds them back for changes to code that runs at import.

**Trade-off.** This narrows selection. A store one test leaves for a later
one (a write without `monkeypatch`, never undone) is outside the record of
the later test. That is the isolation assumption again, with the
reverse-order collection as its detector. An external library that stores
a project value it was handed is not seen today either: no write is
detected, so the lookup was already bounded.

**Done when**
* scenarios cover these cases:
  * a library `dir(builtins)` reached by many tests is no longer
    selected for an unrelated added name (selected before: the narrowing
    scenario);
  * a test whose own `monkeypatch.setattr(builtins, ...)` writer, or a
    value it stores, changes is still selected;
  * the rule is off for a write at import (module level, or a function
    called at import) and for one in a hook;
  * a writer newly run at import in the head revision still reaches the
    site, from a library variable, a test module's variable, a helper
    module importing a test's writer, library code calling it by name, and
    a registry holding it (each missed by an earlier version of this rule);
  * a library module's import-time change reaches the site when a library
    writer exists and not when only tests write;
* static plans are unchanged (scenarios assert the static selection);
* `0da0faf7` on the recording at its parent drops the `lookup_site`
  selections that came from these sites.

## 15. Recording inside child processes (strata trial)

**Status.** Implemented (2026-10-09), unreleased. A recording of
strata's unit suite at `a54a387` flags 28 of 5 817 tests for a subprocess,
down from 608. A body-only edit of a library function a few dozen tests
run now selects 61, 91 and 88 tests (three functions), down from 639, 669
and 640: the flagged tests no longer join every plan. `0da0faf7` itself
moves little (1 921 to 1 901): its change adds names, and the harness
runs each cell with `exec` (`harness._exec_with_display`), a lookup site
that sees every namespace, so the cell-running tests are selected through
it instead. A function only the children run, broken
(`_exec_with_display` raising on `print(` cells), fails 85 tests in a
full run; the plan selects 505 (8.7 %) and `diffcone check` finds no
miss. Two fixes came from the first strata recording: the installed-copy
check matched the checkout's own directory name (strata lives in a
directory called `strata`), and `uv sync`, which the harness tests run,
flagged until package managers' commands were accepted (`child.TOOLS`).
The recording took 1 072 s, against 486 s for an earlier one on a less
loaded machine; children now record themselves, and CI will say what
that costs.

The sketch, as first written: after items 10, 13 and 14, strata's
floor is the tests that start a Python subprocess: the notebook harness
(`uv run --directory <notebook> python harness.py <manifest>`), warm pool
workers (`python pool_worker.py`), and fixtures that start them (some
1 270 tests). Each is flagged (`FLAG_SUBPROCESS`) and selected for every
change. A probe (a logging `sitecustomize` on `PYTHONPATH`, 133 notebook
tests) saw 263 child interpreters, all Python 3.13, every one of them
loading the `sitecustomize` through the inherited `PYTHONPATH`, 262 of
them during a test, and running checkout code (`harness.py`) with
strata's or a notebook's virtual environment.

**Mechanism (POSIX; Windows keeps the flag).**

* *Starting.* The directory `diffcone` puts on `PYTHONPATH` for the
  plugin also holds a `sitecustomize.py` and a stdlib-only child recorder.
  A Python process that starts with `DIFFCONE_COLLECT_PARENT` in its
  environment (set by the plugin at start, so the pytest process itself
  never sees it at its own start-up) records itself: `sys.monitoring`
  `PY_START` with DISABLE, never re-armed, over the checkout root, and
  the same audit hooks for paths. It then imports any other
  `sitecustomize` on `sys.path`, which ours shadowed. An xdist worker is
  a pytest process: the plugin stops the child recorder there and drops
  its file. A child on Python before 3.12 writes only a header saying it
  cannot record.
* *Records.* Each child streams `children/<ppid>/<pid>-<start>.jsonl` in
  `DIFFCONE_COLLECT_OUT` with unbuffered appends: a header (pid, ppid,
  `sys.orig_argv`, start time), one line per new code object or path,
  each accounted spawn (below) with the child's pid, each fork's pid
  (`os.register_at_fork`), each unaccounted spawn as a flag, an installed
  copy of a project package as a flag, and an exit line at `atexit`. A
  child killed by a signal leaves everything but the exit line, since
  nothing is buffered.
* *Accounted spawns.* `subprocess.Popen._execute_child` is wrapped. A
  spawn inside a test or shared fixture window is accounted when it goes
  through it: its own audit events (`subprocess.Popen`, the
  `os.posix_spawn` it may use) do not flag, and the window records the
  child's pid and spawn time. Everything else (`os.system`, `os.spawn*`,
  `os.posix_spawn` called directly, `multiprocessing`, a spawn at import
  or in a hook) flags as today, and so does an inert probe not flag.
* *Lifetime.* At each window open, the window is credited with every
  accounted spawn of this process still alive: its pid, or a pid in its
  records' subtree, answers `os.kill(pid, 0)` (a zombie or a reused pid
  counts as alive, which only over-credits). A record with a fork line,
  or a child record its parent did not list, makes the spawn alive until
  the process ends.
* *Fold.* A window's spawn resolves to the child records whose pid is
  the spawned pid (it was a Python process), or whose ppid is the spawned
  pid when the spawn's command line is a launcher (`uv run [options]
  <command>`) and one of them has that command as its `orig_argv`. Each
  record brings its own accounted spawns, recursively. The window gets
  the union of their code objects (mapped to symbols with each record's
  own table, as a process's are), paths and directories. A spawn that
  resolves to nothing, a launcher whose command is not among the records,
  a header that could not record, or a flag line anywhere in the subtree
  sets `FLAG_SUBPROCESS` on the window, as today. So does a record that
  ran project code from an entry the index does not hold (`-c`, standard
  input, a script outside it): that code may read any project name, as an
  `exec` would, but no site of the index stands for it. A Python record
  vouches for a spawn only when the spawn's own command line started it
  (a shell's `exec python` ran other commands first), and a package
  manager's command (`uv sync`, `uv pip`, ...) is followed without
  needing a record.

**Trade-off.** It narrows selection: a test that started a recorded child
is selected through what the child ran instead of for every change. The
child's whole life is credited to every window it was alive in, so a
long-lived server or a warm worker reused by later tests is credited to
each of them (over-crediting, never under). A non-Python program is
accounted only as a named launcher whose own work is reading build inputs
(any change to which selects everything already). A child's environment
(a notebook's virtual environment) is not checked at planning time: a
drift there with no change in the pull request is outside any plan, as it
is for the parent's dependencies. The recorder adds an environment
variable to the test process (`DIFFCONE_COLLECT_PARENT`), which a test
comparing `os.environ` whole would see.

**Done when**
* scenarios cover these cases:
  * a test running `sys.executable -c`/a script under the checkout is
    selected by a change to code only the child ran, and not by an
    unrelated change (selected before);
  * a launcher (`uv run python ...`, faked by a script named `uv`) is
    accounted; another program (`sh -c "python ..."`) still flags;
  * a child that strips the environment (`env={}`), runs `python -I`, or
    is killed before writing still flags;
  * a warm child started by one test and used by a later one credits the
    later test (missed by a spawn-time-only attribution);
  * a child of a child is followed; a forked grandchild keeps the spawn
    alive;
  * a shared fixture's child credits every test using the fixture,
    across xdist workers;
* the recording of strata's unit suite loses the flag on the harness and
  pool tests, and a replay of `0da0faf7` on it shows the drop;
  `diffcone check` against full runs finds no miss.

## 16. The environment's own code and its metadata scans are not the project's (strata trial)

**Status.** Implemented (2026-10-09), unreleased. A recording of strata at
`edc32ce0` through `uv run pytest` (CI's command) holds no path touched
outside every test without a module to credit it to; recorded the old way
it held the checkout root, `tests`, every collected test file and the
conftests. #1085's edit replayed on it selects 582 of 6 231 tests with no
fallback (CI ran all 6 231), the edited file's tests among them; an added
test file selects 175 (the `exec` lookup sites of item 15 and the
subprocess-flagged tests), not 6 232.

The sketch, as first written: every strata pull request that edits a
test file has selected the whole unit suite since strata's #1082, which
added `pytest_sessionstart`/`pytest_sessionfinish` hooks calling
`importlib.metadata.distributions()`. #1085 (one parametrize list and a
two-token library fix) ran 6 231 of 6 231 under `unobserved_file_changed`:
"project code touched tests/test_table_uri_errors.py outside every test".
Two causes, both reproduced on a scratch clone at `edc32ce0`:

* *The console script is in the checkout.* CI runs `uv run pytest`, so
  the outermost frame of the pytest process is `.venv/bin/pytest`, a file
  under the checkout root and not under `site-packages`. `_actor` walks to
  it and calls every touch of the process "project": pytest stat'ing its
  arguments and conftests during start-up, pluggy scanning entry points.
  `python -m pytest` has no such frame, which is why recordings made that
  way never showed it.
* *Metadata scans list the checkout.* `distributions()` stats and, when a
  directory's mtime has moved since its cached scan, lists every
  `sys.path` entry (the checkout root, `tests`, the source roots). From a
  conftest hook that is project code outside every test, so an added file
  anywhere under a listed directory selects everything.

**Mechanism.**

* *The environment.* `_relative` (the plugin and the child recorder)
  returns None for a path under `sys.prefix`, `sys.exec_prefix` or
  `sys.base_prefix`, not only under `site-packages`: an environment kept
  in the checkout (`.venv/bin/pytest`, `.venv/lib/...`) is installed code,
  as `site-packages` already is. A child running another environment
  (a notebook's `.venv`) excludes its own prefix.
* *Metadata scans.* A stat or listing of a `sys.path` entry (normalised;
  `""` is the current directory) made with `importlib.metadata` (or the
  `importlib_metadata` backport) on the stack, inside it from the touch
  up to the first project frame, is not recorded. Such a scan reads only
  the names of `*.dist-info`, `*.egg-info` and `*.egg` entries and the
  directory's mtime; reads of files inside a distribution are still
  recorded.
* *What the scan could have seen.* `build_input` also matches any path
  with a component ending in `.dist-info` or `.egg-info`: a committed
  distribution's metadata changed, appeared or disappeared decides what
  `importlib.metadata` finds, so the plan selects everything, as for a
  lock file.

**Trade-off.** Both narrow. The first is exact: code under the
environment's prefix is not the project's code, whoever runs it. The
second drops what a scan could learn from a directory's other names,
which only distribution metadata decides, and the planner rule keeps
that sound. A project that commits a virtual environment's scripts to
version control and edits them (no known case) would lose their reads.

**Done when**
* scenarios: a recording run through a console script inside the
  checkout (a fake `.venv/bin/pytest`-style entry) does not select every
  test for an edited test file; a session hook calling
  `distributions()` does not select every test for an added test file;
  a committed `*.dist-info`/`*.egg-info` change selects every test; each
  fails on the old code where it narrows;
* a recording of strata at `edc32ce0` with `uv run pytest` has no test
  file under `import_paths` with no module, and #1085's edit replayed on
  it plans without a fallback.

## 17. Targets the project always runs (`always_run` in `diffcone.toml`)

**Status.** Sketch (2026-10-09). Some tests are not worth planning: an
end-to-end suite that drives the whole product (strata's E2E job, its
notebook-harness tests, which run cells through `exec` and so are
selected by nearly any change anyway). Running them on every pull request
is the project's decision, and today it can only make it outside
diffcone: a separate CI step running them in full, which the diffcone run
then duplicates or must be told to skip by hand. Without either, strata's
E2E job selected 0 of 59 tests for a dependency-only pull request.

**Mechanism.**

* *Declaration.* `diffcone.toml` takes `[[always_run]]` tables:

  ```toml
  [[always_run]]
  targets = "tests/notebook/*"   # fnmatch on the runner id; * crosses / and ::
  runner = "pytest"              # optional: only this runner's targets
  why = "notebook runs are end to end"
  ```

  Read from both snapshots, like `[[edges]]`: one the change deletes still
  counts for that plan. An unknown key, a missing or empty `targets`, a
  non-string value, or a head-snapshot entry that matches no target (a
  typo, a moved directory) is an analysis error, so the plan selects
  everything rather than quietly planning tests meant to always run.
  Base-only entries matching nothing are not errors (the change removed
  those tests and the entry together).
* *Planning.* After either planner (static or evidence) has decided, each
  target an entry matches is selected, with an `always_run` reason giving
  the pattern and `why`. Matching runs on the targets the plan holds
  (manifest and discovery), so the check for unmatched entries runs before
  planning, where an analysis error can still force select-all.
* *Report.* The JSON report lists the entries (`always_run`, beside
  `declarations`) and counts the matched targets
  (`analysis.counts.always_run`); the text report lists them. `diffcone
  report` computes a job's selected share over the targets not always run,
  so the share measures what diffcone decided.
* *Recording.* `collect` still runs them. Its run is also the full test
  run of a push to the main branch in the CI setups, so dropping them
  there would leave them unrun; a recording of a test always selected is
  simply never read.

**Trade-off.** It only widens selection. A broad pattern costs CI time,
which is the project's choice and visible in the report.

**Done when**
* scenarios (pytest and ASV targets): an entry selects its matching
  targets on a change that reaches none of them, in static and evidence
  mode; `runner` restricts it; a head entry matching nothing, an unknown
  key and a missing `targets` are analysis errors that select everything;
  a base-only entry matching nothing is not; the JSON report carries the
  entries and the count, and `diffcone report`'s share leaves them out;
* the reference page for `diffcone.toml` documents it.

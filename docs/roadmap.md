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
build, the whole suite under `-n 8`):

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

**Not covered.** Edits outside Cython function bodies (38 of the 86
compiled commits in the spike), `.pxi.in` templates, hand-written C and
build files still select everything; telling additive module-level edits
apart is the next gain. A module the store holds no Cython record of
(built without profiling) selects everything for its edits.

**Trade-off.** Any miss here is a miss in compiled code, which coverage
validation does not see. The rule ships only if the spike shows no miss,
or a guard that closes each one (the `nogil` caller rule is the first).

**Done when.** The spike has run, and either the rule is shown sound on
the recent compiled edits with its savings stated, or the gap is measured
and the item is closed as measured and left alone, like items 0 and 1.

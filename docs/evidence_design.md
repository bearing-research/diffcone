# Execution evidence: design proposal

Status: **proposed, not implemented**, apart from step 0, which has
shipped. The spike on pandas passed its go/no-go ("Spike results"). Four
of the five decisions at the end are answered.

## Why

Static planning cannot help pandas (evaluation.md, "pandas: the primary
target"). A change almost anywhere selects 26 026 of 26 027 targets, and the
measurements show why no static rule changes that. Untyped calls
(`x.round()` may be any `round`) tie every symbol to every test.
Annotations would type a third of them and move nothing. And even with
perfect typing, about 40 % of library symbols are reached from a
1 742-symbol core around `DataFrame` that every test enters. Telling which
test's DataFrame takes which path needs to know what each test actually
does, and only running it says that.

This design adds one fact that reading the code cannot supply: **which
functions each test executed**, recorded in a real run. Static analysis
keeps doing what it does well: it defines what changed, and for changes
that are not plain body edits it says which code *observes* the change. It
stops being asked for transitive reach, which is where pandas explodes.

## Step 0, independent of all this: files the index does not read

A commit that changes only a non-Python file gets an empty, complete plan
today. That breaks the governing rule, and it was verified on a fixture
repository: changing a JSON file that the code reads, or a Cython `.pyx`
module, gives no changes, no fallback, no selection and exit 0. In pandas'
last 81 commits, 7 (9 %) touch compiled sources (`.pyx`, `.pxd`, `.c`,
`.h`) and 7 touch no `.py` file at all.

The static fix is a fallback: a changed file under a source root that the
index does not read selects every target (`unanalysed_file_changed`). The
open question is only which files count (decision 1). Execution evidence
answers it precisely for data files (see "Files"), but static mode needs
its own answer, and this should ship first whatever is decided about the
rest.

## The idea, and why it is sound

Collect, at some commit C, the set of symbols `X(T)` each test T executed.
To plan C → head, compute E: the symbols whose execution would *notice* the
change. Select T when `X(T) ∩ E ≠ ∅`.

**The argument (first divergence).** Run T at C and at head, same
environment, same inputs. The two runs execute the same instructions until
the first point where they differ. At that point T is executing some code
g, and either g itself changed, or g read something that changed (a
variable's value, a lookup in a namespace that gained or lost a name, a
signature, a file). Everything before that point was identical, so the
run at C executed g too: `g ∈ X(T)`. So if E contains every changed symbol
*and every reader of every changed thing*, a test whose `X(T)` misses E
runs identically at head and cannot be affected.

Three things make it concrete:

* **Readers are one hop, not a closure.** Static analysis gives E by
  following edges one step back from what changed, and coverage replaces
  everything further out. One hop is what stays small in pandas: the
  explosion lives in the closure.
* **The argument needs assumptions static mode does not**: the run is
  deterministic, T does not depend on what earlier tests left behind, and
  the environment is the same. Each has a guard or a detector below, and
  what remains is stated, not hidden (decision 2).
* **It cannot say anything about what it did not observe**: code executed
  outside the test's window, code in compiled extensions, subprocesses.
  Those fall back to static planning or to selecting the test.

## What is recorded

A pytest plugin shipped inside diffcone (stdlib only). It is loaded into
the project's own test process with `-p`, and diffcone's source directory
is put on `PYTHONPATH`, so nothing is installed into the project. It
records per test node id, folded to the target id the way `validate`
folds parameter cases:

| record | what | mechanism |
|---|---|---|
| `X(T)` | symbols executed during T's setup, call and teardown | `sys.monitoring` `PY_START` (Python 3.12+), disabled per code object after its first hit and re-armed per test with `restart_events()` |
| fixture shares | setup/teardown symbols of every non-function-scoped fixture instance, credited to *every* test that uses it, not just the first | a hookwrapper on `pytest_fixture_setup`/`pytest_fixture_post_finalizer` records each instance's window; `item.fixturenames` links tests to instances |
| `X_import` | symbols executed outside every test window (imports, collection, conftest bodies, session hooks) | the same tracer, outside windows |
| import attribution | for each symbol run by an import, which modules' imports ran it (the innermost module whose top-level code was running) | a stack of modules pushed at a `<module>` code object's `PY_START` and popped at its `PY_RETURN` (a local event) or `PY_UNWIND` (an import that raises, such as `pytest.importorskip` at module level); events re-armed at each push and pop |
| `F(T)` | repository files T opened | an audit hook on `open` (`sys.addaudithook`) |
| `P(T)` | T started a subprocess | audit hooks on `subprocess.Popen`, `os.exec*`, `os.posix_spawn`, `os.fork` |
| environment | Python version, platform, installed distributions and versions, `PYTHONHASHSEED`, `TZ`, pytest plugins and options | read inside the test process at session start |

The tracer starts when the plugin module is imported, not in
`pytest_configure`: `-p` plugins load before the initial conftests, and a
root conftest usually imports the project, whose import-time code (for
pandas, its decorators and registrations) would otherwise go unrecorded.

**Names looked up dynamically are not recorded.** The spike wrapped
`getattr` and `hasattr` to record them, and that changed test outcomes: the
wrapper's frame moves the `stacklevel` of warnings, and pandas checks it.
No pure-Python wrapper avoids that frame, so the name-based rows below use
the static stand-in: the tests that *executed* an unbounded lookup site
that can see the namespace in question.

Code objects map to symbols by file and first line, against diffcone's own
index of commit C (innermost symbol whose line range contains the code
object's first line). Nested functions, lambdas and comprehensions land in
their enclosing symbol; a module's own code maps to the module symbol.
A code object from a file outside the checkout that has the same
module path as a file inside it means the suite ran an installed copy. That
makes the whole collection invalid, and it is refused, using the check
`validate` already does (`shadowed_files`).

The monitoring tool id is claimed with `sys.monitoring.use_tool_id`. If the
id is taken (coverage.py holds id 1), the collection fails loudly rather
than recording nothing.

## What a change is observed by (E)

For each change diffcone classifies between C and head:

| change | E, what is selected | escalates to static |
|---|---|---|
| body changed, `f` | `{f}` | when a library or conftest import ran `f`: its result is baked into objects built at import. When a *test* module's import ran it, the tests that executed that module's code instead |
| docstring changed | the doctest target (as today) + static readers of `__doc__` (`inspect.getdoc`, `.__doc__`) | when read at import (`@doc`, `Appender`) |
| signature or defaults changed | `{f}` + its static callers (resolved references and name matches of its name) + introspection sites (`inspect.signature`) | — |
| decorators changed | as a signature change | library code: always, since decorators run at import and may register. Test code: never, since what a test's decorators do reaches only that test |
| added or deleted name `n` in module or class N | static readers of `n` (resolved, imported, name-matched), enumeration sites (`dir`, `vars`, `__dict__`, `inspect.getmembers`, star imports), the symbol itself, and the unbounded lookup sites that can see N: for a method, reads off objects from elsewhere; for a module-level name, reads off modules and `globals()`/`vars()`. For test code, only lookup sites in test code: nothing else holds a test module or a runner-only test instance | — |
| added or deleted fixture | the tests in its visibility scope that request that name: a new fixture can shadow a conftest one they used (static discovery knows which) | — |
| added, deleted or changed pytest hook (`pytest_*` in a conftest or plugin) | everything | — |
| added or deleted module | static readers (importers) | — (only code that names it reaches it, and that code changed too) |
| added or deleted special method (`__eq__`, `__len__`…) in class C | members of C's hierarchy + static readers of C and of its subclasses (whoever builds or checks an instance) | — |
| variable value changed, `v` | static readers of `v` (by name too, which covers `getattr(obj, "v")` with a literal), enumeration sites | when a library import reads `v` |
| class structure changed (bases, metaclass, class decorators, class body) | every member of C, its bases and its subclasses + static readers of those classes | class decorators and metaclasses: always |
| module-level statements or imports changed | — | always: they run at import |
| a test or fixture changed | selected (`changed_target`, as today) | — |
| changed file the index does not read | tests with the file in `F(T)`, or with code from it in `X(T)` (a `.py` outside the source roots) | when opened or executed at import; compiled sources always (see below) |

"Escalates to static" means that one change is planned statically and the
rest of the commit still uses evidence; selection is the union over all
changes. The static half runs **without dynamic seeds**. A dynamic
reference a test executed is in its record through the code it reached, so
the executed lookup sites that can see the escalated module are added to E
instead: closure-bounded ones whose module's import closure holds it, reads
off objects from elsewhere, and, for a library module, imports named at
run time. A test module's import-time objects reach library code only
while its own tests run, and those tests are selected, or through its
importers, which static planning follows. So a test module escalates to its
own tests plus static reach, not to every lookup site. Without that, one
unbounded import in pandas (`_get_plot_backend`) made every escalation
select everything.

**Compiled code.** A `.pyx` change is invisible to a Python tracer. The
first version escalates it. The follow-up maps a changed `.pyx`/`.pxd` to
its extension module and follows `cimport` edges among the extension
modules. It then selects the tests that executed a Python function
referencing any affected module. That is sound only once a test's use of
extension *objects* is covered too (a `Timestamp` returned by one extension
and used through another), so it needs its own measurement before it
replaces escalation.

**Targets without evidence** (added since C, or never collected) are
selected. **ASV targets** have no evidence in the first version and keep
static selection.

## The assumptions, and what guards each

| assumption | what breaks it | guard | detector |
|---|---|---|---|
| same environment | a different numpy, a missing optional dependency, another Python | the environment fingerprint must match the run; if it doesn't, fall back to static | — |
| determinism | hash-seed-dependent ordering, time, randomness | `PYTHONHASHSEED` pinned to the recorded value in `run`; `TZ` in the fingerprint | optional second collection in reverse order: tests whose `X(T)` differs are **unstable** and always selected |
| test isolation | a value one test computes and caches is served to a later test, which then never executes the computation | `functools.cache`/`lru_cache` caches (23 in pandas) cleared before every test during collection; shared fixture instances credited to every user (8 of pandas' 1 051 fixtures) | the reverse-order collection also catches order dependence between two tests |
| complete observation | subprocesses, threads outliving their test, code run from strings | `P(T)` → always selected; strings run by `exec` belong to the function that ran them, which is in `X(T)` | — |
| fresh evidence | evidence from an old C | changes are always C → head, so older evidence selects more, never less | age reported |

What remains is stated in the report and in AGENTS.md: a lazily computed
value on an object shared by three or more tests (pandas'
`cache_readonly`, 171 uses, on an object shared across tests), time- or
network-dependent paths, and state kept in module-level dictionaries.
These can make a test's recorded path shorter than its real one. A
periodic full run under collection (nightly in CI) detects them after the
fact. Every test that fails there but went unselected in the runs since is
reported as a miss.

## Storage and lifecycle

`.diffcone/evidence/<commit>-<environment hash>.sqlite`:
`meta` (commit, environment, command, diffcone version, time), `symbols`
(index to symbol id), `sets` (deduplicated compressed bitmaps over the
symbol table: parametrized tests share sets), `tests` (target id, set id,
flags: subprocess, unstable), `names`, `files`, `import_phase`. Expected
size for pandas: about 26 000 targets over about 37 000 symbols, which is
tens of megabytes before deduplication. That has to be measured (spike).

**Advancing without a full run.** After `run --collect` executes the
selected tests under the plugin at head, the store can be written for head
without running the rest. By the argument above, an unselected test runs
identically at head, so its C record *is* its head record. Selected tests
get their fresh head records. That is how evidence stays fresh without a
full suite per commit, and the nightly full run resets any drift.

## Interface and architecture

* `diffcone collect --command CMD [--reverse-order]`: runs the whole suite
  under the plugin at a clean checkout of HEAD and writes a store.
* `diffcone plan ... --evidence auto|PATH`: uses the newest store whose
  environment matches. Evidence is off by default: static mode stays the
  default and the reference.
* `diffcone run ... --evidence auto [--collect]`: as today; with
  `--collect`, advances the store to head.
* `diffcone evidence`: lists stores, their commits, environments and ages.

Layers: the plugin (`src/diffcone/collect.py`) is new, and it runs *inside*
the project's process. It must never change an outcome: no output, and
every exception is caught and recorded as a collection error, which
invalidates the store. `execution.py` stays the only module that starts
project code. The planner gets the store as an explicit `Evidence`
structure keyed by target id. It stays free of pytest imports and treats
evidence like a manifest: data that a runner integration produced. `plan`
still never runs project code.

**Report** (`schema_version` 3): `analysis.evidence` gives the store's
commit, environment, age, and C → head changes that came from commits
outside base..head. New reason rules each carry what was observed, never
an inferred path:

* `executed_changed`: "T executed `f` in the evidence run at C; `f` changed".
* `executed_reader`: "T executed `g`; `g` references `v` (edge); `v` changed".
* `looked_up_name`: "T looked up `n` dynamically; `n` was added to N".
* `opened_file`: "T opened `pandas/tests/io/data/x.csv`; it changed".
* `escalated`: "`f` ran at import: static planning for this change".

Plus fallbacks: `no_evidence`, `unstable`, `subprocess`,
`environment_mismatch`.

## How it will be checked

1. **Spike (go/no-go): done, and it passed.** See "Spike results" below.
2. **Scenarios** for every row of both tables above, mixing pytest and ASV
   targets (ASV falling back to static), each asserting exact targets and
   reasons: a change seen only through a dynamic name, through an opened
   file, through import-time code, through a shared fixture, a
   subprocess-spawning test, an unstable test, an environment mismatch,
   a target with no evidence.
3. **Recall on pandas.** Collect at an older commit C0 and plan C0 → c for
   recent commits c. Run the whole suite at each c with `validate
   --coverage`: every test that executes a changed symbol at c, and every
   test whose outcome changes, must be selected. **Done when** that holds
   on at least 20 pandas commits, and the 28-repository corpus re-planned
   with evidence shows no miss and states its savings against static.

## Spike results (2026-09-24)

A prototype recorder and analysis (`scripts/evidence_spike/`, kept as the
starting point, not as the implementation) ran on
pandas at `3f57341`. The recorder is the table above, minus names; pandas
was built in place with Python 3.13, with pandas' CI marker
filter (`not slow and not network and not db and not single_cpu`) on
8 workers.

**Cost.** 194 s traced against 155 s plain: **1.25×** wall time, 1.49× CPU.
The outcomes were identical: 187 615 passed and the same 13 failed. The
record is 27 MB for 22 258 folded tests. A test executes a median of 134
pandas symbols (p90 328) of the 37 307. `test_round` executes 121, where
static reach said all of them.

**Selection.** The 79 commits before `3f57341` were replayed against that
record, with E and escalation as in the tables above. Evidence at the head
stood in for evidence at each parent, which estimates size but is not a
recall claim.

| selection | commits |
|---|---|
| under 1 % | 10 |
| 1–5 % | 20 |
| 5–25 % | 19 |
| 25–75 % | 7 |
| 75–100 % | 23 |

Median **6.9 %** (p25 4.4 %, p75 84 %), against 100 % for static planning on
every one of them. Of the 23 commits over 75 %: 11 escalate a library change
(module-level edits in `pandas.core.frame` and `pandas.core.missing`,
changed class decorators, library code run by the root conftest), 8 change
a compiled source, a template or a lockfile, and 4 are broad on evidence
alone (an added library method must count every read off an object from
elsewhere; a `DataFrame` class change takes all its members).

**What the spike corrected in this design** (each is folded into the
tables above): names cannot be recorded by wrapping; the tracer must start
at plugin import; imports that raise end with an unwind; escalation must
leave dynamic seeds to the evidence; test code's decorators and added test
functions do not escalate; added modules do not escalate. It also found
two rows the design lacked: fixture shadowing and pytest hooks.

**Not measured by the spike**: recall (step 3), the bases and subclasses
of a changed class (members of the class itself only), fixture shadowing
and hooks, subprocess and unstable tests, and ASV.

## Decisions

Answered (2026-09-24):

1. **Step 0: every file.** Any changed non-Python file under a source root
   selects everything, including a `README.md` under a `.` root. It shipped
   as the `unanalysed_file_changed` fallback (design.md), and its cost on
   the recorded corpora is in evaluation.md.
2. **The assumptions: opt-in only.** Evidence mode is accepted with its
   stated residuals as an explicit opt-in. Static stays the default, and
   the nightly miss audit is part of the mode, not advice.
3. **Scope** follows from 2: runtime tracing moves into scope for the
   collector only, when the mode is implemented. AGENTS.md changes with
   that implementation, not before.
4. **Collection needs Python 3.12+** (`sys.monitoring`). There is no 3.11
   fallback; a collection on an older interpreter is refused.

Open:

5. **Where collection runs.** Nightly in CI is the natural producer for
   pandas, and developers would download that store. That makes the
   environment fingerprint the thing to get right. Local-only collection
   is simpler, but it costs every developer a full traced run. The spike
   measured 1.25× a plain run for pandas (3 minutes on 8 workers), which
   makes local collection affordable. The fingerprint question stays open.

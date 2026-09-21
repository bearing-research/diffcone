# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Diffcone is a static-first, function-level change-impact engine for Python. It maps changes in application code to affected tests and benchmarks (pytest and ASV are the first runner integrations).

Milestone 1 (`diffcone plan` over two committed revisions with a target manifest), milestone 2 (static pytest/ASV discovery) and milestone 3 (`INDEX`/`WORKTREE` snapshots) are implemented. `docs/diffcone_coding_agent_handoff.md` is the original spec; `docs/design.md` documents the rules as implemented; `docs/evaluation.md` records corpus results on real repositories (re-run it when changing selection rules); `AGENTS.md` holds the scope boundaries. `docs/roadmap.md` carries a design sketch (mechanism, trade-off, done-when) for every planned item; write or update the sketch there before implementing something new, and drop items once they ship. Stdlib only, no runtime dependencies.

## Commands

```bash
uv sync
uv run diffcone plan --repo . --base <rev> --head <rev> \
    --discover pytest --discover asv [--targets targets.json] \
    --source-root src --source-root . --format json|text
uv run diffcone discover --repo . --rev HEAD --discover pytest -o targets.json
uv run diffcone run --base main --head WORKTREE --discover pytest --command "uv run pytest" [--dry-run] -- -x
uv run diffcone validate --base main --head HEAD --discover pytest --command "uv run pytest" [--coverage]
uv run diffcone corpus --range main~10..main --discover pytest --command "uv run pytest" --coverage
uv run python scripts/census.py run --work /tmp/census -o census.json  # plan-only census
uv run python scripts/census.py report census.json
uv run pytest                                   # all tests
uv run pytest tests/test_scenarios.py -k alias  # one scenario
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
```

Exit codes: 0 complete, 1 degraded (analysis errors forced select-all), 2 no plan.

Module names come from the longest matching source root: with roots `src` and `.`, `src/calc/ops.py` is `calc.ops` and `tests/test_x.py` is `tests.test_x`. Do not document commands or manifests in README or here that are not actually wired up and verified.

## Code layout

- `src/diffcone/snapshot.py` reads a commit from git objects (`ls-tree` + `cat-file --batch`), the staged `INDEX`, or the `WORKTREE` from disk (via `ls-files --exclude-standard`); the snapshot `kind` travels through the index into the report.
- `src/diffcone/indexer.py` parses modules, assigns symbol identities, hashes bodies/definitions, resolves references into `Edge`s and records `UnresolvedReference`s. This is where the supported subset lives.
- `src/diffcone/classify.py` diffs two indexes into `SymbolChange`s (added, deleted, body_changed, definition_changed, dependencies_changed).
- `src/diffcone/planner.py` builds the union graph of both revisions, adds target nodes and conservative edges, runs the backward search with the propagation rules in its docstring, and produces `Decision`s with `Reason` paths and `Fallback`s.
- `src/diffcone/report.py` renders JSON (`schema_version` 2) and text.
- `src/diffcone/execution.py` is the only module that executes project code, and only from `run`/`validate` after a plan exists; keep it that way.
- `src/diffcone/discovery/` turns the head snapshot into targets without importing project code: `pytest_static.py` (config, collection rules, fixture chain) and `asv_static.py`. Each module's docstring is the authoritative list of what it models; keep it in sync with `docs/design.md`.
- `src/diffcone/testing.py` is the public scenario-test toolkit: `FixtureRepo` (throwaway git repo built from dicts, `commit`/`plan`/`git`/`try_git`), target constructors and plan assertion helpers. Tests import from `diffcone.testing`, never from other test files; `tests/conftest.py` only defines the `repo` fixture.

## What `diffcone plan` is

The deliverable is an **explainable selection plan**. The command does not execute or deselect targets, does not run project code (discovery included), and does not modify the working tree. It compares two snapshots: git revisions, `INDEX` (staged) or `WORKTREE` (on disk). The report must always state which kind was read (`analysis.<side>.kind`, `uncommitted_analyzed`); never let a working-tree analysis look like a committed one.

Targets are manifest records (`runner`, `runner_id`, `entry_symbol`, `lifecycle_dependencies`), hand-written or produced by static discovery. ASV setup functions and pytest fixtures are both expressed as lifecycle dependencies on the target. Discovery must document exactly which collection rules it reproduces and report, not guess, everything else; unknown fixtures become `fixture:<name>` so the planner selects conservatively.

## Architecture (keep these layers separate)

```
Git snapshot reader
  -> Source index and dependency resolver
  -> Change classifier
  -> Impact planner
  -> Human-readable and JSON reports
```

- The planner operates on explicit in-memory data structures and must not import pytest or ASV. The engine is runner-independent; runner names are just labels on targets.
- The only persistent state is the cache under `.diffcone/cache/` (`src/diffcone/cache.py`): whole indexes per commit (keyed by commit, source roots, `INDEX_FORMAT` and a fingerprint of the indexer's source and Python version; never used for `WORKTREE`/`INDEX`) and per-module facts and resolution outputs in `modules.sqlite` (keyed by file content and an environment fingerprint; used for every snapshot kind). Both must yield a byte-identical plan to a miss: every indexer unit test and scenario fixture is also run cold- and warm-cached and compared. Anything the indexer reads from other modules during resolution must be part of the environment fingerprint (`Indexer._environment_fingerprint`, `_facts_to_dict`'s `env`), and every write in pass 2 must go through `self.out`. Bump `INDEX_FORMAT` when the index's meaning changes; the fingerprint covers ordinary edits.
- JSON report must distinguish: analyzed snapshots and scope, changed symbols, selected targets, unselected targets, dependency explanations, unresolved relationships, fallback decisions, analysis errors.

## Non-negotiable analysis rules (see docs/design.md for the precise propagation table)

- **Analyze both revisions.** Deleted functions, removed calls, and changed aliases must stay in consideration even if absent from the head graph.
- **Stable symbol identity.** Identity is the qualified symbol (module + function/method), never source location. Inserting blank lines above a function must not change its identity or count as a body change.
- **Body changes vs. broader changes.** Class structure and class bodies, decorators, defaults, removed or redirected imports/dependencies, and other definition-time changes invalidate every member (structural); pure additions of imports or dependencies are not structural. Module body changes reach only members and importers that reference module state, plus targets that declare the module as a lifecycle dependency. This trade-off is documented in `docs/design.md`; do not silently move it in either direction.
- **Unknown != unaffected.** Unresolved relationships are represented explicitly. When impact cannot be bounded, select all supplied targets and report the fallback rule.
- **New or changed targets are always selected**, even with no dependency edges.
- **Every selection reason maps to a real dependency edge or an explicit fallback rule.** Never fabricate call paths.
- **An analysis error must not become a successful empty selection.**
- Method identity and method-call resolution are separate: recognizing `Class.method` as a symbol does not mean `obj.method()` calls are resolved. Dispatch is modelled only for `self`/`cls` receivers, as the MRO hit plus in-scope overrides; there is no type inference, no dispatch on receivers of unknown type, no branch- or argument-sensitive analysis, runtime tracing, or ML ranking. Unsupported constructs stay visible in the output.

Supported subset: module-level functions and methods; direct statically resolvable calls/references; ordinary imports and imported-name aliases within configured source roots; transitive dependencies; manifest-declared target and lifecycle dependencies.

## Testing expectations

Acceptance tests use small git repository fixtures with before/after commits. Assert **exact target sets and reasons**, not just that the command succeeds. Every scenario should mix pytest-labeled and ASV-labeled targets to prove runner independence. The required scenarios are tabulated in the handoff doc (independent functions in one file, transitive consumers, aliased imports, shared setup change, deletion/redirect, new/changed target, unresolvable relationship, blank-line insertion).

Any change that **narrows** selection must come with a regression scenario.

## Docs to maintain

`README.md`, `docs/design.md`, `docs/roadmap.md`, and `AGENTS.md` are required by the milestone. Always separate implemented functionality from planned functionality.

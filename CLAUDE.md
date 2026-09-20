# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Diffcone is a static-first, function-level change-impact engine for Python. It maps changes in application code to affected tests and benchmarks (pytest and ASV are the first runner integrations).

**There is no code yet.** The repository contains only the license (MIT, Bearing Research), a stub README, and `docs/diffcone_coding_agent_handoff.md`, which is the authoritative spec for the first milestone. Read that document in full before implementing anything; the rules below summarize it but do not replace it.

## Tooling

Use Python and `uv`. Keep dependencies modest. Once a `pyproject.toml` exists, the expected workflow is:

```bash
uv sync                      # install
uv run diffcone plan --base <rev> --head <rev> --targets <manifest> --format json
uv run pytest                # all tests
uv run pytest tests/test_x.py::test_name   # single test
```

Do not document commands in README or here that are not actually wired up.

## First milestone: `diffcone plan`

The deliverable is an **explainable selection plan**. The command does not execute or deselect targets, does not run project code, and does not modify the working tree. It compares **two explicit committed git revisions** only; output and docs must not imply uncommitted changes were analyzed.

Targets come from a temporary explicit manifest (`runner`, `runner_id`, `entry_symbol`, `lifecycle_dependencies`). ASV setup functions and pytest fixtures are both expressed as lifecycle dependencies on the target. Do not implement real pytest/ASV discovery in this milestone, and do not pretend naming conventions do.

## Architecture (keep these layers separate)

```
Git snapshot reader
  -> Source index and dependency resolver
  -> Change classifier
  -> Impact planner
  -> Human-readable and JSON reports
```

- The planner operates on explicit in-memory data structures and must not import pytest or ASV. The engine is runner-independent; runner names are just labels on targets.
- No persistent caching until correctness is established.
- JSON report must distinguish: analyzed snapshots and scope, changed symbols, selected targets, unselected targets, dependency explanations, unresolved relationships, fallback decisions, analysis errors.

## Non-negotiable analysis rules

- **Analyze both revisions.** Deleted functions, removed calls, and changed aliases must stay in consideration even if absent from the head graph.
- **Stable symbol identity.** Identity is the qualified symbol (module + function/method), never source location. Inserting blank lines above a function must not change its identity or count as a body change.
- **Body changes vs. broader changes.** Module-level init, class structure, decorators, defaults, and other definition-time changes need broader (conservative) invalidation, never "irrelevant".
- **Unknown != unaffected.** Unresolved relationships are represented explicitly. When impact cannot be bounded, select all supplied targets and report the fallback rule.
- **New or changed targets are always selected**, even with no dependency edges.
- **Every selection reason maps to a real dependency edge or an explicit fallback rule.** Never fabricate call paths.
- **An analysis error must not become a successful empty selection.**
- Method identity and method-call resolution are separate: recognizing `Class.method` as a symbol does not mean `obj.method()` calls are resolved. No type inference, dynamic dispatch, branch- or argument-sensitive analysis, runtime tracing, or ML ranking. Unsupported constructs stay visible in the output.

Supported subset: module-level functions and methods; direct statically resolvable calls/references; ordinary imports and imported-name aliases within configured source roots; transitive dependencies; manifest-declared target and lifecycle dependencies.

## Testing expectations

Acceptance tests use small git repository fixtures with before/after commits. Assert **exact target sets and reasons**, not just that the command succeeds. Every scenario should mix pytest-labeled and ASV-labeled targets to prove runner independence. The required scenarios are tabulated in the handoff doc (independent functions in one file, transitive consumers, aliased imports, shared setup change, deletion/redirect, new/changed target, unresolvable relationship, blank-line insertion).

Any change that **narrows** selection must come with a regression scenario.

## Docs to maintain

`README.md`, `docs/design.md`, `docs/roadmap.md`, and `AGENTS.md` are required by the milestone. Always separate implemented functionality from planned functionality.

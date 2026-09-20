# Diffcone: First Implementation Task

**Coding-agent handoff**

**Repository:** `bearing-research/diffcone`  
**Repository URL:** https://github.com/bearing-research/diffcone

## Objective

Build the first working prototype of **Diffcone**, a static-first, function-level change-impact engine for Python.

Diffcone maps changes in application code to affected tests and benchmarks. Pytest and ASV will be the first runner integrations.

**This assignment is to produce an explainable selection plan, not to execute or deselect targets yet.**

## Established design decisions

These decisions supersede earlier proposals for an execution-history-based testing platform:

| Area | Decision |
|---|---|
| Dependency discovery | Static source analysis first. No previous test or benchmark execution required. |
| Source granularity | Whole functions and methods. Broader scopes provide conservative fallbacks. |
| Target granularity | Whole test or benchmark functions, including all their parameter cases. |
| Shared engine | Runner-independent analysis and selection. |
| Initial integrations | Pytest and ASV. |
| Uncertainty | Explicit unresolved dependencies and broader selection, never silently missing edges. |
| Deployment | Local-first. No service, account, or remote database required. |

No branch-sensitive analysis, argument-sensitive selection, runtime tracing, or machine-learning-based ranking is required.

## First deliverable

Implement a command along these lines:

```bash
diffcone plan \
  --base <git-revision> \
  --head <git-revision> \
  --targets <target-manifest> \
  --format json
```

For this first milestone, comparing **two explicit committed snapshots** is sufficient. Make that limitation clear in the documentation and output; do not imply that uncommitted changes were analyzed.

The command must:

1. Read and index both source snapshots.
2. Identify changed functions and changes requiring broader invalidation.
3. Follow dependencies backward to runnable targets.
4. Produce a deterministic plan with selection reasons and fallback information.

It must not execute project code or modify the working tree during source analysis.

### Temporary target manifest

For the prototype, accept an explicit manifest describing target identities, entry functions, and setup dependencies.

This is a temporary integration boundary, not the intended end-user discovery workflow. It lets us validate the core before implementing real pytest and ASV discovery.

For example:

```text
Target
    runner
    runner_id
    entry_symbol
    lifecycle_dependencies
```

An ASV setup function and a pytest fixture can both be represented as dependencies supplied with a target. Do not pretend that naming conventions alone implement either framework's discovery semantics.

## Analysis scope

Start with a documented, narrow subset:

- Module-level functions and methods as identifiable symbols.
- Direct, statically resolvable function calls and references.
- Ordinary imports and imported-name aliases within configured source roots.
- Transitive dependencies.
- Explicit target and lifecycle dependencies from the manifest.

Method identity and method-call resolution are separate concerns. Recognizing a method definition does not mean an arbitrary `object.method()` call has been resolved.

Do not attempt comprehensive Python type inference or dynamic dispatch analysis in this milestone. Unsupported constructs must remain visible.

### Important implementation rules

**Analyze both revisions.** Deleted functions, removed calls, and changed aliases must not disappear from consideration merely because the new graph no longer contains them.

**Use stable symbol identities.** Source locations are metadata, not identity. Adding blank lines above a function must not create a new function identity.

**Separate body changes from broader changes.** Changes to module initialization, class structure, decorators, defaults, and other definition-time behavior may require broader invalidation. Do not silently classify them as irrelevant.

**Do not equate "no known dependency" with "unaffected."** Represent unresolved relationships. When their impact cannot be bounded, select all supplied targets and explain the fallback.

**Handle new targets.** A newly introduced test or benchmark must not be omitted because it lacks historical dependencies.

## Architecture constraints

Use Python and `uv`. Keep dependencies modest and preserve the repository's existing license.

Keep these responsibilities separate:

```text
Git snapshot reader
    -> Source index and dependency resolver
    -> Change classifier
    -> Impact planner
    -> Human-readable and JSON reports
```

The planner should operate on explicit data structures and should not import pytest or ASV.

Start with in-memory data structures. Persistent caching is unnecessary until correctness is established.

Every selection reason should be supported by an actual dependency edge or an explicit fallback rule. Do not fabricate call paths for explanations.

The JSON report should distinguish:

```text
analyzed snapshots and scope
changed symbols
selected targets
unselected targets
dependency explanations
unresolved relationships
fallback decisions
analysis errors
```

An analysis error must not become a successful empty selection.

## Acceptance scenarios

Create small repository fixtures with before-and-after commits. Assertions must check exact target sets and reasons, not merely that the command succeeds.

| Scenario | Expected result |
|---|---|
| Two independent functions share a source file; only one changes. | Select that function's consumers, not the independent consumers. |
| A changed helper has indirect consumers. | Select targets through transitive dependencies. |
| An imported function is referenced through an alias. | Resolve the alias or explicitly fall back; never silently omit the relationship. |
| A shared setup function changes. | Select every declared consumer of that setup. |
| A function is deleted or a dependency is redirected. | Account for relationships from both revisions. |
| A target is introduced or its body changes. | Select that target. |
| A potentially relevant relationship cannot be resolved. | Broaden selection and report the uncertainty. |
| Blank lines are inserted above otherwise unchanged functions. | Preserve symbol identity without manufacturing body changes. |

Include both pytest-labeled and ASV-labeled targets in the same scenarios. This verifies that the engine is genuinely runner-independent.

## Repository documentation

Create or update:

**`README.md`:** What Diffcone does, its development status, the supported prototype workflow, and current limitations.

**`docs/design.md`:** Symbol identities, dependency representation, before/after analysis, change classification, and fallback behavior.

**`docs/roadmap.md`:** Next milestones: real runner discovery, working-tree analysis, broader resolution support, and validated execution integration.

**`AGENTS.md`:** Preserve the scope boundaries and require regression scenarios for changes that narrow selection.

Clearly separate implemented functionality from planned functionality.

## Completion report

Prepare a reviewable branch or pull request. Do not publish a release.

Report what works, the commands used to verify it, the supported source-analysis subset, known limitations, and representative selection explanations.

**The milestone succeeds when the planner makes correct, explainable decisions on its supported scenarios, not when it claims to understand arbitrary Python.**

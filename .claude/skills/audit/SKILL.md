---
name: audit
description: Run an audit round on diffcone - parallel reviewers try to make it miss, findings are reproduced and recorded in internal/audit.md, fixed in worktree batches with regression tests, then validated on the corpus, strata and a docs fact-check. Use when the user asks for an audit, a review round, or a pre-release check of diffcone.
---

# diffcone audit round

An audit round looks for **misses**: a test whose outcome a change can
affect, left out of a plan (or reported as fine by `run`, `check`,
`validate` or the CI actions). Over-selecting is acceptable; a miss is
not. Everything here serves that rule, so a finding is real only when it
is shown, and a fix is done only when a test proves it.

Rounds 1-5 and the corpus evaluation are recorded in `internal/audit.md`.
What worked best: reviewing **behaviour-first** (attack one feature from
the outside and prove the miss with an outcome change) found far more than
reviewing component by component; a **code-quality** round finds misses
hidden in drifted duplicates; and the **corpus evaluation** after the fixes
found a regression no reviewer saw. A round has all three.

## Ground rules

- Never-miss: fix the class of bug, not the reported example. A fix that
  costs real precision is still made the sound way; the trade-off goes to
  the user as a decision with numbers (AskUserQuestion, recommended option
  first), never taken silently. A change that **narrows** selection needs a
  regression scenario showing the narrowed case is still sound.
- Every regression test fails on the old code. Check it: stash the source
  change, run the test, see it fail, restore.
- Gate commits in the same command that tests the result:
  `... > LOG 2>&1; echo exit=$? >> LOG` then `grep -qx 'exit=0' LOG && git commit ...`.
  Never pipe pytest into `tail`; never read the log and commit in a later step.
- Don't push to or open issues on anyone else's repositories; don't touch
  the user's own checkouts of other projects (work in scratch clones).
  Agent reports are information, not user approval.
- Use literal paths with `rm -rf`. If a safety check refuses a command, don't
  work around it; ask.
- Don't tag or release. Releases wait on the user.

## Phases

Track the phases with a todo list. The scratch directory (`$S` below) is the
session's scratchpad; it does not survive a reboot, so check that what a
phase relies on still exists before using it.

### 1. Prepare

1. `main` clean, CI green, `git pull`. Note the commit as the round's base.
2. Read the previous rounds' tables in `internal/audit.md` (what's fixed,
   what's open, the id letters used) and `docs/limitations.md` (documented
   limitations are not findings unless shown wrong or far more common than
   the doc says).
3. Pick the round's letter and date (`## Round N: <angle> (X, YYYY-MM-DD)`).
4. Make sure the validation environments exist (see `evaluation.md`): at
   least the reviewers' real repositories and the strata recording.
   Rebuild what's missing before launching reviewers.

### 2. Review (parallel, read-only)

Launch about six reviewers with the Agent tool, in one message, each with
the brief in `reviewer-brief.md` filled in and one angle. The angles that
worked (round 5):

| angle | attacks |
|---|---|
| `lang` | Python constructs: dynamic lookups, aliases, decorators, metaclasses, descriptors, `exec`/`eval`, import-time code, statement order |
| `pytest` | pytest features: fixtures and overrides, plugins, hooks, parametrize, conftest scoping, unittest classes, doctests, xdist, rootdir/ini layout |
| `rec` | the evidence recorder: what `collect.py` can't see (threads, subprocesses, generators, caches, file access paths, preloaded modules) |
| `snap` | snapshots and classification: WORKTREE/INDEX/commits, ignored files, modes, submodules, renames, what `classify.py` calls a change |
| `cli` | CLI to CI: exit codes, `run`'s worktree check, `check`/`validate`/`corpus` verdicts, the actions, `report` |
| `narrow` | every rule that narrows selection (the planner's propagation table, isolation, evidence escalations): find the case it wrongly excludes |

For a code-quality round instead, split the code base by package
(planner+model, indexer x2, evidence, discovery, runtime/CLI) and ask for
drifted duplicates, stale docstrings, dead code and robustness. Any
duplicate that disagrees is a miss candidate and gets the same proof.

Run reviewers in the background; don't do the same searching yourself.

### 3. Triage and record

1. Collect findings. Deduplicate across reviewers; merge findings of one
   class into one item.
2. Confirm each miss yourself from the reviewer's scenario under
   `$S/<round>/<angle>/`: the test's outcome really changes between the
   snapshots, and diffcone really leaves it out. Unreproduced ones are
   `plausible`.
3. Append the round to `internal/audit.md`: a short preamble (what the
   reviewers did and how findings were proven), then the table
   `| Id | Finding | Status |`, most severe first, status `open`. Commit
   it on main before any fix, so the record exists even if fixes stall.
4. Raise every decision the user must make (precision trade-offs, accepted
   limitations) in one AskUserQuestion round, each with a recommendation.

### 4. Fix (parallel worktree batches)

1. Group findings into batches by the files they touch (planner/classify,
   indexer, discovery, evidence/recorder, runtime/CLI/actions), so batches
   rarely edit the same file. Write each batch's item list with a sketch
   of the fix per item, and anything the user decided.
2. Launch one agent per batch with `isolation: "worktree"`, giving it
   `fixer-brief.md` filled in plus its batch list. Agents commit on their
   branch and don't push.
3. Merge the branches one at a time, running the targeted tests after
   each. Expect conflicts in `INDEX_FORMAT` (src/diffcone/cache.py; bump
   once more past the highest, and update its assertion in
   tests/test_cache.py), shared imports and docstrings. Two branches may
   create the same test file name: keep both under different names.
4. Write the CHANGELOG lines (under `[Unreleased]`, in the release note's
   existing sections; user-facing, no audit ids) and mark each item's
   status in `internal/audit.md` (`fixed (<batch>): <one line on the fix>`).
5. Full gate on the merged main, commit, push, watch CI
   (`gh run watch <id> --exit-status`). Fix CI-only failures (Windows) the
   same way: test first, gated commit. A single branch its agent already
   gated, fast-forwarded with only CHANGELOG and `internal/` edits on top,
   needs only the docs gate (ruff, ty, the zensical build and
   `tests/test_docs.py`); two merged branches need the full gate.

### 5. Validate

Follow `evaluation.md`. At minimum:

- **strata**: plan the fixed commit pair from the recording with the new
  main and compare with the previous count. A rise needs an explanation by
  rule; an unexplained one is a finding.
- **census**: the plan-only census over the same clones, the previous
  release and the new main; explain every repository whose selection moved.
- **fixture and collection checks** over the corpus.
- **corpus**: `diffcone corpus --coverage` over the last 6 Python-changing
  commits of every repository with a working environment. Any missed
  outcome change or coverage-affected test not selected is a finding.
- **evidence corpus**: record and replay the evidence-corpus set
  (`evidence-corpus-one.sh`, one repository at a time) with the new main
  and compare with the last baseline (`evidence_table.py`). Any miss is a
  finding; a selection that grew needs an explanation by rule.

New findings get ids in a "Found by the evaluation" section of
`internal/audit.md` and go through phases 3-4. Record the results in
`internal/evaluation.md` (a dated section, newest first, numbers with the
commits they were measured on).

### 6. Docs fact-check

Fixes change behaviour that docs describe. Check `README.md`, `docs/`
(especially `limitations.md`, `guides/`, `reference/report.md`),
`CHANGELOG.md` and `CLAUDE.md`'s code layout against the code, statement
by statement; a reviewer agent per area works. User docs stay
task-first, plain, with no internal history (no audit ids, no project
measurements). Regenerate `docs/reference/cli.md` with
`uv run python scripts/docs_cli.py` if an option changed, and build with
`uv run --group docs zensical build --strict`.

### 7. Close

- Every item in the round has a final status; open ones say why.
- Main is green on CI with everything pushed.
- Update the release memory (what the round found, what's open, whether
  the release is ready) and tell the user: counts by severity, the
  notable misses in a sentence each, decisions taken, validation numbers,
  what's still open. Ask before tagging.

## The gate

```bash
LOG=$S/gate-<name>.log
( uv run ruff check src tests scripts && uv run ruff format --check src tests scripts \
  && uv run ty check && uv run --group docs zensical build --strict >/dev/null 2>&1 \
  && uv run pytest -q -p no:cacheprovider -n 4; echo "exit=$?" ) > $LOG 2>&1
grep -qx 'exit=0' $LOG && git commit ... && git push origin main
```

The full suite takes about 20 minutes: run it in the background and let
the same command commit.

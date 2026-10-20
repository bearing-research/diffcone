# Fixer brief (fill in the placeholders, then pass with the batch list)

Placeholders: `<ROUND>`, `<LETTER>` (the round's id letter), `<BASE>`,
`<S>`, `<E>`, `<STRATA>`, `<BATCH>` (a short name), `<FILES>` (the files
the batch owns), `<ITEMS>` (ids with a fix sketch each, and any decision
the user made). Launch with `isolation: "worktree"`.

---

# Audit <ROUND> fix batch <BATCH>

You are fixing confirmed findings from diffcone's audit <ROUND>. Read
CLAUDE.md, AGENTS.md and the <ROUND> section at the end of
internal/audit.md. The reviewers' reproductions are under
<S>/<ROUND>/<angle>/: read the scenario for each of your items before
fixing it.

You work in your own git worktree on your own branch (from main at
<BASE> or later). Other agents fix other batches in parallel. Stay inside
your files: <FILES>. If a fix needs a small edit elsewhere, keep it
minimal and list it in your report.

Items: <ITEMS>

## The rule

Never miss: a plan may select more than needed but must never leave out a
test whose outcome a change can affect. Fix the class of bug, not the
example: ask what else falls through the same gap. Where a fix needs a
real trade-off (a large precision loss on real projects), implement the
sound option and report the trade-off with numbers. Don't narrow
silently, and don't widen to select-all where a precise sound rule exists.
A fix that narrows anything needs a regression scenario showing the
narrowed case is still sound.

## How

- Regression tests in the test module that covers the behaviour (the
  layout is in CLAUDE.md, "Testing expectations"; never a module per
  round), named `test_<letter><n>_...`, each failing on the old code (check by stashing
  your source change, running it, then restoring). FixtureRepo scenarios
  asserting exact target sets; mix pytest and ASV targets where natural.
- Keep cache identity (a cold and a warm cache give byte-identical plans;
  FixtureRepo checks it). Bump INDEX_FORMAT in src/diffcone/cache.py if
  the index's meaning changes, and its assertion in tests/test_cache.py;
  say so in the report (the merge reconciles the numbers).
- Docs: module docstrings (the authoritative "what is modelled" lists),
  internal/design.md, internal/evidence_design.md, and docs/ (user docs:
  plain, task-first, no internal history) where user-visible behaviour
  changes. Regenerate docs/reference/cli.md with
  `uv run python scripts/docs_cli.py` if an option changes. Do NOT edit
  CHANGELOG.md, internal/audit.md or CLAUDE.md: propose text in your
  report.
- Precision guard, for any change to planning or the indexer: plan strata
  before and after (`.claude/skills/audit/eval/strata_check.sh <tree>
  <name>` with STRATA=<STRATA>) and explain any change by rule. For static
  precision, compare `diffcone plan` selections over the last ~8 commits
  of a few repositories under <E>/repos before and after.
- Targeted tests while you work
  (`uv run pytest -q -p no:cacheprovider tests/<file> -k ...`); the machine
  is shared. Measure (census, edge counts, plans) once the code is final,
  and run the full gate once, last: a fix after the gate means another
  full run.
- The gate at the end: `uv run ruff check src tests scripts`,
  `uv run ruff format --check src tests scripts`, `uv run ty check`, then
  the full suite to a log, once: `uv run pytest -q -p no:cacheprovider -n 4 > LOG 2>&1;
  echo exit=$?` (never pipe pytest into tail). Commit on your branch only
  when the gate exits 0, in a command that checks it
  (`grep -qx exit=0 LOG && git commit ...`). Don't push.
- End commit messages with the attribution lines of the session's
  system reminder.

## Final report (concise)

Branch and commit SHAs. Per item: fixed, partly or not; the test name; one
line on the fix. Trade-offs with numbers. Proposed CHANGELOG text.
Proposed internal/audit.md status per item. Edits outside your files.
Anything noticed out of scope.

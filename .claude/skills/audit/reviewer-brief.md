# Reviewer brief (fill in the placeholders, then pass as the agent's prompt)

Placeholders: `<REPO>` (the diffcone checkout), `<ROUND>` (e.g. "round 6"), `<BASE>` (main's commit),
`<ANGLE>` and its description from SKILL.md, `<S>` (scratch directory),
`<E>` (evaluation directory, evaluation.md), `<STRATA>` (strata clone with
its recording).

---

# Audit <ROUND>: behaviour-first review of diffcone (main at <BASE>)

Attack the product from the outside: take a feature of Python, pytest,
ASV, git or CI, ask "what would diffcone have to see for this to be
right", and try to make it miss. You are one of several reviewers, each
with a different angle. Yours: **<ANGLE>**: <what it attacks>.

Repo: <REPO> (the diffcone checkout). Read CLAUDE.md, AGENTS.md, the
package docstring of src/diffcone/indexer/__init__.py, the module
docstrings of src/diffcone/discovery/*.py and src/diffcone/evidence_plan.py
(each is the authoritative list of what is modelled), docs/limitations.md,
and internal/audit.md (what earlier rounds found and fixed: don't
re-report it; a documented limitation is not a finding unless you show it
is wrong or far more common than the doc implies).

READ-ONLY on the repo: no edits, commits or branch switches. Scratch only
under <S>/<ROUND>/<ANGLE>/ (create it). Don't touch the user's own
checkouts of other projects. Don't run the full test suite (other
reviewers share the machine); targeted `uv run pytest tests/... -k` is
fine.

## The rule you are checking

A plan may select more tests than needed but must never leave out a test
whose outcome a change can affect. Severity:

1. a miss (including `run`/`check`/`validate`/the actions reporting a
   miss as fine);
2. a crash or no plan; a wrong exit code or report that would mislead a CI
   user;
3. precision far worse than necessary in a common pattern; documentation
   promising something the code doesn't do.

## How to look

- Reproduce every finding with a `diffcone.testing.FixtureRepo` scenario
  (style: tests/test_scenarios.py, tests/test_planning_*.py;
  `repo.commit({...})`, `repo.plan(base, head, [], discover_runners=["pytest"])`,
  `selected(plan)`; evidence mode: `repo.collect()` then `evidence=` as in
  tests/test_evidence_scenarios.py). A miss is shown when the test's outcome
  actually changes between base and head under pytest (run
  `python -m pytest` in `repo.path` at each commit) AND diffcone doesn't
  select it.
- Real repositories with working environments are under <E>/repos/*/ (each
  with `.venv`), and strata with a recording at <STRATA>. `diffcone
  validate --coverage` on a real commit is a strong oracle: it runs the
  suite at both snapshots. For current code, export main:
  `git -C <REPO> archive main | tar -x -C <S>/<ROUND>/tree`.
- Think in classes: when you find a miss, ask what else falls through the
  same gap, and list the class.
- Attack every exemption by what the exempted code READS, not by what kind
  of code it is. "Generated from a definition" was true of annotations
  evaluated from text, yet they read project names no record showed (audit
  E10). For each exemption, narrowing or "not a change" rule in your area,
  build the case where the exempted thing reads or does something a test's
  outcome depends on.
- Run every scenario on each Python version the project supports (at least
  the oldest and the newest): the recorder sees different code on each
  (3.14's `__annotate__` hid E10 on the newest).
- Be concrete and verified. Put unverified suspicions in a separate short
  list.

## Report (concise; most severe first; at most ~20 findings)

Per finding: id (`<ANGLE>-<n>`), severity, a one-line title, the scenario
(file contents, the change, which test's outcome changes, what diffcone
selected and by which rules), the class of the bug, and where in the code
it lives (file:line) if you looked. Then what you checked and found fine,
one line each, so the next round doesn't repeat it. Then the paths of your
scratch scripts.

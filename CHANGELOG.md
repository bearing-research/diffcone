# Changelog

All notable changes to diffcone. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/), and before 1.0 a minor version
may change the report schema, the cache and evidence store formats, or
selection rules.

## [0.1.0] - 2026-10-06

The first release.

### Planning

- `diffcone plan` compares two snapshots (git revisions, the staged
  `INDEX`, or the `WORKTREE`) and reports which tests and benchmarks a
  change can affect, each with the dependency path or the fallback rule
  that selected it. Both revisions are analysed; analysis never runs
  project code. JSON (`schema_version` 3) and text reports; exit codes
  distinguish a complete plan (0), a degraded one that selects everything
  (1), no plan (2) and a target list that may be short of what the runner
  collects (3).
- Static discovery of pytest tests (configuration, collection rules,
  inheritance, star-imported suites, the fixture chain with fixtures bound
  by alias or import, fixtures and marks inherited from base classes in
  other modules, marks stored in variables, parametrized names supplied to
  fixtures, plugins declared by plugins, and overrides of fixtures that
  pytest and installed plugins request, doctests)
  and ASV benchmarks, without importing project code; what it cannot see
  it reports. `diffcone discover` writes the targets as a manifest.
- Dependencies the analysis cannot see can be declared in `diffcone.toml`.
- Indexes, discovery and per-module results are cached under
  `.diffcone/cache/`; a cached plan is identical to an uncached one.
  `diffcone prune --keep REV` shrinks the cache to what planning at given
  commits reads.

### Running and checking

- `diffcone run` plans and runs only the selected targets with pytest or
  ASV; `diffcone validate` runs the whole suite at both snapshots and
  checks the plan against outcome changes (and, with `--coverage`, against
  what each test executed); `diffcone corpus` does so over a commit range.
- `diffcone check` compares a plan with the JUnit XML of a full run, and of
  other selective runs (pytest-testmon's, say), reporting every failure the
  plan missed; Markdown output for CI job summaries.

### Execution evidence (opt-in, Python 3.12+ in the project)

- `diffcone collect` records what each test executed, at a commit;
  `plan --evidence` selects the tests whose record meets the change, and
  plans the rest statically or selects everything, saying which.
  `run --collect` advances the record to the new commit. `--env-var`
  records project variables that change what tests do.
- Cython: with a `profile=True` build recorded on Python 3.13+, edits to
  function bodies and to the names a file binds outside functions select
  the tests that executed the code concerned.
- A recording settles what static discovery cannot know (whether a plugin
  collects a class pytest's rules skip), so such plans no longer stop at
  exit 3.

### CI

- Composite GitHub Actions (`actions/record`, `actions/run`,
  `actions/check`) to record nightly on the default branch and plan, run
  and check each pull request against it; see `docs/ci.md`.

[0.1.0]: https://github.com/bearing-research/diffcone/releases/tag/v0.1.0

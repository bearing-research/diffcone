# Planning

`diffcone plan` compares two snapshots and reports which targets a change
can affect. It reads files and git objects only: no checkout, no import,
no test run.

```bash
diffcone plan --base main --head HEAD --discover pytest --discover asv \
    --source-root src --source-root . --format text
```

## Snapshots

`--base` and `--head` each name a snapshot:

- **a git revision** (`main`, `HEAD~3`, a commit hash), read from git's
  objects, whatever is checked out;
- **`INDEX`**, the staged content;
- **`WORKTREE`**, the files on disk, tracked or untracked, ignored files
  excluded.

`--head WORKTREE` is the everyday developer loop ("what does my unfinished
change affect?"); `--head HEAD` is the CI form. The report always names the
kind of each snapshot (`analysis.head.kind`, `uncommitted_analyzed`), so a
plan of a working tree never looks like a plan of a commit.

Both revisions are analysed, not only the head: a deleted function, a
removed call or a redirected import still selects the tests that used to
reach it.

## Source roots

A source root is a directory whose `.py` files are analysed as a module
tree. Module names come from the **longest** root containing a file: with
`--source-root src --source-root .`, `src/calc/ops.py` is `calc.ops` and
`tests/test_calc.py` is `tests.test_calc`. With only `--source-root tests`,
that test module would be `test_calc`.

Pick the roots your code is imported from: for a `src` layout, `src` and
`.` (or `src` and `tests`); for a flat layout, `.` alone (the default).
Files outside every root are not analysed; a test that imports them is
planned as if they were external libraries.

When one pytest session collects several test trees whose files share
names (`--import-mode=importlib` in a monorepo), give each tree its own
namespace with `--source-root DIR=PREFIX`: `--source-root
packages/api/tests=api_tests` names `packages/api/tests/test_x.py`
`api_tests.test_x`. Plan once per pytest session otherwise: two files
mapping to one module name are an analysis error, as pytest would report an
import mismatch.

## Targets

Targets are what a plan selects from: pytest tests, ASV benchmarks, or
anything else a runner can run by name. They come from static discovery,
from a manifest, or both.

### Static discovery

`--discover pytest` and `--discover asv` find targets in the head snapshot
by reproducing the runner's collection rules from the source, without
importing it. For pytest that means its configuration (`python_files`,
`python_classes`, `python_functions`, `testpaths`, `addopts` doctests),
test functions and classes, inherited and star-imported tests, and each
test's fixtures, resolved the way pytest resolves them. The
[discovery reference](../reference/discovery.md) lists exactly what is
modelled.

What discovery cannot see, it reports. A fixture it cannot find makes the
test's selection conservative (the report says so); a class a plugin might
collect, a base class outside the source roots or an imported test it
cannot follow make the plan exit `3`: the target list may be short of what
pytest collects, and running only the selected targets could skip tests.
`diffcone run` refuses such a plan unless given
`--allow-incomplete-discovery`; with [execution evidence](evidence.md), a
recording of the real collection can settle the doubt instead.

Fixtures from well-known plugins (`mocker`, `httpx_mock`, `freezer`,
`benchmark`, ...) are assumed external; name others with
`--assume-external-fixture NAME`.

To see what discovery finds, write it out as a manifest:

```bash
diffcone discover --rev HEAD --discover pytest --discover asv \
    --source-root src --source-root . -o targets.json
```

### A manifest

`--targets targets.json` supplies targets explicitly: for a runner without
discovery, or to correct one. Discovery and a manifest combine, and a
manifest entry overrides a discovered target with the same id. The format
is in the [manifest reference](../reference/manifest.md).

## Telling diffcone what it cannot see

Some dependencies are real but invisible to any static rule: a registry
filled at import time, a plugin resolved through entry points. Declare them
in `diffcone.toml` at the repository root, and the plan follows them,
explaining the selection in your words:

```toml
[[edges]]
from = "pkg.registry.dispatch"
to = "pkg.handlers.json_handler"
why = "handlers register themselves through entry points"
```

Declarations only add edges, so they can only select more. See the
[`diffcone.toml` reference](../reference/declarations.md).

## What a change selects

In short (the [design](../design.md) has the precise rules):

- A changed function or method body selects the targets that reach it
  through calls and references, transitively.
- A changed definition (decorators, defaults, a class body, bases, a
  removed or redirected import) changes every member it shapes.
- A change that runs at import (module-level code, a constant, a class
  body) reaches every target whose module imports that module, directly or
  not.
- A new or changed target is always selected.
- What cannot be bounded (a dynamic import, `eval`, a file that does not
  parse) widens the selection and is reported as a fallback, never dropped.

## The cache

Committed snapshots are indexed once and cached under `.diffcone/cache/`
(add `.diffcone/` to `.gitignore`), and every module's analysis is cached
by its content, so planning the working tree again re-reads only the files
that changed. A cached plan is identical to an uncached one. `--no-cache`
and `--cache-dir` control it; `diffcone prune --keep REV` shrinks it to
what planning at given commits reads, for a cache shipped between CI runs.

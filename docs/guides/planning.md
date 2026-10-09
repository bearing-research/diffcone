# Planning

`diffcone plan` compares two snapshots and reports which targets the
changes between them can affect. It reads files and git objects only: it
doesn't check anything out, import your code, or run tests.

```bash
diffcone plan --base main --head HEAD --discover pytest \
    --source-root src --source-root . --format text
```

## Choose what to compare

`--base` and `--head` each take a git revision, `INDEX` or `WORKTREE`
([snapshots](../concepts.md#snapshots)). Common combinations:

| You want to check | Use |
|---|---|
| Uncommitted work, before you commit | `--base HEAD --head WORKTREE` |
| A branch, before you open a pull request | `--base main --head WORKTREE` |
| A pull request in CI | `--base origin/main --head HEAD` |
| Only what you have staged | `--base HEAD --head INDEX` |

## Source roots

A source root is a directory that diffcone treats as an import root, the
way Python's `sys.path` does. Module names come from the closest root that
contains a file:

| Roots | `src/calc/ops.py` | `tests/test_calc.py` |
|---|---|---|
| `.` (the default) | `src.calc.ops` | `tests.test_calc` |
| `src` and `.` | `calc.ops` | `tests.test_calc` |
| `src` and `tests` | `calc.ops` | `test_calc` |

Use the roots your code is actually imported from. For a `src` layout,
that's usually `--source-root src --source-root .`; for a flat layout, the
default is fine. Files outside every root are not analysed, so code there
is treated like a third-party library.

If one pytest session collects several test directories whose files share
names (common in monorepos that use `--import-mode=importlib`), give each
directory its own namespace with `DIR=PREFIX`:

```bash
diffcone plan --base main --head HEAD --discover pytest \
    --source-root packages/api/src --source-root packages/api/tests=api_tests
```

## Find the targets

### Discover them

`--discover pytest` and `--discover asv` find targets by reading your
configuration and test files, the way the runner would collect them,
without importing anything. To see what discovery finds, write it to a
file:

```bash
diffcone discover --rev HEAD --discover pytest --source-root src --source-root . -o targets.json
```

For pytest, discovery follows each test's fixtures, so a change to a
fixture or a `conftest.py` selects the tests that use it. Fixtures from
well-known plugins (`mocker`, `httpx_mock`, `freezer`, `benchmark` and
others) are recognised automatically. If your tests use fixtures from
another installed plugin, name them with `--assume-external-fixture NAME`;
otherwise diffcone can't see what they depend on and selects their tests
every time.

!!! note "When the plan exits with code 3"

    Exit code `3` means your runner may collect tests that diffcone can't
    see: for example a pytest plugin that collects classes by its own
    naming rules, or a test base class outside your source roots. The
    report's `discovery` section lists each case. Running only the selected
    tests could skip some, so `diffcone run` refuses unless you pass
    `--allow-incomplete-discovery`. See
    [static discovery](../reference/discovery.md#when-the-target-list-may-be-short).

### List them in a manifest

For a runner without discovery, or to adjust what discovery finds, pass a
[target manifest](../reference/manifest.md) with `--targets targets.json`.
You can combine it with `--discover`; manifest entries replace discovered
targets with the same ID.

## Declare dependencies diffcone can't see

Some dependencies aren't visible in the code: a plugin registered through
entry points, a handler named in a configuration file. Declare them in a
`diffcone.toml` at the repository root:

```toml
[[edges]]
from = "pkg.registry.dispatch"
to = "pkg.handlers.json_handler"
why = "handlers register themselves through entry points"
```

Now a change to `json_handler` selects the tests that reach `dispatch`.

Tests you want on every pull request whatever it changes, such as an
end-to-end suite, go in the same file:

```toml
[[always_run]]
targets = "tests/e2e/*"
```

See the [`diffcone.toml` reference](../reference/declarations.md).

## Speed

diffcone caches its analysis in `.diffcone/cache/` (add `.diffcone/` to
your `.gitignore`). Committed snapshots are analysed once, and every file
is cached by its content, so planning your working tree again only re-reads
the files you changed. Cached and uncached plans are identical.

- `--no-cache` turns the cache off; `--cache-dir` moves it.
- `diffcone prune --keep HEAD` removes everything the cache doesn't need
  for planning from a commit, which keeps a cache saved between CI runs
  small.

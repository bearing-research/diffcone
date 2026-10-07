# How it works

## Snapshots

diffcone compares two **snapshots** of a repository: a *base* and a *head*.
A snapshot is one of:

- a git revision, such as `main`, `HEAD~1` or a commit hash;
- `INDEX`, the changes you have staged with `git add`;
- `WORKTREE`, the files on disk, including uncommitted and untracked files
  (files ignored by git are left out).

diffcone reads commits straight from git, so it doesn't matter what is
checked out. The report always says which kind of snapshot it read, so a
plan of uncommitted files is never mistaken for a plan of a commit.

## Targets

A **target** is something a runner can run on its own: a pytest test or an
ASV benchmark. diffcone finds targets by reading your runner's
configuration and source files (see [static discovery](reference/discovery.md)),
or you list them in a [manifest](reference/manifest.md).

Each target has an entry point (the test function) and the code the runner
executes around it, such as pytest fixtures or ASV `setup` methods. A
change to any of these can affect the target.

## Symbols and changes

diffcone reads every Python module under your
[source roots](guides/planning.md#source-roots) and records its
**symbols**: modules, classes, functions and methods, each named the way
it is imported (`calc.ops.mul`). It compares the two snapshots symbol by
symbol, and classifies each difference:

| Change | Example |
|---|---|
| added or deleted | A new helper function, a removed method. |
| body changed | The code inside a function changed. |
| definition changed | Its signature, defaults, decorators or class bases changed. |
| dependencies changed | It now calls or imports something different. |

Formatting and comments are not changes: moving a function down the file or
adding blank lines selects nothing.

## From a change to a test

diffcone links symbols by the calls, references and imports it can resolve
in the code. To plan, it starts from each test and follows those links: a
test is selected when it reaches a changed symbol. The report shows the
path for each one, for example:

```text
test_square -> calc.ops.square -> calc.ops.mul (body changed)
```

Some changes reach further than calls:

- A change to code that runs when a module is imported (a module-level
  statement, a constant, a class body) can affect every module that imports
  it, directly or indirectly.
- A change to a class's structure (its bases, decorators or attributes)
  affects all of its methods.
- A new or changed test is always selected.

Both snapshots are analysed, so a deleted function or a removed call still
selects the tests that used it.

## When diffcone can't tell

Python is dynamic, and not every relationship can be read from the source:
`getattr` with a computed name, a module imported by a name built at run
time, a plugin registered through entry points. diffcone does not guess.
When it cannot bound what a change reaches, it selects more tests and
records why in the report's `fallback_decisions`. If an error stops the
analysis altogether (a file that doesn't parse, for example), it selects
every target.

You can also tell diffcone about dependencies it cannot see in
[`diffcone.toml`](reference/declarations.md).

## Execution evidence

Reading the code works well when changes stay local. In a large library
where nearly every module imports a few central ones, a change to a
central module reaches nearly every test. [Execution
evidence](guides/evidence.md) solves this by recording which functions each
test actually ran, once, and selecting from that record instead.

# diffcone

**Which tests and benchmarks can a change to Python code affect, and why?**

diffcone reads two snapshots of a repository (two commits, or a commit and
your working tree), works out which functions, methods and classes
changed, and follows the dependencies back to the tests and benchmarks
that can observe them. Every selected target comes with the path that
connects it to a change, or the rule that made diffcone select it without
one. It never runs your code to plan.

```console
$ diffcone plan --base HEAD~1 --head HEAD --discover pytest \
    --source-root src --source-root . --format text
...
selected targets (2):
  pytest: tests/test_calc.py::test_mul
    - dependency: calc.ops.mul body_changed
  pytest: tests/test_calc.py::test_square
    - dependency: calc.ops.mul body_changed
      ... -> tests.test_calc.test_square -[references]-> calc.ops.square -[references]-> calc.ops.mul

unselected targets (1):
  pytest: tests/test_calc.py::test_add
```

## What makes it different

- **It explains itself.** A selection is a dependency path through the
  code, or an explicit fallback rule; never a guess or a score.
- **It never misses on purpose.** When the analysis cannot bound a change
  (a dynamic import, a file that does not parse, a plugin it cannot see),
  it selects more and says why. A plan may run more tests than needed; it
  is built not to run fewer.
- **It is runner-independent.** pytest tests and ASV benchmarks are both
  just targets; discovery for each runner is static and documents what it
  reproduces.
- **It can learn from a run.** Optional [execution evidence](guides/evidence.md)
  records what each test executed once, so a large, tightly connected
  library (pandas) stops selecting nearly everything for nearly every
  change.

## Where to go next

- [Getting started](getting-started.md): install, a first plan, reading it.
- [Planning](guides/planning.md): snapshots, source roots, discovery, and
  telling diffcone what it cannot see.
- [Running and checking](guides/running.md): run only the selected tests,
  and check a plan against a full run.
- [Execution evidence](guides/evidence.md) and [CI](ci.md): record once
  a night, plan every pull request.
- [Command reference](reference/cli.md), [limitations](limitations.md), and
  how it works in [design](design.md).

diffcone is alpha software (0.1). It needs Python 3.11+ and git, and has no
runtime dependencies.

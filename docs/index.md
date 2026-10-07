# diffcone

**Run the tests your change can affect, and know why each one was picked.**

diffcone compares two versions of a Python repository, finds the
functions, methods and classes that changed, and follows the code's
dependencies back to the tests and benchmarks that can observe them. Each
selected test comes with the reason it was selected.

```console
$ diffcone plan --base main --head WORKTREE --discover pytest \
    --source-root src --source-root . --format text
...
changed symbols (1):
  calc.ops.mul [function] body_changed

selected targets (2):
  pytest: tests/test_calc.py::test_mul
    - dependency: calc.ops.mul body_changed
  pytest: tests/test_calc.py::test_square
    - dependency: calc.ops.mul body_changed

unselected targets (1):
  pytest: tests/test_calc.py::test_add
```

## Why diffcone

**Every selection is explained.**
:   A test is selected because a chain of calls and references connects it
    to a change, and the report shows that chain. When diffcone selects a
    test for another reason, it names the rule.

**It errs on the side of running more.**
:   When diffcone cannot tell what a change reaches (a dynamic import, a
    file it cannot parse, a test a plugin creates), it selects more tests
    and tells you why. It is designed never to skip a test that could fail.

**It never runs your code to plan.**
:   Planning reads files and git history only. Your tests run only when you
    ask diffcone to run them.

**It works with pytest and ASV.**
:   Tests and benchmarks are found by reading your configuration and
    source, the way each runner collects them.

**It can learn from a test run.**
:   For large codebases where nearly everything imports nearly everything,
    [execution evidence](guides/evidence.md) records what each test
    actually ran, and plans from that.

## Next steps

<div class="grid cards" markdown>

- **[Getting started](getting-started.md)**: install diffcone and plan
  your first change.
- **[How it works](concepts.md)**: snapshots, targets, and how a change
  reaches a test.
- **[Guides](guides/planning.md)**: planning, running tests, execution
  evidence, and CI.
- **[Reference](reference/cli.md)**: every command and option, and the
  file formats.

</div>

# diffcone

[![CI](https://github.com/bearing-research/diffcone/actions/workflows/ci.yml/badge.svg)](https://github.com/bearing-research/diffcone/actions/workflows/ci.yml)
[![Docs](https://github.com/bearing-research/diffcone/actions/workflows/docs.yml/badge.svg)](https://bearing-research.github.io/diffcone/)

**Run the tests your change can affect, and know why each one was picked.**

diffcone compares two versions of a Python repository, finds the functions,
methods and classes that changed, and follows the code's dependencies back
to the tests and benchmarks that can observe them. Each selected test comes
with the reason it was selected.

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

- **Every selection is explained:** the report shows the chain of calls and
  references from each test to the change.
- **It errs on the side of running more:** when diffcone can't tell what a
  change reaches, it selects more tests and says why.
- **It never runs your code to plan:** planning reads files and git history
  only.
- **pytest and ASV:** tests and benchmarks are found from your
  configuration and source, the way each runner collects them.
- **Execution evidence:** for large codebases where nearly everything
  imports everything, record once what each test ran and plan from that.

## Install

Python 3.11 or later and git; no other dependencies.

```bash
uv tool install diffcone    # or: pipx install diffcone, pip install diffcone
```

## Use

```bash
# Which tests can my uncommitted changes affect?
diffcone plan --base main --head WORKTREE --discover pytest --format text

# Run only those tests (arguments after -- go to pytest)
diffcone run --base main --head WORKTREE --discover pytest --command "uv run pytest" -- -x

# Record what each test runs, then plan from the recording (Python 3.12+)
diffcone collect --command "uv run pytest" -- -n 8
diffcone plan --base main --head HEAD --discover pytest --evidence auto -o plan.json

# Did the plan select every test that failed in a full run?
diffcone check --plan plan.json --full full.xml
```

For a `src` layout, add `--source-root src --source-root .` so modules get
their import names.

## Documentation

Read the documentation at
**[bearing-research.github.io/diffcone](https://bearing-research.github.io/diffcone/)**:

- [Getting started](https://bearing-research.github.io/diffcone/latest/getting-started/)
- [How it works](https://bearing-research.github.io/diffcone/latest/concepts/)
- Guides: [planning](https://bearing-research.github.io/diffcone/latest/guides/planning/),
  [running and checking](https://bearing-research.github.io/diffcone/latest/guides/running/),
  [execution evidence](https://bearing-research.github.io/diffcone/latest/guides/evidence/),
  [CI with GitHub Actions](https://bearing-research.github.io/diffcone/latest/ci/)
- [Command reference](https://bearing-research.github.io/diffcone/latest/reference/cli/)
  and [limitations](https://bearing-research.github.io/diffcone/latest/limitations/)

## Status

diffcone is alpha software. Its selections have been checked against the
full test suites of 34 open-source projects. See the
[changelog](https://github.com/bearing-research/diffcone/blob/main/CHANGELOG.md)
for what each release contains.

## Contributing

```bash
uv sync
uv run pytest
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run ty check
```

See the [development guide](https://bearing-research.github.io/diffcone/latest/development/)
and [AGENTS.md](https://github.com/bearing-research/diffcone/blob/main/AGENTS.md).

## License

MIT; see [LICENSE](https://github.com/bearing-research/diffcone/blob/main/LICENSE).

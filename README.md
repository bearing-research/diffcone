# diffcone

[![CI](https://github.com/bearing-research/diffcone/actions/workflows/ci.yml/badge.svg)](https://github.com/bearing-research/diffcone/actions/workflows/ci.yml)
[![Docs](https://github.com/bearing-research/diffcone/actions/workflows/docs.yml/badge.svg)](https://bearing-research.github.io/diffcone/)

**Which tests and benchmarks can a change to Python code affect, and why?**

diffcone reads two snapshots of a repository (two commits, or a commit and
your working tree), works out which functions, methods and classes changed,
and follows the dependencies back to the tests and benchmarks that can
observe them. Every selection comes with the path that connects it to a
change, or the rule that made diffcone select it without one. It never runs
your code to plan.

```console
$ diffcone plan --base HEAD~1 --head HEAD --discover pytest \
    --source-root src --source-root . --format text
...
changed symbols (1):
  calc.ops.mul [function] body_changed

selected targets (2):
  pytest: tests/test_calc.py::test_mul
    - dependency: calc.ops.mul body_changed
  pytest: tests/test_calc.py::test_square
    - dependency: calc.ops.mul body_changed
      ... -> tests.test_calc.test_square -[references]-> calc.ops.square -[references]-> calc.ops.mul

unselected targets (1):
  pytest: tests/test_calc.py::test_add
```

- **Explainable:** a selection is a dependency path or an explicit fallback
  rule, never a guess or a score.
- **Conservative:** what it cannot bound (a dynamic import, a file that does
  not parse, a plugin it cannot see) widens the selection and is reported. A
  plan may run more tests than needed; it is built not to run fewer.
- **Runner-independent:** pytest tests and ASV benchmarks are both targets,
  found by static discovery that never imports your code.
- **Evidence when static analysis is not enough:** record once what each
  test executed, and a large library where everything imports everything
  (pandas) stops selecting nearly the whole suite for nearly every change.

## Install

Python 3.11+ and git; no other dependencies.

```bash
uv tool install diffcone    # or: pipx install diffcone, pip install diffcone
```

## Use

```bash
# what can my uncommitted change affect?
diffcone plan --base main --head WORKTREE --discover pytest --format text

# run only that (arguments after -- go to pytest)
diffcone run --base main --head WORKTREE --discover pytest --command "uv run pytest" -- -x

# record what each test executes, then plan on it (Python 3.12+ in the project)
diffcone collect --command "uv run pytest" -- -n 8
diffcone plan --base main --head HEAD --discover pytest --evidence auto -o plan.json

# did the plan select every test that failed in a full run?
diffcone check --plan plan.json --full full.xml
```

With a `src` layout, add `--source-root src --source-root .` so modules get
their import names.

## Documentation

**[bearing-research.github.io/diffcone](https://bearing-research.github.io/diffcone/)**:
[getting started](https://bearing-research.github.io/diffcone/getting-started/),
guides to [planning](https://bearing-research.github.io/diffcone/guides/planning/),
[running and checking](https://bearing-research.github.io/diffcone/guides/running/),
[execution evidence](https://bearing-research.github.io/diffcone/guides/evidence/)
and [CI](https://bearing-research.github.io/diffcone/ci/), the
[command reference](https://bearing-research.github.io/diffcone/reference/cli/),
[limitations](https://bearing-research.github.io/diffcone/limitations/), and how
it works in the [design](https://bearing-research.github.io/diffcone/design/)
and [evaluation](https://bearing-research.github.io/diffcone/evaluation/).

## Status

Alpha (0.1). Planning, static pytest and ASV discovery, running, validating
and checking plans, and execution evidence are implemented and covered by
acceptance scenarios; recall has been measured on 34 public repositories
and on pandas. See the
[changelog](https://github.com/bearing-research/diffcone/blob/main/CHANGELOG.md)
for what each release contains and the
[roadmap](https://bearing-research.github.io/diffcone/roadmap/) for what is
next.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run ty check
```

See [development](https://bearing-research.github.io/diffcone/development/) and
[AGENTS.md](https://github.com/bearing-research/diffcone/blob/main/AGENTS.md)
for the rules that apply when changing selection behaviour.

## License

MIT, see [LICENSE](https://github.com/bearing-research/diffcone/blob/main/LICENSE).

# Getting started

## Install

diffcone needs Python 3.11 or later and git. It has no other dependencies.

=== "uv"

    ```bash
    uv tool install diffcone
    ```

=== "pipx"

    ```bash
    pipx install diffcone
    ```

=== "pip"

    ```bash
    pip install diffcone
    ```

Check the installation with `diffcone --version`.

You don't need to install diffcone into your project's environment.
Planning only reads your files, and the commands that run tests take the
command your project already uses, such as `uv run pytest`.

## Plan a change

Here is a small project with a `src` layout:

=== "src/calc/ops.py"

    ```python
    def add(a, b):
        return a + b


    def mul(a, b):
        return a * b


    def square(a):
        return mul(a, a)
    ```

=== "tests/test_calc.py"

    ```python
    from calc.ops import add, mul, square


    def test_add():
        assert add(1, 2) == 3


    def test_mul():
        assert mul(2, 3) == 6


    def test_square():
        assert square(3) == 9
    ```

Change `mul` to `return b * a`, commit, and ask diffcone which tests the
commit can affect:

```console
$ diffcone plan --base HEAD~1 --head HEAD --discover pytest \
    --source-root src --source-root . --format text
diffcone plan: HEAD~1 -> HEAD
source roots: src, .
base: commit 964b20a3a4dc (HEAD~1)
head: commit c32300921438 (HEAD)
scope: two committed snapshots; the working tree was not analyzed
status: complete

changed symbols (1):
  calc.ops.mul [function] body_changed

selected targets (2):
  pytest: tests/test_calc.py::test_mul
    - dependency: calc.ops.mul body_changed
      target:pytest:tests/test_calc.py::test_mul -[entry]-> tests.test_calc.test_mul -[references]-> calc.ops.mul
  pytest: tests/test_calc.py::test_square
    - dependency: calc.ops.mul body_changed
      target:pytest:tests/test_calc.py::test_square -[entry]-> tests.test_calc.test_square -[references]-> calc.ops.square -[references]-> calc.ops.mul

unselected targets (1):
  pytest: tests/test_calc.py::test_add

unresolved relationships: 0 (0 matching an affected symbol, 0 dynamic)

discovery (pytest): 3 target(s), 0 note(s)
```

Here is what each option did:

- `--base HEAD~1 --head HEAD` compares the commit with its parent.
- `--discover pytest` found the three tests by reading pytest's
  configuration and the test files.
- `--source-root src --source-root .` tells diffcone where modules are
  imported from, so `src/calc/ops.py` is the module `calc.ops`. See
  [source roots](guides/planning.md#source-roots).

`test_mul` calls `mul` directly, and `test_square` reaches it through
`square`, so both are selected. `test_add` has no path to `mul`.

!!! tip "Plan before you commit"

    Use `--head WORKTREE` to plan the files on disk, including uncommitted
    edits: `diffcone plan --base HEAD --head WORKTREE --discover pytest`.

## Run the selected tests

`diffcone run` plans the same way, then runs only the selected tests.
Arguments after `--` are passed to pytest:

```bash
diffcone run --base main --head WORKTREE --discover pytest \
    --source-root src --source-root . --command "uv run pytest" -- -x
```

## Use the JSON report

Without `--format text`, `diffcone plan` prints a JSON report for scripts
and CI:

```json
{
  "status": "complete",
  "changed_symbols": [
    {"id": "calc.ops.mul", "kind": "function", "changes": ["body_changed"]}
  ],
  "selected_targets": [
    {"runner": "pytest", "runner_id": "tests/test_calc.py::test_mul", "rules": ["dependency"]},
    {"runner": "pytest", "runner_id": "tests/test_calc.py::test_square", "rules": ["dependency"]}
  ],
  "unselected_targets": [
    {"runner": "pytest", "runner_id": "tests/test_calc.py::test_add",
     "reason": "no dependency path from this target to a changed symbol in either revision"}
  ]
}
```

(Some fields are left out here; the [report reference](reference/report.md)
lists them all.) Check the exit code in scripts: `0` means a complete plan,
and other codes are described in the
[command reference](reference/cli.md#exit-codes).

## Next steps

- Add `.diffcone/` to your `.gitignore`. diffcone keeps a cache there, so
  later plans only re-read the files that changed.
- Read [How it works](concepts.md) to understand what makes a test
  selected.
- If almost every change selects almost every test, try
  [execution evidence](guides/evidence.md).

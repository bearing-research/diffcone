# Development

```bash
uv sync
uv run pytest                                   # all tests
uv run pytest tests/test_scenarios.py -k alias  # one scenario
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run ty check                                 # types
uv run --group docs zensical serve              # this site, locally
```

`tests/test_scenarios.py` holds the acceptance scenarios and
`tests/test_discovery.py` the discovery rules. Each builds a small git
repository with before and after commits and asserts exact target sets and
the reasons for them. The toolkit they use, `diffcone.testing`
(`FixtureRepo` and plan assertion helpers), is public, so a project
integrating diffcone can write the same kind of scenarios:

```python
import pytest

from diffcone.testing import FixtureRepo, selected


@pytest.fixture
def repo(tmp_path):
    return FixtureRepo(tmp_path / "repo")


def test_a_body_change_selects_its_callers(repo):
    base = repo.commit({"pkg/__init__.py": "", "pkg/ops.py": "def f():\n    return 1\n",
                        "tests/test_ops.py": "from pkg.ops import f\n\n\ndef test_f():\n    assert f()\n"})
    head = repo.commit({"pkg/ops.py": "def f():\n    return 2\n"})
    plan = repo.plan(base, head, [], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_ops.py::test_f"}
```

The rules for changing selection behaviour (any change that narrows
selection needs a regression scenario; recall comes before precision) are in
[`AGENTS.md`](https://github.com/bearing-research/diffcone/blob/main/AGENTS.md),
and every planned item has a design sketch in the [roadmap](roadmap.md).

The command reference is generated from the parser: after changing an
option, run `uv run python scripts/docs_cli.py` (a test fails until you
do).

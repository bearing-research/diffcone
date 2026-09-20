from __future__ import annotations

from pathlib import Path

import pytest

from diffcone.testing import FixtureRepo


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    return FixtureRepo(tmp_path / "repo", check_cache=True)

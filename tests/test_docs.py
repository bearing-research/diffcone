"""The documentation that is generated from the code is up to date."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_command_reference_matches_the_parser():
    spec = importlib.util.spec_from_file_location("docs_cli", ROOT / "scripts" / "docs_cli.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = (ROOT / "docs" / "reference" / "cli.md").read_text("utf-8")
    assert current == module.render(), "run: uv run python scripts/docs_cli.py"


def _documented_commands() -> list[tuple[str, str]]:
    """Every ``diffcone ...`` command in a ``bash`` or ``console`` code block
    of the README and the docs (in a console block only the ``$`` lines),
    with continuation lines joined."""
    import re

    found = []
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        text = path.read_text("utf-8")
        for language, block in re.findall(r"```(bash|console)\n(.*?)```", text, re.S):
            joined = re.sub(r"\\\n\s*", " ", block)
            for line in joined.splitlines():
                line = line.strip()
                if language == "console":
                    if not line.startswith("$ "):
                        continue
                    line = line[2:]
                for start in ("uv run diffcone ", "diffcone "):
                    if line.startswith(start):
                        command = line[len(start) :].split(" #", 1)[0]
                        found.append((f"{path.relative_to(ROOT)}: {line}", command))
    return found


def test_every_documented_command_parses():
    import shlex

    import pytest

    from diffcone.cli import build_parser

    commands = _documented_commands()
    assert len(commands) > 20
    for where, command in commands:
        words = shlex.split(command)
        if "--" in words:
            words = words[: words.index("--")]
        if words in (["--version"], ["--help"]):
            continue
        try:
            build_parser().parse_args(words)
        except SystemExit as exc:
            pytest.fail(f"{where}: does not parse ({exc})")

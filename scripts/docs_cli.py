"""Write docs/reference/cli.md from the command-line parser itself, so the
reference cannot drift from what the commands accept.

usage: docs_cli.py [--check]

``--check`` exits 1 when the file differs from what would be written (the
test suite runs the same comparison).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from diffcone.cli import build_parser

OUT = Path(__file__).resolve().parent.parent / "docs" / "reference" / "cli.md"

INTRO = """\
# Commands

Every command and its options. `diffcone <command> --help` prints the same
information. Wherever a command takes a snapshot (`--base`, `--head`,
`--rev`), you can pass a git revision, `INDEX` (your staged changes) or
`WORKTREE` (the files on disk).

## Exit codes

`plan` and `discover`:

- `0`: plan produced, analysis complete.
- `1`: plan produced, but analysis errors (a file that does not parse, a
  source root with no Python file) forced selecting everything.
- `2`: no plan (bad arguments, an unreadable manifest, an unknown revision,
  or an internal error).
- `3`: plan produced, but discovery may be short of what the runner
  collects.

`1` and `3` are opposite failures: `1` selects too much, `3` means the
target list itself may be short, so running only the selected targets
could skip tests. `3` wins when both apply. A manifest written by
`discover` keeps its notes, so a plan made from it exits `3` as well.

`run` exits with the runner's own code (0 when nothing was selected, or
with `--dry-run`; pytest's `5`, no test ran, counts as `0`), and with `3`
when a selected test was not collected. It refuses with `2` when the
working tree differs from the snapshot the plan analysed (unless
`--allow-mismatched-worktree`) or `--repo` is not the top of a git
repository, and with `3` when discovery may be incomplete (unless
`--allow-incomplete-discovery`).

`validate` and `corpus` exit `0` when every outcome change was selected,
`1` when some were missed, `2` on errors. `check` exits `0` when every new
failure of the full run was selected, `1` when the plan missed one, `2`
when an input cannot be read.
"""


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _sentence(text: str) -> str:
    """Capitalised, ending with a full stop."""
    text = " ".join(text.split())
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "?")) else text + "."


def _option(action: argparse.Action) -> str:
    if not action.option_strings:
        name = action.metavar or action.dest
        return f"`{name}`" if action.nargs not in ("*", "+") else f"`{name} ...` (after `--`)"
    flags = ", ".join(f"`{flag}`" for flag in sorted(action.option_strings, key=len, reverse=True))
    if action.nargs == 0:
        return flags
    long = max(action.option_strings, key=len)
    metavar = action.metavar or long.lstrip("-").upper().replace("-", "_")
    if isinstance(metavar, tuple):
        metavar = " ".join(metavar)
    if "|" in metavar:
        # A pipe inside a code span keeps its escaping backslash in a table.
        return f"{flags} <code>{metavar.replace('|', '&#124;')}</code>"
    return _cell(f"{flags} `{metavar}`")


def _default(action: argparse.Action) -> str:
    if action.nargs == 0 or action.default in (None, [], argparse.SUPPRESS):
        return ""
    return f"`{action.default}`"


def _help(action: argparse.Action) -> str:
    parts = [_sentence(action.help or "")]
    if action.choices and not isinstance(action, argparse._SubParsersAction):
        parts.append("One of " + ", ".join(f"`{c}`" for c in action.choices) + ".")
    if isinstance(action, argparse._AppendAction) and "repeatable" not in (action.help or ""):
        parts.append("Repeatable.")
    # Python 3.11's argparse marks a ``nargs="*"`` positional required.
    if action.required and action.option_strings:
        parts.insert(0, "**Required.**")
    # ``<repo>`` would be read as an HTML tag.
    text = " ".join(p for p in parts if p).replace("<", "&lt;").replace(">", "&gt;")
    return _cell(text)


def render() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    summaries = {c.dest: c.help or "" for c in sub._choices_actions}
    parts = [INTRO]
    for name, command in sub.choices.items():
        usage = command.format_usage().replace("usage: ", "").strip()
        usage = " ".join(usage.split())
        parts.append(f"## `diffcone {name}`\n")
        parts.append(_sentence(summaries.get(name, "")) + "\n")
        if command.description:
            parts.append(_sentence(command.description) + "\n")
        parts.append(f"```text\n{usage}\n```\n")
        rows = [
            a
            for a in command._actions
            if not isinstance(a, argparse._HelpAction) and a.help != argparse.SUPPRESS
        ]
        if rows:
            parts.append("| Option | Default | Description |\n|---|---|---|")
            parts.append(
                "\n".join(f"| {_option(a)} | {_default(a)} | {_help(a)} |" for a in rows) + "\n"
            )
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv[1:]:
        current = OUT.read_text("utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is out of date: run python scripts/docs_cli.py", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

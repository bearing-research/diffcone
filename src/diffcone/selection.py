"""Keep only the tests ``diffcone run`` selected: a pytest plugin.

``run`` used to name the selected tests on pytest's command line. pytest
imports the conftests of command-line paths while it parses its
configuration, and there a conftest that skips at module level
(``pytest.importorskip("tables")`` in pandas' ``tests/io/pytables``) raises
``Skipped`` past pytest's handler and kills the session; collected the
ordinary way, the same conftest skips its directory. So pytest collects from
its own starting points, as a full run does, and this plugin deselects every
item whose test is not selected.

Loaded into the project's process as ``-p diffcone_select`` (a link to this
file, as with the recorder), so it imports nothing of diffcone. Settings, from
the environment:

* ``DIFFCONE_SELECT``: a directory holding ``selected``, one selected test per
  line (``path::Class::test``, parameter cases folded); each process writes
  ``missing-<pid>`` there, the selected tests it did not collect, leaving out
  those under a directory or module that skipped at collection;
* ``DIFFCONE_COLLECT_ROOT``: the repository, which those paths are relative
  to (pytest's own node ids are relative to its rootdir, which may differ).
"""

from __future__ import annotations

import os
import re

import pytest

DIRECTORY = os.environ.get("DIFFCONE_SELECT")
ROOT = os.environ.get("DIFFCONE_COLLECT_ROOT") or os.getcwd()
skipped: list[str] = []  # repository-relative paths of collectors that skipped


def _relative(where) -> str:
    return os.path.relpath(str(where), ROOT).replace(os.sep, "/")


def _key(item) -> str:
    """The item's test as diffcone names it: the file relative to the
    repository, then the rest of the node id without the parameter case."""
    path = _relative(getattr(item, "path", None) or item.fspath)  # ``path`` from pytest 7
    if "::" not in item.nodeid:
        return path
    rest = re.sub(r"\[.*\]$", "", item.nodeid.split("::", 1)[1])
    return path + "::" + rest


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    if DIRECTORY and outcome.get_result().skipped:
        skipped.append(_relative(getattr(collector, "path", None) or collector.fspath))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    # First, so that a test the project's own options (-m, -k) deselect is
    # still counted as collected.
    if not DIRECTORY:
        return
    with open(os.path.join(DIRECTORY, "selected"), encoding="utf-8") as f:
        wanted = {line for line in f.read().splitlines() if line}
    keep, drop, found = [], [], set()
    for item in items:
        key = _key(item)
        if key in wanted:
            keep.append(item)
            found.add(key)
        else:
            drop.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
    with open(os.path.join(DIRECTORY, f"missing-{os.getpid()}"), "w", encoding="utf-8") as f:
        f.write(
            "\n".join(
                sorted(
                    test
                    for test in wanted - found
                    if not any(
                        test.split("::", 1)[0] == s or test.startswith(s + "/") for s in skipped
                    )
                )
            )
        )

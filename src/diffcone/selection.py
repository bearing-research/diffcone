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
  line (``path::Class::test``, parameter cases folded), and ``targets``, every
  target of the plan; each process writes ``missing-<pid>`` there, the
  selected tests it did not collect, leaving out those under a directory or
  module that skipped at collection, and ``unknown-<pid>``, the collected
  tests that are no target at all: those are kept and run, since the plan
  cannot say whether a change reaches them (a plugin's own collection, an
  option discovery did not see);
* ``DIFFCONE_COLLECT_ROOT``: the repository, which those paths are relative
  to (pytest's own node ids are relative to its rootdir, which may differ);
* ``DIFFCONE_OUTCOMES``: a directory; each process writes ``outcomes-<pid>``,
  one ``nodeid<TAB>OUTCOME`` line per report (``PASSED``, ``FAILED``,
  ``SKIPPED``, ``XFAIL``, ``XPASS``, ``ERROR``), as ``validate`` reads them:
  pytest's console output depends on the project's verbosity options.
"""

from __future__ import annotations

import os
import re

import pytest

DIRECTORY = os.environ.get("DIFFCONE_SELECT")
OUTCOMES = os.environ.get("DIFFCONE_OUTCOMES")
_ROOT = os.environ.get("DIFFCONE_COLLECT_ROOT") or os.getcwd()
# The repository as given and with symlinks resolved: pytest reports paths
# under whichever its rootdir is, and a linked or relative ``--repo`` differs.
ROOTS = tuple(dict.fromkeys([os.path.realpath(_ROOT), os.path.abspath(_ROOT)]))
skipped: list[str] = []  # repository-relative paths of collectors that skipped


def _relative(where) -> str:
    path = os.path.abspath(str(where))
    for root in ROOTS:
        if path.startswith(root + os.sep):
            return os.path.relpath(path, root).replace(os.sep, "/")
    return os.path.relpath(os.path.realpath(path), ROOTS[0]).replace(os.sep, "/")


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
    targets_file = os.path.join(DIRECTORY, "targets")
    known: set[str] | None = None
    if os.path.exists(targets_file):
        with open(targets_file, encoding="utf-8") as f:
            known = {line for line in f.read().splitlines() if line}
    keep, drop, found, unknown = [], [], set(), set()
    for item in items:
        key = _key(item)
        if key in wanted:
            keep.append(item)
            found.add(key)
        elif known is not None and key not in known:
            keep.append(item)
            unknown.add(key)
        else:
            drop.append(item)
    with open(os.path.join(DIRECTORY, f"unknown-{os.getpid()}"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(unknown)))
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


def pytest_runtest_logreport(report):
    if not OUTCOMES:
        return
    outcome = None
    xfail = hasattr(report, "wasxfail")
    if report.when == "call":
        if xfail:
            outcome = "XPASS" if report.passed else "XFAIL"
        else:
            outcome = report.outcome.upper()
    elif report.failed:
        outcome = "ERROR"  # in setup or teardown
    elif report.when == "setup" and report.skipped:
        outcome = "XFAIL" if xfail else "SKIPPED"
    if outcome is not None:
        path = os.path.join(OUTCOMES, f"outcomes-{os.getpid()}")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{report.nodeid}\t{outcome}\n")

"""Regression scenarios for what round 3 left known (internal/audit.md,
"Known after round 3"): W3 (project code a test runs as a subprocess) and
W4 (writes through an argument or a receiver).

Each test names the finding it guards and failed before its fix.
"""

from __future__ import annotations

import sys

import pytest

from diffcone.testing import asv_target, path_ids, reason, selected

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")

BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "pkg/other.py": "def g():\n    return 3\n",
    "tests/__init__.py": "",
    "tests/test_other.py": "from pkg.other import g\n\n\ndef test_g():\n    assert g() == 3\n",
    "benchmarks/__init__.py": "",
    "benchmarks/bench.py": "from pkg.other import g\n\n\ndef time_g():\n    g()\n",
}
BENCH = asv_target("bench.time_g", "benchmarks.bench.time_g")
OTHER = "tests/test_other.py::test_g"


def _static(repo, files, change):
    base = repo.commit({**BASE, **files})
    head = repo.commit(change)
    plan = repo.plan(base, head, [BENCH], discover_runners=["pytest"])
    chosen = selected(plan)
    assert OTHER not in chosen and BENCH.runner_id not in chosen, [
        (r.rule, r.detail, [(s.source, s.target, s.kind) for s in r.path])
        for d in plan.selected
        if d.target.runner_id == OTHER
        for r in d.reasons
    ]
    return chosen


# --------------------------------------------------------------------------- W3

# A script that imports project code, and the module it imports.
GEN = "from pkg.core import value\n\nprint(value())\n"
CORE = {"pkg/core.py": "def value():\n    return 1\n"}

RUNNERS = {
    "relative path": "subprocess.run([sys.executable, 'scripts/gen.py'], check=True)",
    "os.path.join": (
        "subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'gen.py')], check=True)"
    ),
    "Path division": (
        "subprocess.run([sys.executable, str(pathlib.Path(ROOT) / 'scripts' / 'gen.py')])"
    ),
    "Path(__file__)": (
        "subprocess.run([sys.executable, pathlib.Path(__file__).parents[1] / 'scripts/gen.py'])"
    ),
    "f-string": "subprocess.run([sys.executable, f'{ROOT}/scripts/gen.py'])",
    "-m": "subprocess.run([sys.executable, '-m', 'scripts.gen'], check=True)",
    "-m of a package": "subprocess.run([sys.executable, '-u', '-m', 'scripts'], check=True)",
    "-c": "subprocess.run([sys.executable, '-c', 'import scripts.gen'], check=True)",
    "-c through a local": (
        "code = textwrap.dedent('''\n        import scripts.gen\n    ''')\n"
        "    subprocess.run([sys.executable, '-c', code], check=True)"
    ),
    "coverage run": (
        "subprocess.run([sys.executable, '-m', 'coverage', 'run', '-m', 'scripts.gen'])"
    ),
    "shell string": "subprocess.run(f'{sys.executable} -m scripts.gen', shell=True)",
    "-m under a prefix": (
        "subprocess.run(f'{sys.executable} -m scripts.{os.getenv(\"N\")}', shell=True)"
    ),
    "os.system": "os.system('python scripts/gen.py')",
    "a helper's literal": "run_python('scripts/gen.py')",
}


def _runner_test(call: str) -> str:
    return (
        "import os\nimport pathlib\nimport subprocess\nimport sys\nimport textwrap\n\n"
        "from tests.helpers import run_python\n\n"
        "ROOT = os.path.dirname(os.path.dirname(__file__))\n\n\n"
        f"def test_script():\n    {call}\n"
    )


# A helper in test code that runs the script it is handed by path: the
# program is a parameter (any program), and the caller's literal names it.
HELPERS = (
    "import subprocess\nimport sys\n\n\n"
    "def run_python(script):\n    subprocess.run([sys.executable, script], check=True)\n"
)


@pytest.mark.parametrize("change", ["script", "imported module"])
@pytest.mark.parametrize("how", list(RUNNERS))
def test_w3_a_test_running_a_script_depends_on_it(repo, how, change):
    files = {
        **CORE,
        "scripts/__init__.py": "",
        "scripts/__main__.py": "import scripts.gen  # noqa: F401\n",
        "scripts/gen.py": GEN,
        "tests/helpers.py": HELPERS,
        "tests/test_script.py": _runner_test(RUNNERS[how]),
    }
    edit = (
        {"scripts/gen.py": GEN.replace("value()", "value() + 1")}
        if change == "script"
        else {"pkg/core.py": "def value():\n    return 2\n"}
    )
    assert _static(repo, files, edit) == {"tests/test_script.py::test_script"}


DYNAMIC_SCRIPT = "import importlib\n\nimportlib.import_module('pkg.' + 'core').value()\n"


@pytest.mark.parametrize("script", [GEN, DYNAMIC_SCRIPT], ids=["imports", "imports by name"])
def test_w3_a_script_no_module_name_maps_to(repo, script):
    """``scripts/gen-data.py`` is a file the index does not read: a test
    running it sees a change to it, and what the script reaches through its
    imports (any change, when it imports by a name it computes); a script
    nothing names stays unseen (a conftest pytest never loads,
    ``test_a_conftest_pytest_does_not_load_is_not_read``)."""
    files = {
        **CORE,
        "scripts/gen-data.py": script,
        "scripts/unused-tool.py": script,
        "tests/test_script.py": _runner_test(RUNNERS["relative path"]).replace(
            "gen.py", "gen-data.py"
        ),
        "tests/helpers.py": HELPERS,
    }
    repo.commit({**BASE, **files})

    def plan_change(change):
        return repo.plan(
            repo.git("rev-parse", "HEAD").strip(),
            repo.commit(change),
            [BENCH],
            discover_runners=["pytest"],
        )

    plan = plan_change({"scripts/gen-data.py": script + "\n# changed\nX = 1\n"})
    assert selected(plan) == {"tests/test_script.py::test_script"}
    (why,) = [r for d in plan.selected for r in d.reasons]
    assert why.rule == "unanalysed_file_changed" and "scripts/gen-data.py" in why.detail
    assert selected(plan_change({"scripts/unused-tool.py": script + "X = 2\n"})) == set()
    core = plan_change({"pkg/core.py": "def value():\n    return 2\n"})
    assert selected(core) == {"tests/test_script.py::test_script"}
    elsewhere = selected(plan_change({"pkg/other.py": "def g():\n    return 4\n"}))
    assert elsewhere - {OTHER, BENCH.runner_id} == (
        set() if script == GEN else {"tests/test_script.py::test_script"}
    )


DYNAMIC = {
    "a script path from elsewhere": "subprocess.run([sys.executable, os.environ['SCRIPT']])",
    "a module from elsewhere": "subprocess.run([sys.executable, '-m', os.environ['MOD']])",
    "code built at run time": "subprocess.run([sys.executable, '-c', 'import ' + NAME])",
    "standard input": "subprocess.run([sys.executable, '-'], input=CODE)",
    "starred arguments": "subprocess.run([sys.executable, *ARGS])",
    "pytest on a directory": "subprocess.run([sys.executable, '-m', 'pytest', 'tests/sub'])",
    "a command line": "subprocess.run(f'{sys.executable} -c \"import {NAME}\"', shell=True)",
    "a concatenated list": "subprocess.run([sys.executable] + ARGS)",
}


@pytest.mark.parametrize("how", list(DYNAMIC))
def test_w3_a_python_program_built_at_run_time_can_run_anything(repo, how):
    test = _runner_test(DYNAMIC[how]).replace(
        "ROOT =", "NAME = 'pkg.core'\nCODE = b'import pkg.core'\nARGS = ['scripts/gen.py']\nROOT ="
    )
    files = {**CORE, "tests/helpers.py": HELPERS, "tests/test_script.py": test}
    assert _static(repo, files, {"pkg/core.py": "def value():\n    return 2\n"}) == {
        "tests/test_script.py::test_script"
    }


PRECISE = {
    "-c code importing nothing of ours": "subprocess.run([sys.executable, '-c', 'print(1)'])",
    "--version": "subprocess.run([sys.executable, '--version'])",
    "another module": "subprocess.run([sys.executable, '-m', 'venv', str(ROOT)])",
    "a command line": "subprocess.run(f'{sys.executable} -c \"import sys; print(1)\"')",
    "a git command": "subprocess.run(['git', 'commit', '-m', MESSAGE])",
    "interpreter paths": "assert [p for p in (sys.executable, str(sys.executable))]",
    "a list of words": "assert {'python'} <= set(['python', 'recursion', MESSAGE])",
    "a docstring naming a file": "'''Like scripts/gen.py and python -m scripts.gen.'''",
}


@pytest.mark.parametrize("how", list(PRECISE))
def test_w3_a_python_program_that_runs_nothing_of_ours(repo, how):
    test = _runner_test(PRECISE[how]).replace("ROOT =", "MESSAGE = 'x'\nROOT =")
    files = {
        **CORE,
        "scripts/__init__.py": "",
        "scripts/gen.py": GEN,
        "tests/helpers.py": HELPERS,
        "tests/test_script.py": test,
    }
    assert _static(repo, files, {"pkg/core.py": "def value():\n    return 2\n"}) == set()


BOUNDED = {
    "a helper's literal": "run_python('scripts/gen.py')",
    "a local path": (
        "path = pathlib.Path(__file__).parents[1] / 'scripts' / 'gen.py'\n"
        "    subprocess.run([sys.executable, str(path)])"
    ),
    "a module's __file__": (
        "subprocess.run([sys.executable, '-m', 'coverage', 'run', MOD.__file__])"
    ),
}


@pytest.mark.parametrize("how", list(BOUNDED))
def test_w3_a_named_program_is_not_any_program(repo, how):
    """The program is named (through a helper's parameter, a local, a
    module's ``__file__``): a change elsewhere does not select the test."""
    test = _runner_test(BOUNDED[how]).replace("ROOT =", "import scripts.gen as MOD\n\nROOT =")
    files = {
        **CORE,
        "scripts/__init__.py": "",
        "scripts/gen.py": GEN,
        "tests/helpers.py": HELPERS,
        "tests/test_script.py": test,
    }
    base = repo.commit({**BASE, **files})
    head = repo.commit({"pkg/other.py": "def g():\n    return 4\n"})
    plan = repo.plan(base, head, [BENCH], discover_runners=["pytest"])
    assert selected(plan) == {OTHER, BENCH.runner_id}


@needs_monitoring
def test_w3_evidence_follows_the_child_itself(repo):
    """Evidence mode records the child process: the test is selected by a
    change to what the script ran, not by any change."""
    files = {
        **CORE,
        "scripts/__init__.py": "",
        "scripts/gen.py": GEN,
        "tests/test_script.py": (
            "import subprocess\nimport sys\n\n\ndef test_script():\n"
            "    subprocess.run([sys.executable, '-m', 'scripts.gen'], check=True)\n"
        ),
    }
    base = repo.commit({**BASE, **files})
    evidence = repo.collect()
    for change, want in [
        ({"pkg/core.py": "def value():\n    return 2\n"}, {"tests/test_script.py::test_script"}),
        (
            {"pkg/core.py": CORE["pkg/core.py"], "pkg/other.py": "def g():\n    return 4\n"},
            {OTHER, BENCH.runner_id},
        ),
    ]:
        head = repo.commit(change)
        plan = repo.plan(base, head, [BENCH], discover_runners=["pytest"], evidence=evidence)
        assert selected(plan) == want


@needs_monitoring
@pytest.mark.parametrize(
    "program", ["scripts/gen-data.py", "os.environ.get('S', 'scripts/gen.py')"]
)
def test_w3_evidence_a_script_no_module_name_maps_to(repo, program):
    """The child runs a file the index does not read (or one it cannot
    name): a change to that file reaches the test in evidence mode too."""
    script = program if program.endswith(".py") else "scripts/gen.py"
    arg = repr(program) if program.endswith(".py") else program
    files = {
        **CORE,
        script: GEN,
        "tests/test_script.py": (
            "import os\nimport subprocess\nimport sys\n\n\ndef test_script():\n"
            f"    subprocess.run([sys.executable, {arg}], check=True)\n"
        ),
    }
    base = repo.commit({**BASE, **files})
    evidence = repo.collect()
    head = repo.commit({script: GEN + "X = 1\n"})
    plan = repo.plan(base, head, [BENCH], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/test_script.py::test_script"}


# --------------------------------------------------------------------------- W4

REGISTRY = "REG = {}\n\n\ndef get():\n    return REG.get('k')\n"
READS_REG = "from pkg.registry import get\n\n\ndef test_b():\n    assert get() == 'slow'\n"
WRITER = "def set_mode(d, v='slow'):\n    d['k'] = v\n"
CALLS_AT_IMPORT = "from pkg.registry import REG\nfrom pkg.state import set_mode\n\nset_mode(REG)\n"
W4_FILES = {
    "pkg/registry.py": REGISTRY,
    "pkg/state.py": WRITER,
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": CALLS_AT_IMPORT,
    "tests/sub/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": READS_REG,
}


@pytest.mark.parametrize(
    "change",
    [
        {"pkg/state.py": WRITER.replace("'slow'", "'fast'")},
        {"tests/sub/conftest.py": CALLS_AT_IMPORT.replace("set_mode(REG)", "set_mode(REG, 'x')")},
    ],
    ids=["writer body", "call argument"],
)
def test_w4_a_call_at_import_writing_through_its_argument(repo, change):
    """``set_mode(REG)`` in a conftest fills ``REG`` through the parameter
    ``d``: test_b reads it without importing the conftest."""
    chosen = _static(repo, W4_FILES, change)
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


# How the call at import reaches the state it changes: through a helper,
# a local alias, a keyword, ``setattr``, a method of an unknown receiver.
WRITE_SHAPES = {
    "helper": (
        "def set_mode(d, v='slow'):\n    _store(d, v)\n\n\ndef _store(m, v):\n    m['k'] = v\n",
        "set_mode(REG)",
    ),
    "local alias": ("def set_mode(d, v='slow'):\n    m = d\n    m.update(k=v)\n", "set_mode(REG)"),
    "keyword": ("def set_mode(v='slow', *, d):\n    d['k'] = v\n", "set_mode(d=REG)"),
    "setattr": ("def set_mode(d, v='slow'):\n    setattr(d, 'k', v)\n", "set_mode(REG)"),
    "unresolved receiver": (
        "class Filler:\n    def fill(self, d, v='slow'):\n        d['k'] = v\n\n\n"
        "def filler():\n    return [Filler()]\n",
        "filler()[0].fill(REG)",
    ),
}


@pytest.mark.parametrize("shape", list(WRITE_SHAPES))
def test_w4_how_a_call_writes_through_its_argument(repo, shape):
    writer, call = WRITE_SHAPES[shape]
    conftest = CALLS_AT_IMPORT.replace("set_mode(REG)", call)
    if "filler" in call:
        conftest = conftest.replace("import set_mode", "import filler")
    files = {**W4_FILES, "pkg/state.py": writer, "tests/sub/conftest.py": conftest}
    chosen = _static(repo, files, {"pkg/state.py": writer.replace("'slow'", "'fast'")})
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


RECEIVER = (
    "class Registry:\n    def __init__(self):\n        self.items = []\n\n"
    "    def register(self, x):\n        self._add(x)\n\n"
    "    def _add(self, x):\n        self.items.append(x)\n\n\n"
    "registry = Registry()\n\n\ndef get():\n    return registry.items\n"
)
RECEIVER_FILES = {
    "pkg/registry.py": RECEIVER,
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": "from pkg.registry import registry\n\nregistry.register('slow')\n",
    "tests/sub/test_a.py": "def test_a():\n    pass\n",
    "tests/test_b.py": (
        "from pkg.registry import get\n\n\ndef test_b():\n    assert get() == ['slow']\n"
    ),
}


STATE = (
    'MODE = ["slow"]\n\n\n'
    "def set_mode(m):\n    _store(m)\n    return m\n\n\n"
    "def _store(m):\n    MODE[0] = m\n\n\n"
    "def mode():\n    return MODE[0]\n"
)


@pytest.mark.parametrize(
    "caller",
    [
        "pkg/config.py",  # at import, in the library
        "tests/sub/conftest.py",  # at import, in a conftest
        "tests/sub/test_a.py",  # in a test
    ],
)
def test_w4_a_changed_body_reaches_what_its_callees_write(repo, caller):
    """``set_mode`` stores what it is given through ``_store``; a change to
    what it hands on (``_store(m + "!")``) changes ``MODE``, read by test_b
    through ``mode`` without running ``set_mode``."""
    call = "from pkg.state import set_mode\n\nX = set_mode('slow')\n"
    files = {
        "pkg/state.py": STATE,
        "tests/sub/__init__.py": "",
        "tests/sub/test_a.py": "import pkg.config  # noqa: F401\n\n\ndef test_a():\n    pass\n",
        "tests/test_b.py": (
            "from pkg.state import mode\n\n\ndef test_b():\n    assert mode() == 'slow'\n"
        ),
    }
    if caller == "tests/sub/test_a.py":
        files[caller] = "from pkg.state import set_mode\n\n\ndef test_a():\n    set_mode('slow')\n"
    else:
        files[caller] = call
    edit = STATE.replace("    _store(m)\n", "    _store(m + '!')\n")
    chosen = _static(repo, files, {"pkg/state.py": edit})
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_w4_a_changed_value_handed_to_a_writer(repo):
    """``setup`` hands ``_store`` what ``compute`` returns: a change to
    ``compute`` changes ``MODE`` though neither ``setup`` nor the writer
    changed. The explanation names the real call chain."""
    state = (
        'MODE = ["slow"]\n\n\n'
        "def compute():\n    return 'slow'\n\n\n"
        "def setup():\n    _store(compute())\n\n\n"
        "def _store(m):\n    MODE[0] = m\n\n\n"
        "def mode():\n    return MODE[0]\n"
    )
    files = {
        "pkg/state.py": state,
        "tests/sub/__init__.py": "",
        "tests/sub/conftest.py": "from pkg.state import setup\n\nsetup()\n",
        "tests/sub/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": (
            "from pkg.state import mode\n\n\ndef test_b():\n    assert mode() == 'slow'\n"
        ),
    }
    base = repo.commit({**BASE, **files})
    head = repo.commit({"pkg/state.py": state.replace("return 'slow'", "return 'fast'")})
    plan = repo.plan(base, head, [BENCH], discover_runners=["pytest"])
    assert selected(plan) == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}
    why = reason(plan, "tests/test_b.py::test_b")
    assert path_ids(why)[-4:] == [
        "pkg.state.MODE",
        "pkg.state._store",
        "pkg.state.setup",
        "pkg.state.compute",
    ], path_ids(why)
    assert [s.kind for s in why.path][-3:] == ["references", "called_by", "references"]
    assert why.path[-3].detail == "mutated_by"


@needs_monitoring
@pytest.mark.parametrize(
    "case",
    ["argument: writer body", "argument: call", "receiver: writer body", "receiver: call"],
)
def test_w4_evidence_a_call_at_import_writing_through_an_argument(repo, case):
    """The same in evidence mode: test_b never runs the conftest's call."""
    if case.startswith("argument"):
        files = W4_FILES
        change = (
            {"pkg/state.py": WRITER.replace("'slow'", "'fast'")}
            if case.endswith("body")
            else {"tests/sub/conftest.py": CALLS_AT_IMPORT.replace("(REG)", "(REG, 'x')")}
        )
    else:
        files = RECEIVER_FILES
        conftest = files["tests/sub/conftest.py"]
        change = (
            {"pkg/registry.py": RECEIVER.replace("self.items.append(x)", "self.items.append(1)")}
            if case.endswith("body")
            else {"tests/sub/conftest.py": conftest.replace("slow", "fast")}
        )
    base = repo.commit({**BASE, **files})
    evidence = repo.collect()
    head = repo.commit(change)
    plan = repo.plan(base, head, [BENCH], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_w4_a_call_at_import_writing_through_its_receiver(repo):
    """``registry.register(x)`` changes ``registry`` (``register`` appends to
    ``self.items`` through a helper): test_b reads it through ``get``."""
    conftest = RECEIVER_FILES["tests/sub/conftest.py"]
    chosen = _static(
        repo, RECEIVER_FILES, {"tests/sub/conftest.py": conftest.replace("slow", "fast")}
    )
    assert chosen == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


@pytest.mark.parametrize(
    ("value", "base", "reaches"),
    [
        ("Recorder()", "", True),  # an instance of ours whose ``info`` writes it
        ("make()", "", True),  # what a factory returns: any ``info`` of ours
        ("{'name': 'x'}", "", False),  # a dict display: only its own mutators
        # A third-party value: no ``info`` of ours, unless a class of ours
        # derives from a third-party one (``logging.setLoggerClass``).
        ("logging.getLogger('x')", "", False),
        ("logging.getLogger('x')", "logging.Logger", True),
        # A wrapper whose every return is a third-party call: the same,
        # whatever ``cast`` says (W4 on strata: ``logger.info`` matched a
        # test fake's ``info``).
        ("get_logger('x')", "", False),
        ("get_logger('x')", "logging.Logger", True),
    ],
)
def test_w4_a_method_called_on_a_variable_writes_it_when_it_may_be_ours(repo, value, base, reaches):
    log = (
        "import logging\nfrom typing import cast\n\n\n"
        f"class Recorder({base}):\n    def info(self, m):\n        self.last = m\n\n\n"
        "def make():\n    return Recorder()\n\n\n"
        "def get_logger(name):\n    if not name:\n        return logging.getLogger()\n"
        "    return cast(Recorder, logging.getLogger(name))\n\n\n"
        f"log = {value}\n\n\n"
        "def get():\n    return log\n"
    )
    files = {
        "pkg/log.py": log,
        "tests/sub/__init__.py": "",
        "tests/sub/conftest.py": "from pkg.log import log\n\nlog.info('slow')\n",
        "tests/sub/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": "from pkg.log import get\n\n\ndef test_b():\n    assert get()\n",
    }
    change = {"tests/sub/conftest.py": "from pkg.log import log\n\nlog.info('fast')\n"}
    want = {"tests/sub/test_a.py::test_a"} | ({"tests/test_b.py::test_b"} if reaches else set())
    assert _static(repo, files, change) == want


# W15: a write through a module attribute and an item or method.
W15_STORE = (
    "_CACHE = {'l': [], 'x': 1, 'y': 2, 'n': {}}\nREG = []\n\n\n"
    "def get():\n    return _CACHE.get('k'), list(REG)\n"
)
W15_WRITES = {
    "item through the module": "from pkg import store\n\nstore._CACHE['k'] = 'slow'\n",
    "method through the module": "from pkg import store\n\nstore.REG.append('slow')\n",
    "item through the package": "import pkg.store\n\npkg.store._CACHE['k'] = 'slow'\n",
    "update through an alias": "import pkg.store as s\n\ns._CACHE.update(k='slow')\n",
    "method on an item": "from pkg import store\n\nstore._CACHE['l'].append('slow')\n",
    "augmented item": "from pkg import store\n\nstore._CACHE['l'] += ['slow']\n",
    "deleted item": "from pkg import store\n\ndel store._CACHE['x' if 'slow' else 'y']\n",
    "nested item": "from pkg import store\n\nstore._CACHE['n']['j'] = 'slow'\n",
    "in a function": (
        "from pkg import store\n\n\ndef fill():\n    store._CACHE['k'] = 'slow'\n\n\nfill()\n"
    ),
}


def _w15_files(how):
    return {
        "pkg/store.py": W15_STORE,
        "tests/sub/__init__.py": "",
        "tests/sub/conftest.py": W15_WRITES[how],
        "tests/sub/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": "from pkg.store import get\n\n\ndef test_b():\n    assert get()\n",
    }


@pytest.mark.parametrize("how", list(W15_WRITES))
def test_w15_a_write_through_a_module_attribute(repo, how):
    files = _w15_files(how)
    change = {"tests/sub/conftest.py": W15_WRITES[how].replace("slow", "fast")}
    assert _static(repo, files, change) == {
        "tests/sub/test_a.py::test_a",
        "tests/test_b.py::test_b",
    }


@needs_monitoring
@pytest.mark.parametrize("how", list(W15_WRITES))
def test_w15_evidence_a_write_through_a_module_attribute(repo, how):
    base = repo.commit({**BASE, **_w15_files(how)})
    evidence = repo.collect()
    head = repo.commit({"tests/sub/conftest.py": W15_WRITES[how].replace("slow", "fast")})
    plan = repo.plan(base, head, [BENCH], discover_runners=["pytest"], evidence=evidence)
    assert selected(plan) == {"tests/sub/test_a.py::test_a", "tests/test_b.py::test_b"}


def test_w4_locals_bound_to_each_other_are_no_loop(repo):
    """``a = b[0]`` and ``b = a[0]``: following what a local holds stops."""
    files = {
        "pkg/state.py": WRITER,
        "tests/test_loop.py": (
            "from pkg.state import set_mode\n\n\ndef test_loop():\n"
            "    if False:\n        a = b[0]  # noqa: F821\n        b = a[0]\n        set_mode(a)\n"
        ),
    }
    chosen = _static(repo, files, {"pkg/state.py": WRITER.replace("'slow'", "'fast'")})
    assert chosen == {"tests/test_loop.py::test_loop"}

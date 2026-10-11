"""Regression scenarios for audit round 5, batch B: the pass-2 walker,
programs and text a test runs, and objects handed to code the analysis
does not follow.

* R8: the interpreter need not be ``argv[0]`` (``env``, ``uv run``, a
  launcher held in a name, ``os.environ.get("PYTHON", sys.executable)``),
  and a command may be a call's own arguments (``create_subprocess_exec``).
* R12: text run as code in-process (``exec``, ``eval``, ``compile``) is read
  when written out; text from elsewhere is unbounded.
* R13: a ``pytester``/``testdir`` test depends on the project's plugins and
  on what its inline texts import.
* R14: a test file given to a test runner runs whole: its tests, fixtures
  and conftests, not only its import-time code.
* R17: ``match`` class patterns read attributes by name.
* R18: names in strings the stdlib consumes (``attrgetter``,
  ``methodcaller``, ``str.format`` fields).
* R19: an instance handed to code the analysis does not follow (a library,
  ``dict(x)``, ``print(file=w)``, a method on an unknown receiver) escapes.
* R25: a module obtained by ``import_module``/``__import__`` and handed on
  is a module handed on.

Every scenario asserts exact target sets, with an ASV target beside the
pytest ones that no change reaches, and each failed before its fix.
"""

from __future__ import annotations

import pytest

from diffcone.testing import asv_target, py_target, rules, selected

NOOP = asv_target("bench_noop.time_noop", "benchmarks.bench_noop.time_noop")
BASE = {
    ".gitignore": "__pycache__/\n.diffcone/\n",
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "benchmarks/__init__.py": "",
    "benchmarks/bench_noop.py": "def time_noop():\n    pass\n",
}
OPS = "def value():\n    return 1\n\n\ndef other():\n    return 10\n"
OPS_VALUE = OPS.replace("return 1\n", "return 2\n")
OPS_OTHER = OPS.replace("return 10\n", "return 11\n")
# A test that imports nothing of the project: no rule but select-all reaches it.
CONTROL = "def test_c():\n    assert True\n"
T_A = py_target("tests/test_a.py::test_a", "tests.test_a.test_a")
T_C = py_target("tests/test_c.py::test_c", "tests.test_c.test_c")
T_X = py_target("tests/test_x.py::test_x", "tests.test_x.test_x")
TEST_X = "from pkg import api\n\n\ndef test_x():\n    assert api.run() == 1\n"


def _plan(repo, base, head, *targets, **kwargs):
    return repo.plan(base, head, [*targets, NOOP], source_roots=["."], **kwargs)


def _api_scenario(repo, mod, api, mod_changed, mod_other):
    """``tests/test_x.py`` calls ``pkg.api.run``, which uses ``pkg.mod``:
    the test is selected on ``mod_changed`` and not on ``mod_other``."""
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": mod,
            "pkg/tmpl.py": "FMT = '{0.v}'\n",
            "pkg/api.py": api,
            "tests/test_x.py": TEST_X,
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/mod.py": mod_changed}), T_X, T_C)
    assert selected(plan) == {T_X.runner_id}
    if mod_other is not None:
        plan2 = _plan(repo, base, repo.commit({"pkg/mod.py": mod_other}), T_X, T_C)
        assert selected(plan2) == set()
    return plan


# --------------------------------------------------------------------------- R8

CHILD = {
    "pkg/work.py": "def handle(x):\n    return x + 1\n",
    "pkg/tool.py": (
        'from pkg.work import handle\n\nif __name__ == "__main__":\n    print(handle(1))\n'
    ),
}
HANDLED = "def handle(x):\n    return x + 2\n"
CHILD_TEST = (
    "import asyncio\nimport os\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n"
    "LAUNCHER = Path('/opt/uv')\n\n\ndef test_a():\n    {call}\n"
)
PYTHON_TOOL = '"-m", "pkg.tool"'


@pytest.mark.parametrize(
    "call",
    [
        f'subprocess.run(["env", "X=1", sys.executable, {PYTHON_TOOL}])',
        f'subprocess.run([str(LAUNCHER), "run", sys.executable, {PYTHON_TOOL}])',
        'subprocess.run(["timeout", "30", sys.executable, "pkg/tool.py"])',
        'subprocess.run(["env", "OMPI=1", "--", sys.executable, "pkg/tool.py"])',
        f'subprocess.run([os.environ.get("PYTHON", sys.executable), {PYTHON_TOOL}])',
        f"asyncio.run(asyncio.create_subprocess_exec(sys.executable, {PYTHON_TOOL}))",
        f"os.execl(sys.executable, sys.executable, {PYTHON_TOOL})",
        'subprocess.run("X=1 " + sys.executable + " -m pkg.tool", shell=True)',
        'subprocess.run(f"X=1 {sys.executable} -m pkg.tool", shell=True)',
        'subprocess.run("env X=1 python -m pkg.tool", shell=True)',
    ],
    ids=[
        "env",
        "launcher-in-a-name",
        "timeout",
        "double-dash",
        "environ-get",
        "create_subprocess_exec",
        "execl",
        "concatenated-shell-line",
        "f-string-shell-line",
        "shell-line-with-env",
    ],
)
def test_r8_the_interpreter_need_not_be_the_first_item(repo, call):
    """A command the test starts runs ``pkg.tool`` whatever stands before
    the interpreter; the program it runs is what changed."""
    base = repo.commit(
        {
            **BASE,
            **CHILD,
            "tests/test_a.py": CHILD_TEST.format(call=call),
            "tests/test_c.py": CONTROL,
        }
    )
    head = repo.commit({"pkg/work.py": HANDLED})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dependency"}


def test_r8_a_program_from_elsewhere_after_a_launcher_is_unbounded(repo):
    """``["env", "X=1", sys.executable, script]`` with a script nothing
    names: the test may run anything, as with the interpreter first."""
    call = 'subprocess.run(["env", "X=1", sys.executable, os.environ["SCRIPT"]])'
    base = repo.commit(
        {
            **BASE,
            **CHILD,
            "pkg/other.py": "def g():\n    return 1\n",
            "tests/test_a.py": CHILD_TEST.format(call=call),
            "tests/test_c.py": CONTROL,
        }
    )
    head = repo.commit({"pkg/other.py": "def g():\n    return 2\n"})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dynamic_reference"}


# --------------------------------------------------------------------------- R12

IMPORT_OPS = "from pkg import ops"
EXEC_TESTS = {
    "literal": f"def test_a():\n    ns = {{}}\n    exec('{IMPORT_OPS}\\nres = ops.value()', ns)\n",
    "template-format": (
        f"TEMPLATE = '{IMPORT_OPS}\\nres = ops.{{fn}}()'\n\n\n"
        "def test_a():\n    ns = {}\n    exec(TEMPLATE.format(fn='value'), ns)\n"
    ),
    "template-percent": (
        f"TEMPLATE = '{IMPORT_OPS}\\nres = ops.%s()'\n\n\n"
        "def test_a():\n    ns = {}\n    exec(TEMPLATE % 'value', ns)\n"
    ),
    "f-string-literal": (
        f"def test_a():\n    fn = 'value'\n    exec(f'{IMPORT_OPS}\\nres = ops.{{fn}}()')\n"
    ),
    "eval-dunder-import": (
        "def test_a():\n    assert eval('__import__(\"pkg.ops\").ops.value()') == 1\n"
    ),
    "compile-in-helper": (
        f"SRC = '{IMPORT_OPS}\\nres = ops.value()'\n\n\n"
        "def run_src(src):\n    ns = {}\n    exec(compile(src, '<t>', 'exec'), ns)\n"
        "    return ns\n\n\ndef test_a():\n    assert run_src(SRC)['res'] == 1\n"
    ),
    "parameter-fills-a-field": (
        "def make(body):\n    d = {}\n    exec(f'def fn():\\n    {body}', {}, d)\n"
        "    return d['fn']\n\n\n"
        f"def test_a():\n    assert make('{IMPORT_OPS}; return ops.value()')() == 1\n"
    ),
    "dedent": (
        "import textwrap\n\n\ndef test_a():\n    exec(textwrap.dedent('''\n"
        f"        {IMPORT_OPS}\n        res = ops.value()\n    '''))\n"
    ),
    "timeit": (
        "import timeit\n\n\ndef test_a():\n"
        f"    timeit.timeit('ops.value()', setup='{IMPORT_OPS}', number=1)\n"
    ),
    "interactive": (
        "import code\n\n\ndef test_a():\n    i = code.InteractiveInterpreter()\n"
        f"    i.runsource('{IMPORT_OPS}; ops.value()')\n"
    ),
}


@pytest.mark.parametrize("text", list(EXEC_TESTS.values()), ids=list(EXEC_TESTS))
def test_r12_text_written_out_is_read_as_the_code_it_is(repo, text):
    """Code a test runs as text imports ``pkg.ops`` and calls ``value``: a
    body change of ``value`` reaches the test, and nothing else does."""
    base = repo.commit(
        {**BASE, "pkg/ops.py": OPS, "tests/test_a.py": text, "tests/test_c.py": CONTROL}
    )
    head = repo.commit({"pkg/ops.py": OPS_VALUE})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    # The text names ``value`` only: ``other`` does not reach it.
    plan2 = _plan(repo, base, repo.commit({"pkg/ops.py": OPS_OTHER}), T_A, T_C)
    assert selected(plan2) == set()


def test_r12_text_from_elsewhere_is_unbounded(repo):
    """``exec(gen.source())``: the project builds the text, which may import
    anything; nothing bounds the test, so any change reaches it."""
    base = repo.commit(
        {
            **BASE,
            "pkg/ops.py": OPS,
            "pkg/gen.py": (
                f"SRC = '{IMPORT_OPS}\\nres = ops.value()'\n\n\ndef source():\n    return SRC\n"
            ),
            "tests/test_a.py": (
                "from pkg import gen\n\n\ndef test_a():\n    ns = {}\n    exec(gen.source(), ns)\n"
            ),
            "tests/test_c.py": CONTROL,
        }
    )
    head = repo.commit({"pkg/ops.py": OPS_VALUE})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dynamic_reference"}


# --------------------------------------------------------------------------- R13

PLUGIN_PROJECT = {
    **BASE,
    "pyproject.toml": (
        "[project]\nname = 'pytest-myplug'\nversion = '0'\n\n"
        "[project.entry-points.pytest11]\nmyplug = 'pytest_myplug'\n"
    ),
    "pytest_myplug.py": "import pytest\n\n\n@pytest.fixture\ndef plugval():\n    return 1\n",
    "tests/conftest.py": "pytest_plugins = ['pytester']\n",
    "tests/test_c.py": CONTROL,
}
PLUGIN_CHANGED = "import pytest\n\n\n@pytest.fixture\ndef plugval():\n    return 2\n"
ROOT = "import pathlib\n\nROOT = str(pathlib.Path(__file__).resolve().parent.parent)\n"
INLINE = "def test_x(plugval):\\n    assert plugval == 1\\n"
PYTESTER_TESTS = {
    "subprocess-dash-p": (
        f"{ROOT}\n\ndef test_a(pytester):\n    pytester.makepyfile('{INLINE}')\n"
        "    pytester.syspathinsert(ROOT)\n"
        "    r = pytester.runpytest_subprocess('-p', 'pytest_myplug')\n"
        "    r.assert_outcomes(passed=1)\n"
    ),
    "entry-point-only": (
        f"{ROOT}\n\ndef test_a(pytester):\n    pytester.makepyfile('{INLINE}')\n"
        "    r = pytester.runpytest()\n    r.assert_outcomes(passed=1)\n"
    ),
    "makeconftest-pytest_plugins": (
        f"{ROOT}\n\ndef test_a(pytester):\n"
        "    pytester.makeconftest('pytest_plugins = [\"pytest_myplug\"]')\n"
        f"    pytester.makepyfile('{INLINE}')\n"
        "    r = pytester.runpytest_inprocess()\n    r.assert_outcomes(passed=1)\n"
    ),
    "testdir": (
        f"{ROOT}\n\ndef test_a(testdir):\n    testdir.makepyfile('{INLINE}')\n"
        "    r = testdir.runpytest()\n    r.assert_outcomes(passed=1)\n"
    ),
    "through-a-fixture": (
        f"{ROOT}\nimport pytest\n\n\n@pytest.fixture\ndef session(pytester):\n"
        f"    pytester.makepyfile('{INLINE}')\n    return pytester\n\n\n"
        "def test_a(session):\n    session.runpytest().assert_outcomes(passed=1)\n"
    ),
}


@pytest.mark.parametrize("text", list(PYTESTER_TESTS.values()), ids=list(PYTESTER_TESTS))
def test_r13_a_pytester_test_depends_on_the_projects_plugins(repo, text):
    """The inline session loads the project's ``pytest11`` plugin (or is
    told to), and the inline test requests its fixture by a name only the
    text holds: a body change of the fixture reaches the test."""
    base = repo.commit({**PLUGIN_PROJECT, "tests/test_a.py": text})
    head = repo.commit({"pytest_myplug.py": PLUGIN_CHANGED})
    plan = repo.plan(base, head, [NOOP], source_roots=["."], discover_runners=["pytest"])
    assert selected(plan) == {"tests/test_a.py::test_a"}


def test_r13_what_the_inline_text_imports_counts(repo):
    """``makepyfile("from pkg import ops ...")``: the text is read as code,
    so a body change of what it calls reaches the test, and ``other`` does
    not."""
    inline = f"{IMPORT_OPS}\\ndef test_x():\\n    assert ops.value() == 1\\n"
    base = repo.commit(
        {
            **BASE,
            "pkg/ops.py": OPS,
            "tests/conftest.py": "pytest_plugins = ['pytester']\n",
            "tests/test_c.py": CONTROL,
            "tests/test_a.py": (
                f"{ROOT}\n\ndef test_a(pytester):\n    pytester.makepyfile('{inline}')\n"
                "    pytester.syspathinsert(ROOT)\n"
                "    pytester.runpytest_inprocess().assert_outcomes(passed=1)\n"
            ),
        }
    )
    kwargs = {"source_roots": ["."], "discover_runners": ["pytest"]}
    plan = repo.plan(base, repo.commit({"pkg/ops.py": OPS_VALUE}), [NOOP], **kwargs)
    assert selected(plan) == {"tests/test_a.py::test_a"}
    plan2 = repo.plan(base, repo.commit({"pkg/ops.py": OPS_OTHER}), [NOOP], **kwargs)
    assert selected(plan2) == set()


# --------------------------------------------------------------------------- R14

INNER_TEST = "from pkg import ops\n\n\ndef test_v(expected):\n    assert ops.value() == expected\n"
INNER_CASES = (
    "import unittest\nfrom pkg import ops\n\n\nclass T(unittest.TestCase):\n"
    "    def test_v(self):\n        self.assertEqual(ops.value(), 1)\n"
)
INNER_CONFTEST = "import pytest\n\n\n@pytest.fixture\ndef expected():\n    return 1\n"
INNER = {
    "pkg/ops.py": OPS,
    "tests/inner/__init__.py": "",
    "tests/inner/conftest.py": INNER_CONFTEST,
    "tests/inner/check_inner.py": INNER_TEST,
    "tests/inner/cases.py": INNER_CASES,
    "tests/test_c.py": CONTROL,
}
RUN = "import subprocess\nimport sys\n\n\ndef test_a():\n    subprocess.run("
INNER_PATH = "tests/inner/check_inner.py"
RUNNER_TESTS = {
    "subprocess-pytest-path": (
        f"{RUN}[sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '{INNER_PATH}'])\n"
    ),
    "subprocess-pytest-nodeid": (
        f"{RUN}[sys.executable, '-m', 'pytest', '-x', '{INNER_PATH}::test_v'])\n"
    ),
    "pytest-main-literal": (
        "import pytest\n\n\ndef test_a():\n"
        f"    assert pytest.main(['-p', 'no:cacheprovider', '{INNER_PATH}']) == 0\n"
    ),
    "pytest-main-joined-path": (
        "import pathlib\nimport pytest\n\n\ndef test_a():\n"
        "    here = pathlib.Path(__file__).parent\n"
        "    assert pytest.main(['--tb', 'short', str(here / 'inner' / 'check_inner.py')]) == 0\n"
    ),
    "pytest-main-local-list": (
        f"import pytest\n\n\ndef test_a():\n    args = ['-q', '{INNER_PATH}']\n"
        "    assert pytest.main(args) == 0\n"
    ),
    "coverage-run-pytest": (
        f"{RUN}[sys.executable, '-m', 'coverage', 'run', '--source', 'pkg', "
        f"'-m', 'pytest', '{INNER_PATH}'])\n"
    ),
    "shell-line": f"{RUN}f'{{sys.executable}} -m pytest -q {INNER_PATH}', shell=True)\n",
}
UNITTEST_TESTS = {
    "subprocess-unittest-module": (
        f"{RUN}[sys.executable, '-m', 'unittest', '-v', 'tests.inner.cases'])\n"
    ),
    "unittest-main": (
        "import unittest\n\n\ndef test_a():\n"
        "    unittest.main(module='tests.inner.cases', exit=False)\n"
    ),
    "loader-by-name": (
        "import unittest\n\n\ndef test_a():\n"
        "    suite = unittest.defaultTestLoader.loadTestsFromName('tests.inner.cases')\n"
        "    assert unittest.TextTestRunner().run(suite).wasSuccessful()\n"
    ),
    "loader-instance-by-names": (
        "import unittest\n\n\ndef test_a():\n    loader = unittest.TestLoader()\n"
        "    suite = loader.loadTestsFromNames(['tests.inner.cases.T'])\n"
        "    assert unittest.TextTestRunner().run(suite).wasSuccessful()\n"
    ),
}


@pytest.mark.parametrize("text", list(RUNNER_TESTS.values()), ids=list(RUNNER_TESTS))
def test_r14_a_test_file_given_to_pytest_runs_whole(repo, text):
    """The file's test calls ``ops.value`` and uses a conftest fixture: a
    change to the called function, to the test's own body or to the fixture
    reaches the test that runs the file; ``other`` reaches nothing."""
    base = repo.commit({**BASE, **INNER, "tests/test_a.py": text})
    for change in (
        {"pkg/ops.py": OPS_VALUE},
        {"tests/inner/check_inner.py": INNER_TEST.replace("== expected", "== expected + 0")},
        {"tests/inner/conftest.py": INNER_CONFTEST.replace("return 1", "return 2")},
    ):
        plan = _plan(repo, base, repo.commit({**INNER, **change}), T_A, T_C)
        assert selected(plan) == {T_A.runner_id}, change
        assert rules(plan, T_A.runner_id) == {"dependency"}, change
    plan = _plan(repo, base, repo.commit({**INNER, "pkg/ops.py": OPS_OTHER}), T_A, T_C)
    assert selected(plan) == set()


@pytest.mark.parametrize("text", list(UNITTEST_TESTS.values()), ids=list(UNITTEST_TESTS))
def test_r14_a_module_given_to_unittest_runs_whole(repo, text):
    base = repo.commit({**BASE, **INNER, "tests/test_a.py": text})
    for change in (
        {"pkg/ops.py": OPS_VALUE},
        {"tests/inner/cases.py": INNER_CASES.replace("value(), 1", "value(), 1 + 0")},
    ):
        plan = _plan(repo, base, repo.commit({**INNER, **change}), T_A, T_C)
        assert selected(plan) == {T_A.runner_id}, change
        assert rules(plan, T_A.runner_id) == {"dependency"}, change
    plan = _plan(repo, base, repo.commit({**INNER, "pkg/ops.py": OPS_OTHER}), T_A, T_C)
    assert selected(plan) == set()


@pytest.mark.parametrize(
    "call",
    [
        "subprocess.run([sys.executable, '-m', 'pytest', 'tests/inner'])",
        "subprocess.run([sys.executable, '-m', 'pytest', 'tests'])",
        "subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])",
        "pytest.main()",
        "pytest.main(sys.argv[1:])",
        "unittest.main(module=None)",
        "unittest.defaultTestLoader.discover('tests')",
    ],
    ids=[
        "dir",
        "top-dir",
        "discover",
        "main-argv",
        "main-list-from-elsewhere",
        "unittest-argv",
        "loader-discover",
    ],
)
def test_r14_a_directory_or_a_run_from_elsewhere_is_unbounded(repo, call):
    base = repo.commit(
        {
            **BASE,
            **INNER,
            "pkg/other.py": "def g():\n    return 1\n",
            "tests/test_a.py": (
                "import pytest\nimport subprocess\nimport sys\nimport unittest\n\n\n"
                f"def test_a():\n    {call}\n"
            ),
        }
    )
    head = repo.commit({"pkg/other.py": "def g():\n    return 2\n"})
    plan = _plan(repo, base, head, T_A, T_C)
    assert selected(plan) == {T_A.runner_id}
    assert rules(plan, T_A.runner_id) == {"dynamic_reference"}


# --------------------------------------------------------------------------- R17

PROPERTY_X = (
    "class C:\n    @property\n    def x(self):\n        return {x}\n\n"
    "    def other(self):\n        return 0\n"
)
MATCH_ARGS = (
    "class C:\n    __match_args__ = ('x',)\n\n    @property\n    def x(self):\n"
    "        return {x}\n\n    def other(self):\n        return 0\n"
)
INHERITED_MATCH_ARGS = "class Base:\n    __match_args__ = ('x',)\n\n\n" + MATCH_ARGS.replace(
    "class C:\n    __match_args__ = ('x',)\n\n", "class C(Base):\n"
)
MATCH = (
    "from pkg.mod import C\n\n\ndef run():\n    match C():\n        case {pattern}:\n"
    "            return 1\n        case _:\n            return 2\n"
)
MATCH_THROUGH_MODULE = MATCH.replace("from pkg.mod import C", "from pkg import mod").replace(
    "C()", "mod.C()"
)


@pytest.mark.parametrize(
    ("mod", "api"),
    [
        (PROPERTY_X, MATCH.format(pattern="C(x=1)")),
        (MATCH_ARGS, MATCH.format(pattern="C(1)")),
        (MATCH_ARGS, MATCH_THROUGH_MODULE.format(pattern="mod.C(1)")),
        (INHERITED_MATCH_ARGS, MATCH.format(pattern="C(1)")),
    ],
    ids=["keyword", "positional", "positional-through-module", "match_args-inherited"],
)
def test_r17_a_match_class_pattern_reads_the_attributes_it_names(repo, mod, api):
    """``case C(x=1)`` reads ``C.x``; ``case C(1)`` reads what
    ``__match_args__`` lists. A change to the property reaches the test;
    a change to another method of ``C`` does not."""
    other = mod.format(x=1).replace("return 0", "return 5")
    plan = _api_scenario(repo, mod.format(x=1), api, mod.format(x=2), other)
    assert rules(plan, T_X.runner_id) == {"dependency"}


def test_r17_positional_patterns_without_match_args_read_any_attribute(repo):
    """A dataclass (or any class whose ``__match_args__`` the index does not
    see written out) with positional patterns: every member of the class
    may be read."""
    mod = (
        "from dataclasses import dataclass\n\n\n@dataclass\nclass C:\n    x: int = 1\n\n"
        "    @property\n    def y(self):\n        return {y}\n"
    )
    _api_scenario(repo, mod.format(y=1), MATCH.format(pattern="C(1)"), mod.format(y=2), None)


# --------------------------------------------------------------------------- R18

PROP = (
    "class C:\n    @property\n    def v(self):\n        return {v}\n\n"
    "    def m(self):\n        return {v}\n\n    def other(self):\n        return 0\n"
)
FROM_C = "from pkg.mod import C\n\n\n"
STRING_APIS = {
    "attrgetter": (
        f"import operator\n{FROM_C}def run():\n    return operator.attrgetter('v')(C())\n"
    ),
    "attrgetter-imported-name": (
        f"from operator import attrgetter\n{FROM_C}def run():\n    return attrgetter('v')(C())\n"
    ),
    "methodcaller": (
        f"import operator\n{FROM_C}def run():\n    return operator.methodcaller('m')(C())\n"
    ),
    "format-positional-field": f"{FROM_C}def run():\n    return int('{{0.v}}'.format(C()))\n",
    "format-keyword-field": f"{FROM_C}def run():\n    return int('{{c.v}}'.format(c=C()))\n",
    "format-template-constant": (
        "from pkg.mod import C\nfrom pkg.tmpl import FMT\n\n\ndef run():\n"
        "    return int(FMT.format(C()))\n"
    ),
    "formatter-class": (
        f"import string\n{FROM_C}def run():\n"
        "    return int(string.Formatter().format('{0.v}', C()))\n"
    ),
    "attrgetter-dotted": (
        f"import operator\n{FROM_C}def run():\n    return operator.attrgetter('inner.v')(C())\n"
    ),
    "attrgetter-parameter": (
        f"import operator\n{FROM_C}def pick(name):\n    return operator.attrgetter(name)(C())\n"
        "\n\ndef run():\n    return pick('v')\n"
    ),
    "methodcaller-parameter": (
        f"import operator\n{FROM_C}def call(name):\n"
        "    return operator.methodcaller(name)(C())\n\n\ndef run():\n    return call('m')\n"
    ),
    "format-template-parameter": (
        f"{FROM_C}def render(template):\n    return int(template.format(C()))\n\n\n"
        "def run():\n    return render('{0.v}')\n"
    ),
}


@pytest.mark.parametrize("api", list(STRING_APIS.values()), ids=list(STRING_APIS))
def test_r18_names_in_strings_the_stdlib_consumes_are_read_by_name(repo, api):
    """``attrgetter('v')``, ``methodcaller('m')``, ``'{0.v}'.format(...)``:
    the name in the string is read off the object; a body change of the
    property or method reaches the test. (``C()`` handed to the library's
    callable escapes too, R19, so every member of ``C`` reaches the test;
    the precision of that rule is test_r19_builtins_reading_dunders_only_hand_nothing_on's.)"""
    _api_scenario(repo, PROP.format(v=1), api, PROP.format(v=2), None)


def test_r18_a_name_from_elsewhere_is_a_dynamic_reference(repo):
    """``methodcaller(name)`` with ``name`` a test's parameter (pytest
    supplies it): any method may be called, so the test is bounded only by
    its import closure, which the change lies in."""
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": PROP.format(v=1),
            "tests/test_x.py": (
                "import operator\n\nimport pytest\n\nfrom pkg.mod import C\n\n\n"
                "@pytest.mark.parametrize('name', ['m'])\ndef test_x(name):\n"
                "    assert operator.methodcaller(name)(C()) == 1\n"
            ),
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/mod.py": PROP.format(v=2)}), T_X, T_C)
    assert selected(plan) == {T_X.runner_id}


# --------------------------------------------------------------------------- R19

WRITER = (
    "class W:\n    def __init__(self):\n        self.n = 0\n\n"
    "    def write(self, s):\n        self.n += {n}\n\n    def flush(self):\n        pass\n\n"
    "    def other(self):\n        return 0\n"
)
FROM_W = "from pkg.mod import W\n\n\n"
DUCK_APIS = {
    "json.dump": (
        f"import json\n{FROM_W}def run():\n    w = W()\n    json.dump(1, w)\n    return w.n\n"
    ),
    "print-file": f"{FROM_W}def run():\n    w = W()\n    print('x', file=w)\n    return w.n\n",
    "csv.writer": (
        f"import csv\n{FROM_W}def run():\n    w = W()\n    csv.writer(w).writerow([1])\n"
        "    return w.n\n"
    ),
    "logging.StreamHandler": (
        f"import logging\n{FROM_W}def run():\n    w = W()\n    h = logging.StreamHandler(w)\n"
        "    return w.n\n"
    ),
    "contextlib.closing": (
        f"import contextlib\n{FROM_W}def run():\n    w = W()\n    with contextlib.closing(w):\n"
        "        pass\n    return w.n\n"
    ),
    "stored-on-a-library-object": (
        f"import logging\n{FROM_W}def run():\n    h = logging.StreamHandler()\n"
        "    h.stream = W()\n    return 1\n"
    ),
    "appended-to-an-unknown-list": (
        f"{FROM_W}def run():\n    sinks = make()\n    sinks.append(W())\n    return 1\n\n\n"
        "def make():\n    return []\n"
    ),
    "factory-result": (
        f"import json\n{FROM_W}def make():\n    return W()\n\n\ndef run():\n"
        "    json.dump(1, make())\n    return 1\n"
    ),
    "class-without-constructor": (
        f"import json\n{FROM_W}class Box(dict):\n    pass\n\n\ndef run():\n    Box(W())\n"
        "    return 1\n"
    ),
}


@pytest.mark.parametrize("api", list(DUCK_APIS.values()), ids=list(DUCK_APIS))
def test_r19_an_instance_handed_to_code_outside_the_roots_escapes(repo, api):
    """A library (or a receiver of unknown type) calls plain-named methods
    on what it is handed: a body change of ``W.write`` reaches the test
    through the class."""
    plan = _api_scenario(repo, WRITER.format(n=1), api, WRITER.format(n=2), None)
    assert rules(plan, T_X.runner_id) == {"dependency"}


KEYS = (
    "class C:\n    def keys(self):\n        return ['a'{more}]\n\n"
    "    def __getitem__(self, k):\n        return 1\n"
)


@pytest.mark.parametrize(
    "body",
    ["len(dict(C()))", "len({**C()})", "len(dict(**C()))", "update(C())"],
    ids=["dict", "double-star-display", "double-star-call", "dict.update"],
)
def test_r19_the_keys_protocol_is_a_plain_name(repo, body):
    api = (
        f"{FROM_C}def update(x):\n    d = {{}}\n    d.update(x)\n    return len(d)\n\n\n"
        f"def run():\n    return {body}\n"
    )
    _api_scenario(repo, KEYS.format(more=""), api, KEYS.format(more=", 'b'"), None)


@pytest.mark.parametrize(
    "body",
    ["len(W())", "str(W())", "isinstance(W(), W)", "cast(W, None)", "repr(W())", "sorted(W())"],
    ids=["len", "str", "isinstance", "cast", "repr", "sorted"],
)
def test_r19_builtins_reading_dunders_only_hand_nothing_on(repo, body):
    """``len(w)`` calls ``__len__``, nothing by a plain name: ``W.write``
    changing reaches no test that only does that (the rule's precision)."""
    api = f"from typing import cast\n\n{FROM_W}def run():\n    {body}\n    return 1\n"
    base = repo.commit(
        {
            **BASE,
            "pkg/mod.py": WRITER.format(n=1),
            "pkg/api.py": api,
            "tests/test_x.py": TEST_X,
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/mod.py": WRITER.format(n=2)}), T_X, T_C)
    assert selected(plan) == set()


# --------------------------------------------------------------------------- R25

READ = "def read(m, n):\n    return getattr(m, n)()\n"
SUB = "def helper():\n    return 0\n\n\ndef other():\n    return {v}\n"
IMPORT_MODULE = "from importlib import import_module\nfrom pkg.rd import read\n\n\n"
HANDED = {
    "result-handed-on": (
        f"{IMPORT_MODULE}def run(name):\n    return read(import_module('pkg.sub'), name)\n"
    ),
    "local-variable": (
        f"{IMPORT_MODULE}def run(name):\n    m = import_module('pkg.sub')\n"
        "    return read(m, name)\n"
    ),
    "stored-in-a-dict": (
        f"{IMPORT_MODULE}MODS = {{'sub': import_module('pkg.sub')}}\n\n\n"
        "def run(name):\n    return read(MODS['sub'], name)\n"
    ),
    "returned-by-a-helper": (
        f"{IMPORT_MODULE}def _mod():\n    return import_module('pkg.sub')\n\n\n"
        "def run(name):\n    return read(_mod(), name)\n"
    ),
    "dunder-import-top": (
        "from pkg.rd import read\n\n\ndef run(name):\n    top = __import__('pkg.sub')\n"
        "    return read(top.sub, name)\n"
    ),
    "attribute-of-import_module": (
        "from importlib import import_module\nimport pkg.sub\nfrom pkg.rd import read\n\n\n"
        "def run(name):\n    return read(import_module('pkg').sub, name)\n"
    ),
    "importlib-module": (
        "import importlib\nfrom pkg.rd import read\n\n\ndef run(name):\n"
        "    return read(importlib.import_module('pkg.sub'), name)\n"
    ),
}
T_RUN = py_target("tests/test_run.py::test_run", "tests.test_run.test_run")
TEST_RUN = "from pkg.use import run\n\n\ndef test_run():\n    assert run('other') == 1\n"


@pytest.mark.parametrize("use", list(HANDED.values()), ids=list(HANDED))
def test_r25_a_module_obtained_by_a_literal_name_and_handed_on_escapes(repo, use):
    """``read(import_module("pkg.sub"), name)`` reads any attribute of the
    module: a body change of ``other`` reaches the test, as it does when the
    module is handed on by its import name."""
    base = repo.commit(
        {
            **BASE,
            "pkg/use.py": use,
            "pkg/rd.py": READ,
            "pkg/sub.py": SUB.format(v=1),
            "tests/test_run.py": TEST_RUN,
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/sub.py": SUB.format(v=2)}), T_RUN, T_C)
    assert selected(plan) == {T_RUN.runner_id}


def test_r25_a_module_obtained_by_a_literal_name_and_only_read_does_not(repo):
    """``import_module("pkg.sub").helper()`` reads one name: ``other``
    changing reaches nothing (the precision the rule keeps)."""
    base = repo.commit(
        {
            **BASE,
            "pkg/use.py": (
                "from importlib import import_module\n\n\ndef run(name):\n"
                "    return import_module('pkg.sub').helper()\n"
            ),
            "pkg/sub.py": SUB.format(v=1),
            "tests/test_run.py": TEST_RUN.replace("== 1", "== 0"),
            "tests/test_c.py": CONTROL,
        }
    )
    plan = _plan(repo, base, repo.commit({"pkg/sub.py": SUB.format(v=2)}), T_RUN, T_C)
    assert selected(plan) == set()
    helper_changed = SUB.format(v=1).replace("return 0", "return 3")
    plan2 = _plan(repo, base, repo.commit({"pkg/sub.py": helper_changed}), T_RUN, T_C)
    assert selected(plan2) == {T_RUN.runner_id}

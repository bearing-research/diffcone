# Limitations

diffcone is designed to select too many tests rather than too few. These
are the cases where it selects more than you might expect, or can't see
everything.

## What a change reaches

**Changes that run on import reach every importer.**
:   A change to code that runs when a module is imported (a module-level
    statement, a constant, a class body, a decorator) selects every target
    whose module imports that module, directly or indirectly. In a codebase
    where most modules import a few central ones, such a change selects
    most tests.

    With a [recording](guides/evidence.md), a changed constant or class
    attribute selects only the tests that read it. Other code that runs on
    import (a module-level statement, a decorator) is still planned this
    way.

**Functions under unknown decorators count as import-time code.**
:   `@deco` over a function is a call `deco(f)` when the module is
    imported, and the decorator may call the function then. So a change to
    the function's body counts as a change to code that runs on import,
    unless diffcone knows the decorator calls nothing of it:

    - a decorator such as `property`, `staticmethod`, `functools.cache`,
      `pytest.fixture` or `dataclasses.dataclass`;
    - a decorator of your own that only sets attributes and returns the
      function, or a `functools.wraps` wrapper.

    A decorator from an installed package (a compiled `cache_readonly`,
    say) is one diffcone can't read.

**Class-level changes affect every method.**
:   A change to a class's body (its bases, decorators or class attributes)
    affects all of its methods.

**Removed imports affect the whole module.**
:   Removing or redirecting an import affects the whole importing module.
    Adding one affects only the code that uses the new name, unless the
    imported module runs code of its own that the importing module did not
    run before.

**A change to a file diffcone doesn't read selects everything.**
:   Under your source roots, diffcone reads only `.py` files. A change to
    any other file there (a data file, a `.pyx`, a `README.md`, a
    configuration file) selects every target, since code or tests may read
    it. So does a change to a file's mode (a script made executable, a
    file turned into a symlink). `diffcone.toml` and `.diffcone/` are the
    exceptions.

    With the source root `.` (the default), the whole repository is under
    a root, so editing `README.md` selects every test. With a
    [recording](guides/evidence.md), most such changes select only the
    tests that read the file.

## Calls and lookups

**Calls through objects of unknown type are matched by name.**
:   diffcone doesn't infer types. `self.method()` and `cls.method()` are
    resolved through the class hierarchy. A call like `obj.run()` on an
    object of unknown type is matched against every method named `run`,
    and against module-level `run` in modules such an object could be. The
    calling code is selected when any of those changes.

    A module "such an object could be" is one your code passes around as a
    value or binds to another name (`m = ops`), gets by name
    (`importlib.import_module`, `sys.modules`, a string naming it), or one
    pytest hands out (test modules, conftests). A name bound only to
    modules (`m = ops`, then `m.value()`) is followed like the import
    itself.

    An instance attribute is followed only when every assignment of it is
    in `__init__` and assigns a parameter, a string, or one of your
    functions or classes by name. An attribute that holds an object
    (`self.client = Client()`) is not typed for calls: `self.client.get()`
    is matched against every method named `get`.

**Dynamic lookups are bounded by imports, if at all.**
:   A function that uses `eval`, `exec`, `globals()`, `vars()` or
    `getattr` with a computed name is treated as affected by any change in
    the modules its module imports, and in the modules of code that stores
    objects on one of those modules. Some code changes that bound:

    - **Lookups on a library module.** `getattr(logging, name)` is bounded
      unless your code may store something on that module: directly, by
      handing the module to other code, or by putting an object in
      `sys.modules` under its name. Then any change can affect the lookup.
      A patch undone when the test ends doesn't count (see *Patches in
      tests* below).
    - **Lookup tables.** A name taken from a dict, list or set written in
      the code (`getattr(handlers, NAMES[key])`, the export table of a
      lazy `__getattr__`) is bounded by that table only while nothing can
      change it. Once the table, or the module holding it, is modified,
      passed to other code, or reached through `globals()`, `vars()`,
      `sys.modules` or `exec`, the lookup counts as dynamic. A parameter or
      local variable that happens to share the table's name isn't bounded
      by it.
    - **Names in strings.** `operator.attrgetter("a.b")`,
      `methodcaller("m")`, a format template's fields (`"{0.name}"`) and a
      `match` class pattern (`case C(x=1)`, or `case C(1)` through
      `__match_args__`) read attributes by name. They are matched like
      `getattr(x, "a")`. A name or template that isn't written out is
      bounded by what callers pass, or else counts as dynamic.
    - **Names that start with fixed text.** `f"plugins.{name}"` is bounded
      to the modules it can name.

**Code loaded at run time can be affected by any change.**
:   These can reach any of your code, so the code using them is selected
    for any change:

    - a module imported or run by a name nothing bounds
      (`importlib.import_module(name)`, `runpy.run_module(name)`,
      `runpy.run_path(path)`);
    - a module loaded from a file whose path is computed
      (`importlib.util.spec_from_file_location`);
    - an object unpickled from data the code doesn't write out. A
      `pickle.loads(pickle.dumps(x))` round trip, and bytes written in the
      code, are followed.

**Modules obtained at run time count as any of your modules.**
:   A module your code gets by a name it computes counts as any of your
    modules, so passing it on or writing to it makes every lookup table
    dynamic. The same goes for a function's `__globals__`, a frame's
    `f_globals`, and what `pickle.loads`, `pkgutil.resolve_name` or
    `gc.get_objects()` hand back.

    Two exceptions: a name that starts with fixed text, and a new module
    (`types.ModuleType(name)`) that isn't installed in `sys.modules` under
    a name of yours. A copy made with `importlib.util.module_from_spec`
    counts as the module whose file it loads.

**Replacing a module in `sys.modules` reaches every importer.**
:   Code that puts an object in `sys.modules` for good under a computed
    name may replace any of your modules. Without a recording, every test
    that imports one of them depends on that code.

## Objects and modules handed to other code

**Passing a module on depends on all of it.**
:   Code that passes a module to other code (`read(ops, name)`) depends on
    everything in that module and on what its imports bind
    (`api.core.TABLE`). That holds whether the module is named by an
    import or obtained with `importlib.import_module("pkg.sub")`.

**Passing an object on depends on all of its methods.**
:   Code that hands an instance of one of your classes to other code
    depends on every method of that class, since the receiving code may
    call any of them by name. That covers a library (`json.dump(x, w)`,
    `csv.writer(w)`, `print(file=w)`, `dict(obj)`, `handler.stream = w`),
    a method on an object of unknown type (`items.append(w)`), and your
    own functions. Builtins that only use dunder methods (`len`, `str`,
    `iter`, `sorted`) don't count.

## Code a test runs from text or in another process

**Text run as code.**
:   `exec`, `eval`, `timeit` and pytest's `pytester.makepyfile` run text.
    Text written in the code (a string, a constant, a template filled with
    literals) is read as code: what it imports and calls counts. Text
    built at run time or read from a file can import anything, so the test
    is selected for any change. Text passed in as a helper's argument is
    read at each call site. A template filled by the test's own argument
    is read once per value when `@pytest.mark.parametrize` on the test
    function lists literal values for it (and the test has no decorator
    other than pytest marks); values from a fixture, `indirect=True`, or a
    parametrize on the class or in `pytestmark` count as built at run time.

**Tests that use `pytester`.**
:   A test using `pytester` or `testdir` runs a pytest session of its own,
    with your project's registered plugins loaded. It depends on every
    fixture those plugins define, and on what its inline test code
    imports.

**Python programs a test starts.**
:   A test that runs a script or module of your project in a new process
    depends on that program and on what it imports, when the code names it
    in a string: `[sys.executable, "scripts/gen.py"]`,
    `python -m mypkg.tool`, `python -c "import mypkg"`. This also works
    after `env X=1`, `uv run` or `timeout 30`, and with
    `asyncio.create_subprocess_exec`.

    - A test file given to a test runner (`python -m pytest
      tests/test_x.py`, `pytest.main([...])`, `python -m unittest
      tests.cases`) runs whole: the test depends on everything in that
      file and its `conftest.py` files.
    - A `.py` file no module name maps to (`scripts/gen-data.py`) is read
      like `-c` code: the test depends on the file and on what it imports.
    - When the program is built at run time, the test is selected for any
      change: a path from a variable or the environment, `-c` code
      assembled from pieces, a program that imports by a computed name or
      changes `sys.path`, `*args`, a directory, or `unittest discover`.
    - Console scripts your project installs and files outside your source
      roots aren't seen.

    [Execution evidence](guides/evidence.md) follows the processes a test
    starts.

## State shared between tests

**State changed in place reaches every reader.**
:   Code that changes a module-level object, directly or by handing it to a
    function that changes it (`register(REGISTRY)`, `registry.add(x)`), is
    a dependency of every reader of that object. A change to a function
    also reaches the readers of whatever the functions it calls change in
    place.

    Not followed:

    - an object stored away and changed later under another name
      (`self.store = store`, then `self.store[k] = v`);
    - an object a function returns that its caller then changes;
    - a library function that changes an argument you pass it, a module of
      yours included (a library writing into a module you hand it).

**Patches in tests.**
:   A store on a module, yours or a library's, makes the code storing a
    dependency of every reader. A patch undone when the test ends doesn't
    count:

    - a method of pytest's `monkeypatch` or pytest-mock's `mocker`, called
      in the test or in a function-scoped fixture that requests it, unless
      that module defines its own fixture of the same name;
    - a `with mock.patch(...)` or `with MonkeyPatch.context()` block in a
      function that doesn't `yield`;
    - a `@mock.patch(...)` decorator;
    - a value saved and put back in a test or a function-scoped fixture:
      `orig = m.X`, then `m.X = v`, restored in a `finally:` block or, in a
      fixture, right after the `yield`.

    Any other patch counts as a store that may outlast the test:
    `patch(...).start()`, a patcher kept for later, a `MonkeyPatch()` of
    your own, `monkeypatch` handed to a helper function, a
    `with mock.patch(...)` around a `yield` in a fixture, or a
    `monkeypatch` fixture the same module overrides.

    An override of `monkeypatch` or `mocker` in a parent `conftest.py` is
    not seen: diffcone still assumes what a test patches through it is
    undone when the test ends.

**Tests are assumed not to depend on what an earlier test left behind.**
:   When code that runs inside one test changes module state
    (`server._state = make()`), diffcone counts that change for that test
    only, not for other tests that read the same state. Such code is:

    - the test itself, and unittest's `setUp` and `tearDown`;
    - a function-scoped fixture;
    - a helper in a test module or `conftest.py` that only such code
      calls.

    A test or `setUp` that other code calls (a test module's top-level
    code, a session fixture) is not such code, and neither is a doctest's
    function. Code that runs once for many tests is a dependency of every
    test that reads what it changes: a test module's top-level code, a
    fixture with a wider scope (or a scope it computes), a hook, and any
    code of your project, a cache it fills included.

    If a test relies on state an earlier test set up, declare that
    dependency in [`diffcone.toml`](reference/declarations.md) (an
    `[[edges]]` entry from the reader to the code setting it up), or run
    the whole suite.

**Caches with a recording.**
:   With a [recording](guides/evidence.md), a test that reads a cache an
    earlier test filled counts as running the code that filled it. That
    works when your code fills the cache with a value it computes and keeps
    it in a module variable, on an object or class, in a closure or a
    function attribute, or behind a memoising decorator.

    A decorator counts as memoising when diffcone knows it (`functools`,
    `cachetools`, Django's and Werkzeug's `cached_property`, and others) or
    its name says so (`cached_property`, `cached`, `memoize`, `memoized`,
    `cachedmethod`).

    Not followed:

    - a cache kept by an installed package's decorator under another name;
    - a cache filled through a method of an object whose type diffcone
      can't tell (`self._cache.set(k, compute(k))` where `_cache` is set in
      several ways).

**Code that changes the process while pytest collects reaches every test.**
:   A `conftest.py`, a test module or a module they import may set an
    environment variable, extend `sys.path` or configure a library
    (`warnings.filterwarnings`, `logging.basicConfig`) when it's imported.
    A collection hook may do the same. That can change what any later test
    sees, so such code is a dependency of every test.
    [Static discovery](reference/discovery.md#pytest) lists what is
    recognised.

## Reading files

**A file that doesn't parse selects everything.**
:   Any `.py` file under your source roots that isn't valid Python 3, or
    can't be decoded in its declared encoding (UTF-8 unless the file says
    otherwise), is an analysis error, even a test data file nothing
    imports. So is one that nests an expression hundreds of levels deep,
    as generated code may (a sum of a thousand terms). Choose source roots
    that leave such files out.

**Uncommitted analysis is only as stable as your working tree.**
:   A plan of `WORKTREE` or `INDEX` describes the files at the moment you
    planned. The report always marks such plans as uncommitted.

**Git-ignored files are seen only by `WORKTREE` plans, and only Python ones.**
:   A plan of a commit or of `INDEX` reads what git recorded. A `.py` file
    git ignores (a generated `_version.py`, a `local_settings.py`) and any
    edit to it are invisible to it. `run` refuses to run such a plan while
    one is on disk under your source roots: plan with `--head WORKTREE`
    instead.

    A `WORKTREE` plan reads ignored `.py` files in a directory that also
    holds files git tracks or doesn't ignore. A directory with only ignored
    files is skipped, as are ignored directories (a virtualenv, a build
    tree) and ignored files that aren't Python.

## Discovery

**Discovery reads the source, so it can't see what plugins do.**
:   A pytest plugin that collects tests by its own rules, or a test base
    class outside your source roots, can mean diffcone's target list is
    incomplete. diffcone reports it and the plan exits with code `3`
    ([static discovery](reference/discovery.md#when-the-target-list-may-be-short)).
    A plugin that collects files just by being installed is invisible to a
    plan from the code. Whenever `run` starts pytest, it runs every
    collected test the plan doesn't know, and a recording captures what
    pytest really collected.

**Parametrized tests are selected as a whole.**
:   diffcone selects or skips all of a test's parameter cases together.

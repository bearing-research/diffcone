# Limitations

diffcone is designed to select too many tests rather than too few. These
are the cases where it selects more than you might expect, or can't see
everything.

**Changes that run on import reach every importer.**
:   A change to code that runs when a module is imported (a module-level
    statement, a constant, a class body, a decorator) selects every target
    whose module imports that module, directly or indirectly. In a codebase
    where most modules import a few central ones, such a change selects
    most tests. [Execution evidence](guides/evidence.md) narrows this to the
    tests that actually used the changed code.

**A function under a decorator diffcone can't read runs on import.**
:   `@deco` over a function is a call `deco(f)` when the module is imported,
    and the decorator may call the function then. Unless the decorator is
    one diffcone knows calls nothing of it (`property`, `staticmethod`,
    `functools.cache`, `pytest.fixture`, `dataclasses.dataclass` and the
    like, or a decorator of your own that only sets attributes and returns
    the function or a `functools.wraps` wrapper), a change to the
    function's body counts as a change to code that runs on import. A
    decorator from an installed package (a compiled `cache_readonly`, say)
    is one diffcone can't read.

**Class-level changes affect every method.**
:   A change to a class's body (its bases, decorators or class attributes)
    affects all of its methods. Removing or redirecting an import affects
    the whole importing module; adding one affects only the code that uses
    the new name, unless the imported module runs code of its own that
    the importing module did not run before.

**Calls through objects of unknown type are matched by name.**
:   diffcone doesn't infer types. `self.method()` and `cls.method()` are
    resolved through the class hierarchy, but a call like `obj.run()` on an
    object of unknown type is matched against every method named `run`,
    and against every module-level function, class or variable named `run`
    in a module such an object can be: one your code passes around as a
    value, gets by name (`importlib.import_module`, `sys.modules`, a string
    naming it), or one pytest hands out (test modules, conftests). So it's
    selected whenever any of those changes.

    An instance attribute is followed only when every assignment of it is
    in `__init__` and assigns a parameter, a string, or one of your
    functions or classes by name. An attribute that holds an object
    (`self.client = Client()`) is not typed for calls: `self.client.get()`
    is matched against every method named `get`.

**Dynamic code is bounded by imports, if at all.**
:   A function that uses `eval`, `exec`, `globals()`, `vars()` or `getattr`
    with a computed name is treated as affected by any change in the
    modules its module imports, and in the modules of code that stores
    objects on one of those modules. Some code widens that bound:

    - **Lookups on a library module.** `getattr(logging, name)` is bounded
      unless your code may store something on that module: directly, by
      handing the module to other code, or by putting an object in
      `sys.modules` under its name. Then any change can affect the lookup.
    - **Patches in tests.** A patch undone when the test ends doesn't count
      as storing: a method of pytest's `monkeypatch` fixture or
      pytest-mock's `mocker` in the test or fixture that requests it, a
      `with mock.patch(...)` or `with MonkeyPatch.context()` block, a
      `@mock.patch(...)` decorator. A patch that may outlast the test
      does: `patch(...).start()`, a patcher kept for later, a
      `MonkeyPatch()` of your own, `monkeypatch` handed to a helper
      function, or a `with mock.patch(...)` around a `yield` in a fixture.
    - **Code loaded at run time.** A module imported or run by a name
      nothing bounds (`importlib.import_module(name)`,
      `runpy.run_module(name)`, `runpy.run_path(path)`), an object unpickled with
      `pickle.loads`, and a module loaded from a file whose path is
      computed (`importlib.util.spec_from_file_location`) can be affected
      by any change at all. A name that starts with fixed text
      (`f"plugins.{name}"`) is bounded to the modules it can name.
    - **Lookup tables.** A name taken from a dict, list or set written in
      the code (`getattr(handlers, NAMES[key])`, the export table of a
      lazy `__getattr__`) is bounded by that table only while nothing can
      change it. Once the table, or the module holding it, is modified,
      passed to other code, or reached through `globals()`, `vars()`,
      `sys.modules` or `exec`, the lookup counts as dynamic. A parameter or
      local variable that happens to share the table's name isn't bounded
      by it.
    - **Modules obtained at run time.** A module your code gets by a name
      it computes counts as any of your modules, so passing it on or
      writing to it makes every lookup table dynamic. The same goes for a
      function's `__globals__`, a frame's `f_globals`, and what
      `pickle.loads`, `pkgutil.resolve_name` or `gc.get_objects()` hand
      back. Two exceptions: a name that starts with fixed text, and a new
      module (`types.ModuleType(name)`) that isn't installed in
      `sys.modules` under a name of yours. A copy made with
      `importlib.util.module_from_spec` counts as the module whose file it
      loads.
    - **Passing a module on.** Code that passes a module to other code
      (`read(ops, name)`) depends on everything in that module and on what
      its imports bind (`api.core.TABLE`), as it depends on every member of
      a class whose instances it passes on.
    - **Replacing modules.** Code that puts an object in `sys.modules` for
      good under a computed name may replace any of your modules, so
      without a recording every test that imports one of them depends on
      that code.

**Python programs a test starts.**
:   A test that runs a script or module of your project in a new process
    (`[sys.executable, "scripts/gen.py"]`, `python -m mypkg.tool`,
    `python -c "import mypkg"`) depends on that script or module, and on
    what it imports, when the code names it in a string. When the program
    is built at run time (a path from a variable or the environment, `-c`
    code assembled from pieces or code that changes `sys.path`, `*args`),
    the test is affected by any change. So is a test that runs a `.py` file no module name maps to
    (`scripts/gen-data.py`), which isn't analysed. Console scripts your
    project installs and files outside your source roots aren't seen;
    [execution evidence](guides/evidence.md) follows the processes a test
    starts.

**State changed in place reaches every reader.**
:   Code that changes a module-level object, directly or by handing it to a
    function that changes it (`register(REGISTRY)`, `registry.add(x)`), is
    a dependency of every reader of that object. A change to a function
    also reaches the readers of whatever the functions it calls change in
    place. Not followed: an object stored away and changed later under
    another name (`self.store = store`, then `self.store[k] = v`), an
    object a function returns that its caller then changes, and a library
    function that changes an argument you pass it, a module of yours
    included (a library writing into a module you hand it).

**Tests are assumed not to depend on what an earlier test left behind.**
:   A test, a function-scoped fixture, or a helper in a test module or
    `conftest.py` that only tests call may change module state (`server._state
    = make()`): diffcone counts that change for the test running it only, not
    for the other tests and benchmarks that read the same state. If a test
    relies on state an earlier test set up, declare that dependency in
    [`diffcone.toml`](reference/declarations.md) (an `[[edges]]` entry from
    the reader to the code setting it up) or run the whole suite. Code that
    runs once for many tests is still a dependency of every reader: a test
    module's top-level code, a fixture with a wider scope (or a scope it
    computes), a hook, and any code of your project, a cache a test fills
    included. With a [recording](guides/evidence.md), such a cache is
    followed when your code keeps it in a module variable, on an object, a
    class, a closure or a function attribute, or in a
    `functools.cached_property`, and fills it with a value your code
    computes. Not followed: a cache kept by a memoising decorator from a
    package diffcone doesn't know (it knows `functools`, `cachetools`,
    `cached_property`, Django's and Werkzeug's), and one filled through a
    method of an object whose type diffcone can't tell
    (`self._cache.set(k, compute(k))` where `_cache` is set in several
    ways).

**Code that changes the process while pytest collects reaches every test.**
:   A `conftest.py`, a test module or a module they import that sets an
    environment variable, extends `sys.path` or configures a library
    (`warnings.filterwarnings`, `logging.basicConfig`) when it's imported,
    or a collection hook that does, can change what any later test sees.
    Such code is a dependency of every test
    ([static discovery](reference/discovery.md#pytest) lists what is
    recognised). diffcone assumes a test doesn't depend on state an
    earlier test left behind.

**A file that doesn't parse selects everything.**
:   Any file under your source roots that isn't valid Python 3, or can't be
    decoded in its declared encoding (UTF-8 unless the file says
    otherwise), is an analysis error, even a test data file nothing
    imports. So is one that nests an expression hundreds of levels deep,
    as generated code may (a sum of a thousand terms). Choose source roots
    that leave such files out.

**Discovery reads the source, so it can't see what plugins do.**
:   A pytest plugin that collects tests by its own rules, or a test base
    class outside your source roots, can mean diffcone's target list is
    incomplete. diffcone reports it and the plan exits with code `3`
    ([static discovery](reference/discovery.md#when-the-target-list-may-be-short)).
    A plugin that collects files just by being installed is invisible to a
    plan from the code. Whenever `run` starts pytest, it runs every
    collected test the plan doesn't know, and a recording captures what
    pytest really collected. Parametrized tests are selected or skipped as
    a whole.

**Uncommitted analysis is only as stable as your working tree.**
:   A plan of `WORKTREE` or `INDEX` describes the files at the moment you
    planned. The report always marks such plans as uncommitted.

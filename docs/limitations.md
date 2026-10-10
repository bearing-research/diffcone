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

**Class-level changes affect every method.**
:   A change to a class's body (its bases, decorators or class attributes)
    affects all of its methods. Removing or redirecting an import affects
    the whole importing module; adding one affects only the code that uses
    the new name, unless the imported module runs code of its own that
    the importing module did not run before.

**Calls through objects of unknown type are matched by name.**
:   diffcone doesn't infer types. `self.method()` and `cls.method()` are
    resolved through the class hierarchy, but a call like `obj.run()` on an
    object of unknown type is matched against every function and method
    named `run`, so it's selected whenever any of them changes. Instance
    attributes are resolved only when every assignment is a simple one in
    `__init__`.

**Plugins that collect files of their own.**
:   diffcone reports the collecting plugins it recognises from your
    configuration. A plugin that collects files just by being installed is
    invisible to a plan from the code. Whenever `run` starts pytest, it
    runs every collected test the plan doesn't know, and a recording
    captures what pytest really collected.

**Dynamic code is bounded by imports, if at all.**
:   A function that uses `eval`, `exec`, `globals()`, `vars()` or `getattr`
    with a computed name is treated as affected by any change in the
    modules its module imports. A module imported by a computed name
    (`importlib.import_module(name)`) can be affected by any change at all.
    A name taken from a dict, list or set written in the code
    (`getattr(handlers, NAMES[key])`, the export table of a lazy
    `__getattr__`; not a parameter or local variable that happens to share
    the table's name) is bounded by it only while nothing can change it: once
    the table, or the module holding it, is modified, passed to other code,
    or reached through `globals()`, `vars()`, `sys.modules` or `exec`, the
    lookup counts as dynamic. A module your code gets by a name it computes
    counts as any module, so passing it on or writing to it makes every such
    table dynamic, unless the name starts with fixed text
    (`f"plugins.{name}"` is one of the `plugins.` modules) or the module is
    a new one (`types.ModuleType(name)`) that isn't installed in
    `sys.modules` under a name of yours. The same goes for a function's
    `__globals__`, a frame's `f_globals`, what `pickle.loads`,
    `pkgutil.resolve_name` or `gc.get_objects()` hand back, and a copy made
    with `importlib.util.module_from_spec` (which is the module whose file it
    loads). Passing a module on also passes on whatever its imports bind
    (`api.core.TABLE`). Code that passes a module to other
    code (`read(ops, name)`) depends on everything in that module and on
    what its imports bind, as it does on every member of a class whose
    instances it passes on.
    A lookup on a standard-library or third-party
    module (`getattr(logging, name)`) is bounded unless your code may store
    something on that module, directly, by handing the module to other
    code, or by putting an object in `sys.modules` under its name; then any
    change can affect it.

**Python programs a test starts.**
:   A test that runs a script or module of your project in a new process
    (`[sys.executable, "scripts/gen.py"]`, `python -m mypkg.tool`,
    `python -c "import mypkg"`) depends on that script or module, and on
    what it imports, when the code names it in a string. When the program
    is built at run time (a path from a variable or the environment, `-c`
    code assembled from pieces, `*args`), the test is affected by any
    change. So is a test that runs a `.py` file no module name maps to
    (`scripts/gen-data.py`), which isn't analysed. Console scripts your
    project installs and files outside your source roots aren't seen;
    [execution evidence](guides/evidence.md) follows the processes a test
    starts.

**State changed in place reaches every reader.**
:   Code that changes a module-level object, directly or by handing it to a
    function that changes it (`register(REGISTRY)`, `registry.add(x)`), is
    a dependency of every reader of that object. A change to a function
    also reaches the readers of whatever the functions it calls change in
    place. An object stored away and changed later under another name
    (`self.store = store`, then `self.store[k] = v`) isn't followed.

**A file that doesn't parse selects everything.**
:   Any file under your source roots that isn't valid Python 3 or isn't
    UTF-8 is an analysis error, even a test data file nothing imports.
    Choose source roots that leave such files out.

**Discovery reads the source, so it can't see what plugins do.**
:   A pytest plugin that collects tests by its own rules, or a test base
    class outside your source roots, can mean diffcone's target list is
    incomplete. diffcone reports it and the plan exits with code `3`
    ([static discovery](reference/discovery.md#when-the-target-list-may-be-short)).
    Parametrized tests are selected or skipped as a whole.

**Uncommitted analysis is only as stable as your working tree.**
:   A plan of `WORKTREE` or `INDEX` describes the files at the moment you
    planned. The report always marks such plans as uncommitted.

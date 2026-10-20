# Limitations

diffcone is designed to select too many tests rather than too few. These
are the cases where it selects more than you might expect, or can't see
everything.

## What a change reaches

**Changes that run on import reach every importer.**
:   A change to code that runs when a module is imported (a module-level
    statement, a class body, a decorator) selects every target
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
:   Under your source roots, diffcone reads only `.py` files, and not
    those `[index] exclude` lists. A change to any other file there (a data file, a `.pyx`, a `README.md`, a
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
    `pydoc.locate` given a computed name, or `gc.get_objects()`, hand
    back.

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

    The same holds for state kept on a class (`Config.mode = m`,
    `Config.items.append(x)`, `setattr(Config, "mode", m)`, `cls._instance
    = cls()`, `type(self)._cache[k] = v`): the code storing it is a
    dependency of every reader of the class. When the class can't be known
    (`type(obj).mode = m` or `obj.__class__.items.append(x)`, where `obj`
    is a parameter or comes from a call), the code is a dependency of the
    readers of each of your classes that has that attribute (its body or
    one of its methods sets it), of each class deriving from a library
    class, and of their subclasses, or of
    every class for a special attribute (`__doc__`), for an attribute none
    of your classes has, and for a name only known at run time
    (`setattr(type(obj), name, v)`). A mock's class is its own, so
    `type(mock).name = PropertyMock(...)` changes none of your classes. And
    it holds when the code reaches the object by another name:

    - a local name for it: `cache = _CACHE`, then `cache[k] = v` (also
      `(cache := _CACHE)`, `for cache in (_A, _B)`, `with nullcontext(_CACHE)
      as cache`);
    - a part of it: `entry = _CACHE[k]` or `entry = _CACHE.setdefault(k, [])`,
      then `entry.append(v)`;
    - a bound method: `put = _CACHE.__setitem__`, then `put(k, v)`;
    - what one of your functions returns: `registry()[k] = v`, where
      `registry` returns `_CACHE`;
    - `globals()["_CACHE"][k] = v`.

    A class-level container is shared by every instance: a change made
    through an instance of your class is a dependency of every reader of
    the class too (`Store._cache`, `cls._cache`), as long as the class or
    one of your base classes binds something mutable in its body, some
    code stores onto the class, or it has a decorator, a metaclass or a
    library base that may give it state (other than `dataclasses.dataclass`,
    `functools.total_ordering`, `typing.final` and `abc.ABCMeta`). That holds for an instance a module-level
    name holds (`STORE = Store()`, `STORE = make_store()`, or set by a
    function under `global`: `STORE._cache[k] = v`, `STORE.cache()[k] =
    v`), and for one a function makes (`s = Store()`, `new =
    cls.__new__(cls)`, `make_store()`: `s._cache[k] = v`,
    `new._items.append(x)`, or `s._items` handed to a function that
    changes it). Otherwise a class whose body holds only constants,
    methods and properties is not affected, nor is an attribute of a new
    instance that no class body binds (`s.mine.append(x)`, where only
    `__init__` sets `self.mine`).

    A copy is not the object: `cache = dict(_CACHE)` or `_CACHE.copy()`,
    then `cache[k] = v`, changes nothing shared. Setting an attribute on a
    new instance (`Config().mode = m`, `new = cls.__new__(cls); new.mode =
    m`) changes that instance only, unless a class in its hierarchy (a
    base or a subclass) defines its own `__new__` or derives from a class
    of an installed package, or the class is decorated or has a metaclass
    (for `cls.__new__(cls)`, one other than `functools.total_ordering`,
    `typing.final` or `abc.ABCMeta`): such an instance may be one kept elsewhere (a
    singleton), and setting its attribute counts as a change of the
    class's state. A method changing an attribute its instance owns
    (`self.items.append(x)`, where `__init__` sets `self.items = []`) on an
    instance no module variable or class holds is not followed as shared
    state.

    A module-level constant nothing can change in place has no writers: a
    name your module binds once, to a string, a number, a tuple of such
    values, `re.compile(...)`, `textwrap.dedent(...)` or a path, and that no
    code binds again (`global`, `module.NAME = ...`, `setattr`). Passing
    `SQL` to a function, or using what `PATTERN.match(s)` returns, does not
    make its readers depend on that code. The same holds for a class
    attribute your class body binds once to such a value (`DAYS = "days"`,
    `_MONTHS = 12`) and that no code binds again: handing `TimeDelta.DAYS`
    to a function, or `m = min(m, self._MONTHS)`, writes nothing of the
    class, and neither does calling a method on an item of a class-level
    table of strings and numbers that no code changes
    (`self.timeframes[k].format(n)`). A class with a decorator (other than
    `functools.total_ordering` or `typing.final`), a metaclass (other than
    `abc.ABCMeta`) or a library base keeps every writer of its attributes.

    An object you hand to a constructor of your class (`Tokenizer(source,
    rules=RULES)`) is part of the new instance. Changing the instance in
    place (`tok.rules["x"] = 1`, calling one of its methods, `__init__`
    changing what it stored) changes the object, so it is a dependency of
    every reader of `RULES`. Setting an attribute on the instance
    (`tok.position = 0`; not a special attribute such as `__dict__`), or
    adding a number or string to one that only ever holds such values
    (`tok.position += 1`), in that code or in a function it hands the
    instance to, is not, as long as no class involved defines `__new__`,
    `__setattr__`, `__delattr__`, `__getattr__` or `__getattribute__`,
    binds that name in its body to anything but a constant (a property,
    another descriptor, a list), or has a decorator (other than
    `functools.total_ordering` or `typing.final`), a metaclass (other than
    `abc.ABCMeta`) or a library base.

    Not followed:

    - an object stored away and changed later under another name
      (`self.store = store`, then `self.store[k] = v`), unless the instance
      holding it was made by calling your class with it, in the function
      that changes it or hands it on or in a function of yours that returns
      it (an instance kept in a module variable,
      `TOK = Tokenizer(RULES)`, is not followed back to `RULES`);
    - an object a library function returns that your code then changes;
    - a method called on an item of a module-level dict or list
      (`_CACHE["a"].put(4)`): only the dict's own methods count as writes;
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
    earlier test filled counts as running the code that filled it. Every
    write counts that the code able to run a change makes, itself or
    through a function it calls (`_put(key, compute(key))`), into a module
    variable, a class, an object (`self._cache[k] = v`,
    `self.__dict__[k] = v`, `setattr(self, k, v)`), a closure or a
    function attribute, whatever it stores: a computed value, a constant,
    a registration, or a value it stores only on some branch, since what
    it stores may depend on the change in ways the code doesn't show. So
    does a result kept by a memoising decorator. The one exception is what
    `__init__` (or a dataclass's `__post_init__`) sets on the object it is
    building: no other test can hold that object yet. Calling
    `obj.__init__()`, `Base.__init__(obj)` or `super().__init__()` again on
    an object in use counts as writing into it.

    A decorator counts as memoising when diffcone knows it (`functools`,
    `cachetools`, Django's and Werkzeug's `cached_property`, and others) or
    its name says so (`cached_property`, `cached`, `memoize`, `memoized`,
    `cachedmethod`).

    Not followed:

    - a cache kept by an installed package's decorator under another name;
    - a cache filled through code diffcone can't follow from the code that
      ran the change: a method that stores somewhere other than its own
      object (`_STORE.put(k, v)` writing a module-level `_D`,
      `Engine().run(k)`), a method of an attribute set in several ways
      (`self._cache.set(k, compute(k))`), a bound method kept in a variable
      (`put = _STORE.put`), an object called like a function (`__call__`),
      or a list or table of functions (`for cb in _HOOKS: cb(v)`,
      `_SINKS["a"](k, v)`);
    - a write in a class's `__exit__` at the end of a `with` block, or in a
      generator created elsewhere that the code running the change
      resumes;
    - a value a test computes and hands to code of your project that keeps
      it (`store(compute(2))` in the test itself): it counts for that test
      only, as described above.

    If later tests depend on such a cache, declare the dependency in
    [`diffcone.toml`](reference/declarations.md) (an `[[edges]]` entry
    from the code reading the cache to the code filling it), name those
    tests in an [`[[always_run]]`](reference/declarations.md#tests-to-run-on-every-change)
    entry, or plan that change without the recording.

**What a recording can't see.**
:   The recorder sees what Python code of your project and its tests runs,
    opens and lists. A plan made from a recording misses what happens out
    of its sight:

    - a file opened by compiled code rather than by Python (a certificate
      or key file that `ssl` hands to OpenSSL, say): a change to it doesn't
      reach the tests that load it;
    - an environment variable a test's own code sets in `os.environ` and a
      later test reads; set it with `monkeypatch.setenv` instead, which
      undoes it when the test ends;
    - a cache on disk (a pickle, an SQLite file, a cache directory) one
      test writes and a later one reads: the later test reads the file,
      not the code that wrote it; give each test its own directory
      (`tmp_path`) or clear the cache between tests;
    - a cache an earlier test filled through `functools.lru_cache` or
      `functools.cache`: the recorder clears these before each test, so
      each test records the code that fills them, but a suite that relies
      on one test seeing another's cached result runs differently while
      recording;
    - a program your tests start that is named like a read-only system
      query (`git`, `uname`, `ps`, `locale`) but is a script of yours on
      `PATH`: it is taken for the system program and not followed;
    - tests outside pytest's rootdir: with the rootdir in a subdirectory
      (a `pytest.ini` in `sub/`) and tests both under it and elsewhere
      (`pytest sub/tests tests`), records can be matched to the wrong
      tests; add `--rootdir=.` to the command you record with, or keep
      every test under the rootdir.

    For tests that depend on any of these, use an
    [`[[always_run]]`](reference/declarations.md#tests-to-run-on-every-change)
    entry, or plan without the recording, which follows the code instead
    (a change to a file diffcone doesn't read selects every test).

**Code that changes the process while pytest collects reaches every test.**
:   A `conftest.py`, a test module or a module they import may set an
    environment variable, extend `sys.path` or configure a library
    (`warnings.filterwarnings`, `logging.basicConfig`) when it's imported.
    A collection hook may do the same. That can change what any later test
    sees, so such code is a dependency of every test.
    [Static discovery](reference/discovery.md#pytest) lists what is
    recognised.

**With a recording, code run while pytest collects a file reaches that file's tests.**
:   Building parameter ids, an `ids=` function and `pytest_generate_tests`
    decide the tests of the file pytest is collecting, so a change there
    selects that file's tests and whoever reads what the code stored. Code
    that pytest hands its objects to can also leave something for hooks
    that act on every test. That is recognised when the code, or what ran
    it, reads `.config`, `.session`, `.stash` or `.pluginmanager`, or is a
    hook other than `pytest_generate_tests`, and then every test is
    selected. Something left on another pytest object (`metafunc.function`,
    a node's attributes) for a later hook to act on is not.

## Reading files

**A file that doesn't parse selects everything.**
:   Any `.py` file under your source roots that isn't valid Python 3, or
    can't be decoded in its declared encoding (UTF-8 unless the file says
    otherwise), is an analysis error, even a test data file nothing
    imports. So is one that nests an expression hundreds of levels deep,
    as generated code may (a sum of a thousand terms). Choose source roots
    that leave such files out, or list them under `[index] exclude` in
    [diffcone.toml](reference/declarations.md#files-to-leave-out-of-the-analysis).

**An excluded file is a dependency diffcone can't see into.**
:   diffcone doesn't read a `.py` file `[index] exclude` keeps out, so it
    doesn't know what the file imports or calls. Code that imports or runs
    one (an import, `import_module`, `runpy`, a subprocess, loading it by
    path) is selected on any change under your source roots, and a change
    to the excluded file itself selects every test (with a recording, see
    [diffcone.toml](reference/declarations.md#files-to-leave-out-of-the-analysis)).
    Tests and benchmarks
    in an excluded file, and the fixtures and hooks of an excluded
    `conftest.py` or plugin, are not discovered.

**Uncommitted analysis is only as stable as your working tree.**
:   A plan of `WORKTREE` or `INDEX` describes the files at the moment you
    planned. The report always marks such plans as uncommitted.

**Git-ignored files are seen only by `WORKTREE` plans, and only Python ones.**
:   A plan of a commit or of `INDEX` reads what git recorded. A `.py` file
    git ignores (a generated `_version.py`, a `local_settings.py`) and any
    edit to it are invisible to it. `run` refuses to run such a plan while
    one is on disk under your source roots, unless it only assigns
    constants and neither commit has it (a generated `_version.py`);
    otherwise plan with `--head WORKTREE`.

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

    A fixture decorator is recognised under another name only when the
    module using it binds that name (`from pytest import fixture as fx`,
    `fx = pytest.fixture`). One imported from another of your modules is
    not: an autouse fixture under it is not a dependency of the tests it
    applies to, so a change to it may select none of them. A decorator from
    an installed package that writes docstrings isn't read either: the
    doctests it adds are not targets.

**Parametrized tests are selected as a whole.**
:   diffcone selects or skips all of a test's parameter cases together.

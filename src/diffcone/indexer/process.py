"""Writes of process-global state outside the source roots.

Code that writes the environment (``os.environ["MODE"] = ...``), the import
path, the working directory, warning filters or another library's global
configuration leaves something every later piece of code in the process can
read, through the standard library or a third-party package the analysis
cannot see. No edge leads from such a write to whoever reads it, so the
writing symbols are recorded (``SourceIndex.process_writes``) and discovery
makes the code pytest runs before every test that can reach one a
dependency of every test (pytest_static).

What is recognised, by canonical dotted name through the scope's imports:

* a store, ``del`` or augmented assignment into ``STATE_OBJECTS`` or below
  them (``os.environ[k] = v``, ``sys.path[:0] = [...]``,
  ``pandas.options.display.width = 9``), or a call of a mutating method on
  one (``os.environ.update``, ``sys.path.insert``);
* a store onto an external module's attribute (``sys.stdout = ...``,
  ``tempfile.tempdir = ...``), and ``setattr``/``delattr`` on one;
* a call of ``STATE_CALLS`` (``os.putenv``, ``warnings.filterwarnings``,
  ``locale.setlocale``, ``numpy.random.seed``, ...), and of a configuring
  method on a logger ``logging.getLogger(...)`` returns.

Not recognised: another library's configuration function missing from the
tables, and state a library keeps on objects it hands out. Writes to module
state inside the source roots are not recorded here: those are variables
with ``mutated_by`` edges.
"""

from __future__ import annotations

# Process-wide objects: writing into them (or below them) changes what any
# later code sees.
STATE_OBJECTS = frozenset(
    {
        "os.environ",
        "os.environb",
        "sys.path",
        "sys.meta_path",
        "sys.path_hooks",
        "sys.path_importer_cache",
        "sys.modules",
        "sys.argv",
        "sys.warnoptions",
        "warnings.filters",
        "logging.root",
        "matplotlib.rcParams",
        "matplotlib.pyplot.rcParams",
        "pandas.options",
        "numpy.random",
    }
)

# Methods that change the object they are called on.
STATE_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "popitem",
        "clear",
        "sort",
        "reverse",
        "update",
        "setdefault",
        "__setitem__",
        "__delitem__",
    }
)

# Functions that change process-wide state.
STATE_CALLS = frozenset(
    {
        "os.putenv",
        "os.unsetenv",
        "os.chdir",
        "os.fchdir",
        "os.umask",
        "os.register_at_fork",
        "sys.setrecursionlimit",
        "sys.settrace",
        "sys.setprofile",
        "sys.setswitchinterval",
        "sys.set_int_max_str_digits",
        "sys.setdlopenflags",
        "sys.addaudithook",
        "sys.set_asyncgen_hooks",
        "threading.settrace",
        "threading.setprofile",
        "threading.stack_size",
        "site.addsitedir",
        "importlib.invalidate_caches",
        "importlib.reload",
        "warnings.filterwarnings",
        "warnings.simplefilter",
        "warnings.resetwarnings",
        "logging.basicConfig",
        "logging.disable",
        "logging.captureWarnings",
        "logging.setLoggerClass",
        "logging.setLogRecordFactory",
        "logging.addLevelName",
        "logging.config.dictConfig",
        "logging.config.fileConfig",
        "locale.setlocale",
        "time.tzset",
        "socket.setdefaulttimeout",
        "signal.signal",
        "signal.alarm",
        "signal.setitimer",
        "signal.siginterrupt",
        "faulthandler.enable",
        "faulthandler.disable",
        "faulthandler.register",
        "faulthandler.dump_traceback_later",
        "gc.disable",
        "gc.enable",
        "gc.freeze",
        "gc.unfreeze",
        "gc.set_threshold",
        "gc.set_debug",
        "random.seed",
        "random.setstate",
        "decimal.setcontext",
        "mimetypes.init",
        "mimetypes.add_type",
        "codecs.register",
        "copyreg.pickle",
        "resource.setrlimit",
        "shutil.register_archive_format",
        "shutil.register_unpack_format",
        "asyncio.set_event_loop",
        "asyncio.set_event_loop_policy",
        "numpy.random.seed",
        "numpy.random.set_state",
        "numpy.seterr",
        "numpy.seterrcall",
        "numpy.set_printoptions",
        "numpy.setbufsize",
        "pandas.set_option",
        "pandas.reset_option",
        "matplotlib.use",
        "matplotlib.rc",
        "matplotlib.rcdefaults",
        "matplotlib.style.use",
        "matplotlib.pyplot.switch_backend",
        "matplotlib.pyplot.style.use",
        "matplotlib.pyplot.rcdefaults",
        "torch.manual_seed",
        "torch.set_default_dtype",
        "torch.set_num_threads",
        "hypothesis.settings.load_profile",
        "hypothesis.settings.register_profile",
        "django.setup",
        "django.conf.settings.configure",
    }
)

# ``logging.getLogger(name).<method>(...)`` and ``.<attribute> = ...``.
LOGGER_GETTERS = frozenset({"logging.getLogger"})
LOGGER_MUTATORS = frozenset(
    {
        "setLevel",
        "addHandler",
        "removeHandler",
        "addFilter",
        "removeFilter",
        "disabled",
        "propagate",
        "handlers",
        "level",
    }
)


def state_object(name: str | None) -> str | None:
    """The process-wide object ``name`` is, or lies below."""
    if name is None:
        return None
    for obj in STATE_OBJECTS:
        if name == obj or name.startswith(obj + "."):
            return obj
    return None

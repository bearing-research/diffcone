"""Source index and dependency resolver.

Parses every Python module of a snapshot, assigns stable symbol identities,
hashes bodies and definitions, and resolves the statically resolvable subset
of references into explicit dependency edges. Everything that cannot be
resolved is recorded as an :class:`UnresolvedReference`, never dropped.

Supported subset (see internal/design.md):

* module-level functions, classes, methods (and nested classes) as symbols;
* bare names and dotted attribute chains rooted at a module-level definition,
  an import alias, ``self``/``cls`` inside a method, or a star import;
* ``import``/``from ... import`` (absolute and relative) within source roots;
* a name every binding of which in its scope is a module (``m = ops``,
  ``m = pkg.ops``, a walrus, a literal unpacking or loop, ``global``/
  ``nonlocal`` rebindings: ``aliases.py``) reads off the module; a name
  that may hold something else as well reads off it and stays bounded by
  the attribute name;
* ``importlib.import_module`` / ``getattr`` with literal arguments (a
  literal table's keys, values or elements included: ``D[k]``, ``D.values()``,
  ``for k, v in D.items()``) while every use of the table is a read (see
  ``uses.py``), with parameters whose call sites pass literals,
  and with instance attributes that ``__init__`` binds to such values
  (``getattr(x, self.name)``); with a name starting with literal text
  (``import_module(f"plugins.{name}")``, ``getattr(obj, f"do_{name}")``),
  every in-scope module or attribute name with that prefix; the other
  calls that import by name, the same way (``pytest.importorskip``,
  ``importlib.util.find_spec``, ``runpy.run_module``,
  ``pkgutil.resolve_name``, ``pydoc.locate``, ``pkgutil.iter_modules``/
  ``walk_packages`` over a package's ``__path__``);
* an import or a run of a ``.py`` file ``diffcone.toml``'s ``[index]
  exclude`` keeps out of the analysis: an unknown dependency (a dynamic
  reference that imports anything, naming the file);
* attribute lookup on classes through their in-scope MRO (``self.m`` for an
  inherited ``m``, ``Sub.m``, ``super().m``); an attribute the class does
  not define (``C.__doc__``, ``C.__type_params__``) is a read of the class
  object, and ``C.__mro__``/``__bases__``/``__subclasses__()`` reach the
  classes they hand out (``Resolver.class_object_read``);
* instance attributes ``self.x = ...`` binds in any method to an object of
  a known kind (a construction of an in-scope class, a call into a module
  outside the source roots, an in-scope factory), and in ``__init__`` to a
  parameter, a literal or a symbol as well; any other write leaves the
  attribute unbounded;
* in-place writes of a module variable or of state held on a class,
  directly, through ``cls``/``type(self)``, through a local name, an item,
  ``globals()`` or a function's result bound to it (``references.py``), or
  through a parameter or receiver the callee writes (``writes.py``); a variable filled once by the
  one function writing it, with a value nothing can see differently
  (``global V; if V is None: V = os.path.dirname(pkg.__file__)``), is a
  lazy constant (``lazy.py``); a variable bound once to a value nothing can
  change in place (a string, a number, a tuple of such, ``re.compile(...)``)
  that nothing binds anew has no writers (``literals.module_immutables``,
  ``Indexer._immutable_variables``), and so has a class attribute bound once
  to such a value or a class-level table of them nothing changes
  (``literals.class_immutables``, ``class_tables``);
* in-place writes through an instance of an in-scope class (a module
  variable holding one, or one a function makes) reaching the state its
  class holds (``literals.class_state``, ``Indexer._instance_writes``); what
  a constructor is handed is part of the instance, so a change in place of
  the instance writes it (``writes._HeldSafety``); a write onto the class
  of an object of unknown type (``type(o).x = v``) writes every class that
  may own the attribute (``writes.AnyClassOwners``);
* project code run by naming it in a string: a ``.py`` path, ``-m NAME``,
  ``-c CODE``, and a Python command whose program cannot be named
  (``scripts.py``); a module loaded from a file by path
  (``spec_from_file_location``, ``SourceFileLoader``) and what an unpickler
  loads (a round trip, or bytes written in the code);
* text run as code in the process (``exec``/``eval``, ``timeit``'s
  statements, ``code.InteractiveInterpreter().runsource``, ``pytester``'s
  texts) when the text is written out (a literal, a constant, a template
  literals fill): read as the program it is (``scripts.code_edges``); text
  built at run time or read from a file is unbounded;
* a test file given to a test runner (``-m pytest tests/x.py``, a node id,
  ``unittest.main(module=...)``, ``pytester.copy_example``): the file and,
  for pytest, its conftests, each whole (``scripts.py``);
* names read off an object by the standard library from a string written
  out: ``operator.attrgetter``/``methodcaller``, the attribute fields of a
  ``str.format`` template, and the attributes a ``match`` class pattern
  reads (keywords, ``__match_args__``);
* an instance handed to code the analysis does not follow into (a
  library's function, a builtin reading more than dunders, a receiver of
  unknown type): its class escapes, and referring to it depends on every
  member (``references._handed_instances``); likewise a module handed on,
  a literal ``import_module("m")`` result included;
* the ``__init__``/``__new__`` a class's creation runs (an ``enum`` base,
  an in-scope metaclass), and the ``__init_subclass__`` and metaclass hooks
  every class statement runs;
* the order of a module's or class's top-level statements (a reorder is a
  definition change; two adjacent plain ``def``s trading places is none);
* writes of process-global state outside the source roots (``process.py``),
  and the stores a test makes that are undone when it ends (``patches.py``).

Deliberately unsupported: type inference, dynamic dispatch on unknown
receivers, decorators that rewrite call targets.
"""

from diffcone.indexer.core import Indexer, build_index
from diffcone.indexer.scopes import resolve_relative_module
from diffcone.indexer.syntax import (
    DEF_NODES,
    FUNC_NODES,
    TOO_DEEP,
    decode_source,
    hash_nodes,
    hash_scope_body,
    iter_scope_statements,
)

__all__ = [
    "DEF_NODES",
    "FUNC_NODES",
    "TOO_DEEP",
    "Indexer",
    "build_index",
    "decode_source",
    "hash_nodes",
    "hash_scope_body",
    "iter_scope_statements",
    "resolve_relative_module",
]

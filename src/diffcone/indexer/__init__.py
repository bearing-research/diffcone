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
* ``importlib.import_module`` / ``getattr`` with literal arguments (a
  literal table's keys, values or elements included: ``D[k]``, ``D.values()``,
  ``for k, v in D.items()``) while every use of the table is a read (see
  ``uses.py``), with parameters whose call sites pass literals,
  and with instance attributes that ``__init__`` binds to such values
  (``getattr(x, self.name)``); with a name starting with literal text
  (``import_module(f"plugins.{name}")``, ``getattr(obj, f"do_{name}")``),
  every in-scope module or attribute name with that prefix;
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
* in-place writes of a module variable, directly or through a parameter or
  receiver the callee writes (``writes.py``);
* project code run by naming it in a string: a ``.py`` path, ``-m NAME``,
  ``-c CODE``, and a Python command whose program cannot be named
  (``scripts.py``); a module loaded from a file by path
  (``spec_from_file_location``, ``SourceFileLoader``) and what an unpickler
  loads (a round trip, or bytes written in the code);
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

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
  (``getattr(x, self.name)``);
* attribute lookup on classes through their in-scope MRO (``self.m`` for an
  inherited ``m``, ``Sub.m``, ``super().m``).

Deliberately unsupported: type inference, dynamic dispatch on unknown
receivers, instance attributes written outside ``__init__`` or reflectively,
decorators that rewrite call targets.
"""

from diffcone.indexer.core import Indexer, build_index
from diffcone.indexer.scopes import resolve_relative_module
from diffcone.indexer.syntax import (
    DEF_NODES,
    FUNC_NODES,
    decode_source,
    hash_nodes,
    hash_scope_body,
    iter_scope_statements,
)

__all__ = [
    "DEF_NODES",
    "FUNC_NODES",
    "Indexer",
    "build_index",
    "decode_source",
    "hash_nodes",
    "hash_scope_body",
    "iter_scope_statements",
    "resolve_relative_module",
]

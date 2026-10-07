"""What the indexer's passes share: the snapshot, the index being built,
the scopes, and where pass-2 writes go."""

from __future__ import annotations

from collections import defaultdict

from diffcone.cython import read as read_cython
from diffcone.indexer.facts import _Output
from diffcone.indexer.scopes import ClassScope, ModuleScope
from diffcone.model import AnalysisError, SourceIndex, Symbol
from diffcone.snapshot import Snapshot


class IndexerState:
    """Shared state of the indexer's passes."""

    # Submodules per package (pass 2), set by ``Indexer.build``.
    _children: dict[str, frozenset[str]]

    def __init__(self, snapshot: Snapshot, module_cache=None) -> None:
        self.snapshot = snapshot
        self.index = SourceIndex(
            snapshot=snapshot.info,
            other_files=dict(snapshot.other_files),
            cython={
                path: read_cython(path, content.decode("utf-8", "surrogateescape"))
                for path, content in sorted(snapshot.cython_files.items())
            },
        )
        self.index.errors.extend(snapshot.errors)
        # Optional per-module cache of first-pass facts and second-pass
        # outputs (diffcone.cache.ModuleCache). Applies to every snapshot kind.
        self.module_cache = module_cache
        self.scopes: dict[str, ModuleScope] = {}
        self.class_scopes: dict[str, ClassScope] = {}
        self._module_prefixes: set[str] = set()
        self._bases_final = False
        # Where writes go: the global output is backed by the index; pass 2
        # swaps in a per-module output so it can be cached (see _Output).
        self._global = _Output(
            edges=self.index.edges,
            unresolved=self.index.unresolved,
            external=self.index.external,
            reflection=self.index.reflection,
            class_attributes=self.index.class_attributes,
            class_bases=self.index.class_bases,
            open_classes=self.index.open_classes,
            doc_decorated=self.index.doc_decorated,
        )
        self.out = self._global
        # Symbols and class scopes added by the module being indexed, and
        # whether one of its symbols collided with an earlier module's.
        self._added_symbols: list[Symbol] = []
        self._added_classes: list[ClassScope] = []
        self._collided = False
        # Transitive in-scope descendants per class, built once bases are final.
        self._descendants: dict[str, tuple[str, ...]] = {}
        # Import bindings being resolved (guards self-referential imports).
        self._resolving_bindings: set[tuple[str, str]] = set()
        # Top-level import name -> the analysed package it most likely means
        # under another root (see Resolver._misrooted); built on first use.
        self._misrooted_names: dict[str, str] | None = None
        # Classes with an unresolved ``super().<name>``, per name (final pass).
        self._super_misses: dict[str, set[str]] = defaultdict(set)

    def _error(self, path: str, message: str) -> None:
        self.index.errors.append(
            AnalysisError(revision=self.snapshot.revision, path=path, message=message)
        )

    def _add_symbol(self, symbol: Symbol) -> bool:
        existing = self.index.symbols.get(symbol.id)
        if existing is not None:
            self._error(
                symbol.path,
                f"symbol identity {symbol.id!r} collides with {existing.kind} in {existing.path}",
            )
            self._collided = True
            return False
        self.index.symbols[symbol.id] = symbol
        self._added_symbols.append(symbol)
        return True

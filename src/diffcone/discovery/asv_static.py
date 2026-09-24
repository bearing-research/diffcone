"""Static ASV (airspeed velocity) benchmark discovery.

Reproduces ASV's collection rules without importing benchmark code:

* ``benchmark_dir`` from ``asv.conf.json`` at the repository root (default
  ``benchmarks``); every ``.py`` file below it whose name does not start with
  an underscore, named relative to that directory with dots;
* module-level functions and methods of classes (names not starting with an
  underscore) whose names start with ``time_``, ``timeraw_``, ``mem_``,
  ``peakmem_`` or ``track_``;
* benchmark name ``<module>.<Class>.<method>`` or ``<module>.<function>``.

Lifecycle dependencies: the class's ``setup``, ``setup_cache`` and
``teardown`` methods, the module's ``setup``, ``setup_cache`` and
``teardown`` functions, and the module itself (module-level attributes such
as ``timeout`` or ``params``). Class attributes reach the benchmarks through
the class body, which the planner treats as structural.

Benchmarks inherited from base classes count: ASV reads a class's
attributes including inherited ones. A base defined in the same module, or
imported from any module in the source roots (a shared base often lives in
the package under test), is followed, with the subclass's own definitions
winning; one that resolves nowhere is an ``unknown_base_class`` note.

Not modelled: ``params`` expansion (a benchmark is one target),
``benchmark_dir`` outside the source roots.
"""

from __future__ import annotations

import ast
import json
from pathlib import PurePosixPath
from typing import Any

from diffcone.discovery import DiscoveryNote, DiscoveryOptions, DiscoveryResult
from diffcone.discovery.common import (
    ParsedModule,
    decorator_chain,
    parse_modules,
    scope_classes,
    scope_functions,
)
from diffcone.manifest import Target
from diffcone.model import SourceIndex
from diffcone.snapshot import Snapshot, module_name_for

RUNNER = "asv"
PREFIXES = ("time_", "timeraw_", "mem_", "peakmem_", "track_")
LIFECYCLE_NAMES = ("setup", "setup_cache", "teardown")


def read_asv_config(snapshot: Snapshot) -> dict[str, Any]:
    config: dict[str, Any] = {"source": None, "benchmark_dir": "benchmarks"}
    raw = snapshot.config_files.get("asv.conf.json")
    if raw is None:
        return config
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        config["source"] = "asv.conf.json (unparsable, defaults used)"
        config["error"] = str(exc)
        return config
    config["source"] = "asv.conf.json"
    bench_dir = data.get("benchmark_dir")
    if isinstance(bench_dir, str) and bench_dir.strip():
        config["benchmark_dir"] = bench_dir.strip().strip("/")
    return config


def strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside string literals, as asv's
    configuration loader allows."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _is_benchmark(name: str) -> bool:
    return not name.startswith("_") and name.startswith(PREFIXES)


def _module_paths(snapshot: Snapshot) -> dict[str, str]:
    """Module name -> path for every ``.py`` file in the source roots: how a
    base class imported from the package under test is found."""
    out: dict[str, str] = {}
    for path in snapshot.files:
        if not path.endswith(".py"):
            continue
        name = module_name_for(path, snapshot.source_roots)
        if name is not None:
            out.setdefault(name, path)
    return out


def _imported_names(pm: ParsedModule) -> dict[str, tuple[str, str]]:
    """Bound name -> (module, original name) for ``from x import y`` at the
    top level of ``pm``; relative imports resolve against its own module."""
    out: dict[str, tuple[str, str]] = {}
    package = pm.module.rsplit(".", 1)[0] if "." in pm.module else ""
    for node in pm.tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = package.split(".")
            drop = node.level - 1
            base = base[: len(base) - drop] if drop else base
            source = ".".join([*base, node.module] if node.module else base)
        else:
            source = node.module or ""
        for alias in node.names:
            if alias.name != "*":
                out[alias.asname or alias.name] = (source, alias.name)
    return out


def discover_asv(
    snapshot: Snapshot, index: SourceIndex, options: DiscoveryOptions
) -> DiscoveryResult:
    result = DiscoveryResult(runner=RUNNER)
    config = read_asv_config(snapshot)
    result.config = dict(config)
    if "error" in config:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unparsable_config",
                f"asv.conf.json could not be parsed ({config['error']}); "
                f"using benchmark_dir {config['benchmark_dir']!r}",
            )
        )
    bench_dir = config["benchmark_dir"]
    prefix = bench_dir + "/"
    paths = [
        p
        for p in snapshot.files
        if p.startswith(prefix)
        and not any(part.startswith("_") and part != "__init__.py" for part in p.split("/"))
    ]
    if not paths:
        result.notes.append(
            DiscoveryNote(RUNNER, "no_benchmarks", f"no .py files under {bench_dir!r}")
        )
        return result
    parsed, failed = parse_modules(snapshot, paths)
    module_paths = _module_paths(snapshot)
    # Modules reached through a base class, parsed on demand and cached.
    extra: dict[str, ParsedModule | None] = {pm.module: pm for pm in parsed}

    def module_for(name: str) -> ParsedModule | None:
        if name not in extra:
            path = module_paths.get(name)
            found, _ = parse_modules(snapshot, [path]) if path else ([], [])
            extra[name] = found[0] if found else None
        return extra[name]

    def bases_of(cls: ast.ClassDef, pm: ParsedModule, runner_id: str) -> list[ast.ClassDef]:
        """Base classes of ``cls``, nearest first, each with the module that
        defines it; a base that resolves nowhere is reported."""
        chain: list[tuple[ast.ClassDef, ParsedModule]] = []
        seen: set[tuple[str, str]] = set()
        queue = [(base, pm) for base in cls.bases]
        while queue:
            node, owner = queue.pop(0)
            parts, _ = decorator_chain(node)
            name = parts[-1] if parts else ""
            if name in ("object", "") or (owner.module, name) in seen:
                continue
            seen.add((owner.module, name))
            here = {c.name: c for c in scope_classes(owner.tree.body)}
            found_cls, found_mod = here.get(name), owner
            if found_cls is None:
                target = _imported_names(owner).get(name)
                source = module_for(target[0]) if target else None
                if source is not None:
                    found_cls = {c.name: c for c in scope_classes(source.tree.body)}.get(target[1])
                    found_mod = source
            if found_cls is None:
                result.notes.append(
                    DiscoveryNote(
                        RUNNER,
                        "unknown_base_class",
                        f"{runner_id}: base class {name!r} is not defined in this module or "
                        "imported from one in the source roots; benchmarks it may contribute "
                        "are not discovered",
                    )
                )
                continue
            chain.append((found_cls, found_mod))
            queue.extend((b, found_mod) for b in found_cls.bases)
        return chain

    for path in failed:
        result.notes.append(
            DiscoveryNote(RUNNER, "unparsed_file", f"{path}: not parsed or outside source roots")
        )
    for pm in parsed:
        rel = PurePosixPath(pm.path[len(prefix) :]).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        if not parts:
            continue
        bench_module = ".".join(parts)
        body = pm.tree.body
        module_deps = [pm.module] + [
            pm.member_id(f.name) for f in scope_functions(body) if f.name in LIFECYCLE_NAMES
        ]

        def add(runner_id: str, entry: str, deps: list[str]) -> None:
            if entry not in index.symbols:
                result.notes.append(
                    DiscoveryNote(
                        RUNNER, "missing_symbol", f"{runner_id}: {entry} is not in the index"
                    )
                )
            result.targets.append(Target(RUNNER, runner_id, entry, tuple(sorted(set(deps)))))

        for func in scope_functions(body):
            if _is_benchmark(func.name):
                add(f"{bench_module}.{func.name}", pm.member_id(func.name), module_deps)
        for cls in scope_classes(body):
            if cls.name.startswith("_"):
                continue
            class_id = pm.member_id(cls.name)
            # ASV reads the class's attributes, inherited ones included; the
            # subclass's own definitions win, so bases are applied first.
            methods: dict[str, str] = {}
            for base_cls, base_mod in reversed(bases_of(cls, pm, f"{bench_module}.{cls.name}")):
                base_id = base_mod.member_id(base_cls.name)
                for f in scope_functions(base_cls.body):
                    methods[f.name] = f"{base_id}.{f.name}"
            for f in scope_functions(cls.body):
                methods[f.name] = f"{class_id}.{f.name}"
            deps = module_deps + [
                symbol for name, symbol in methods.items() if name in LIFECYCLE_NAMES
            ]
            for name, symbol in sorted(methods.items()):
                if _is_benchmark(name):
                    add(f"{bench_module}.{cls.name}.{name}", symbol, deps)
    result.targets.sort()
    return result

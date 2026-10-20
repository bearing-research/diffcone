"""Static ASV (airspeed velocity) benchmark discovery.

Reproduces ASV's collection rules without importing benchmark code:

* ``benchmark_dir`` from ``asv.conf.json`` or ``asv.conf.jsonc`` (the
  shallowest one, at the repository root or up to three levels below;
  default ``benchmarks``), relative to the directory holding it, read as
  JSON with comments and trailing commas; a configuration that does not
  read that way (asv reads JSON5) or that sits beside the other spelling
  is an ``unparsable_config`` note, which makes discovery incomplete, and
  the default directory is used; ``"."`` names the configuration's own
  directory, and a ``benchmark_dir`` outside the repository, whose files
  cannot be read, is a ``test_file_outside_roots`` note (incomplete);
  every ``.py`` file below it,
  underscore modules included (ASV walks the package with ``pkgutil``),
  named relative to that directory with dots, the package's own
  ``__init__`` with no module prefix at all;
* every public attribute of such a module, as ASV reads ``module.__dict__``:
  functions and classes defined there, imported there (by name or with a
  star) from a module in the source roots, named after the importing module
  and the object's own name, and attribute aliases in a class body
  (``time_alias = time_a``), under ``if``/``try`` too;
* functions and methods whose names match ASV's patterns: ``time_``,
  ``timeraw_``, ``mem_``, ``peakmem_`` or ``track_`` prefixes, or the
  CamelCase ``Time``, ``Timeraw``, ``Mem``, ``PeakMem`` and ``Track``
  followed by a capital or an underscore;
* benchmark name ``<module>.<Class>.<method>`` or ``<module>.<function>``.

Lifecycle dependencies: the class's ``setup``, ``setup_cache`` and
``teardown`` methods, the module's ``setup``, ``setup_cache`` and
``teardown`` functions (``setup`` and ``teardown`` in any case, ``setUp`` or
``TearDown``, as asv_runner matches them ignoring case; ``setup_cache`` as
written), the methods building the instance asv_runner makes of the class
for every benchmark (``__init__``, ``__new__``, ``__getattribute__``,
``__getattr__``, inherited ones included), and the module itself
(module-level attributes such as ``timeout`` or ``params``). Class
attributes reach the benchmarks through the class body, which the planner
treats as structural.

Benchmarks inherited from base classes count: ASV reads a class's
attributes including inherited ones. A base defined in the same module, or
imported from any module in the source roots (a shared base often lives in
the package under test), is followed, with the subclass's own definitions
winning; one that resolves nowhere is an ``unknown_base_class`` note.

Also: an attribute bound to a function (``time_alias = _impl``) is a
benchmark when the attribute's name matches, named after the function; a
literal ``benchmark_name`` set on a function replaces its name (and its last
part is what is matched) wherever the function is bound, since asv_runner
reads it off the function: imported, aliased or inherited from a base
class; two attributes that end up with one name are one benchmark, with
the dependencies of both; the module's ``setup``/``teardown`` count when
imported (``from .common import setup``) too; a benchmark matched as
``timeraw_`` (by the attribute's name or the ``benchmark_name``, not the
function's own name) depends (``dynamic:``) on the in-scope modules its
returned code imports, and on an unknown (always selected) when that code
is not a literal string.
Benchmark files under ``benchmark_dir`` but outside the source roots, or
kept out of the analysis by ``diffcone.toml``'s ``[index] exclude``, are
reported (``test_file_outside_roots``).

Not modelled: ``params`` expansion (a benchmark is one target), a
``benchmark_name`` that is not a literal string, ``skip_benchmark``
(such a function stays a target), and pkgutil's walk skipping a directory
without ``__init__.py`` (its files are benchmark modules here: an
over-collection).
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import textwrap
from collections import deque
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
from diffcone.indexer import iter_scope_statements
from diffcone.manifest import Target
from diffcone.model import DYNAMIC_DEPENDENCY, SourceIndex
from diffcone.snapshot import ASV_CONFIGS, Snapshot, excluded_paths, module_name_for

RUNNER = "asv"
# asv_runner's ``name_regex`` of each benchmark type, combined, and the
# ``timeraw`` type's own (matched with ``re.match``, as asv_runner does).
BENCHMARK_NAME = re.compile(
    r"^(?:(?:Time|Timeraw|Mem|PeakMem|Track)[A-Z_].+|(?:time|timeraw|mem|peakmem|track)_.+)$"
)
TIMERAW_NAME = re.compile(r"Timeraw[A-Z_].+|timeraw_.+")
# What building the benchmark's instance runs: asv_runner calls ``klass()``
# for every class benchmark and reads the method off the instance.
CONSTRUCTION_NAMES = ("__init__", "__new__", "__getattribute__", "__getattr__")


def _lifecycle(name: str) -> bool:
    """Whether asv_runner runs the attribute ``name`` around a benchmark: it
    looks ``setup`` and ``teardown`` up ignoring case (``setUp``,
    ``TearDown``; ``key.lower() == name.lower()`` over ``dir(source)``), and
    ``setup_cache`` by its exact name."""
    return name == "setup_cache" or name.lower() in ("setup", "teardown")


def read_asv_config(snapshot: Snapshot) -> dict[str, Any]:
    """``benchmark_dir`` as a repository-relative path, and the
    configuration file's own path (``path``, None without one). ASV resolves
    it against the directory holding ``asv.conf.json`` (or
    ``asv.conf.jsonc``), which is usually not the repository root (numpy and
    networkx keep both under ``benchmarks/``, pandas under ``asv_bench/``);
    the snapshot reads those nested copies, and the shallowest one wins as
    ASV's own search does. A ``benchmark_dir`` that leaves the repository
    keeps its leading ``/`` or ``..``; one naming the configuration's own
    directory at the root is ``"."``. A configuration that cannot be read
    has an ``error``: what it sets is unknown, and the defaults stand in for
    it."""
    config: dict[str, Any] = {"source": None, "benchmark_dir": "benchmarks", "path": None}
    names = sorted(
        (n for n in snapshot.config_files if PurePosixPath(n).name in ASV_CONFIGS),
        key=lambda n: (n.count("/"), n),
    )
    if not names:
        return config
    name = names[0]
    config["path"] = name
    here = PurePosixPath(name).parent
    twins = [n for n in names[1:] if PurePosixPath(n).parent == here]
    if twins:
        # asv refuses to choose between the two spellings.
        config["source"] = f"{name} (beside {twins[0]}, defaults used)"
        config["error"] = f"both {name} and {twins[0]} exist, and asv reads only one"
        return config
    text = snapshot.config_files[name].decode("utf-8", "replace")
    try:
        data = json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        config["source"] = f"{name} (unparsable, defaults used)"
        config["error"] = str(exc)
        return config
    if not isinstance(data, dict):
        config["source"] = f"{name} (not an object, defaults used)"
        config["error"] = "the configuration is not a JSON object"
        return config
    config["source"] = name
    bench_dir = data.get("benchmark_dir")
    if isinstance(bench_dir, str) and bench_dir.strip():
        # ``"../benchmarks"`` beside a nested config: normalised, so it is
        # compared with repository paths as they are.
        resolved = posixpath.normpath((here / bench_dir.strip().rstrip("/")).as_posix())
        if not _leaves_repository(resolved):
            resolved = resolved.removeprefix("./").strip("/") or "."
        config["benchmark_dir"] = resolved
    elif str(here) != ".":
        # No benchmark_dir: ASV's default is ``benchmarks`` beside the config.
        config["benchmark_dir"] = (here / "benchmarks").as_posix()
    return config


def _leaves_repository(path: str) -> bool:
    """Whether a normalised ``benchmark_dir`` lies outside the repository."""
    return path.startswith("/") or path == ".." or path.startswith("../")


def strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside string literals, and a
    comma before a closing ``]`` or ``}``, as asv's configuration loader
    (JSON5) allows."""
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
            if ch in "]}":
                # A trailing comma: the last character kept, past blanks.
                j = len(out) - 1
                while j >= 0 and out[j].isspace():
                    j -= 1
                if j >= 0 and out[j] == ",":
                    del out[j]
            out.append(ch)
        i += 1
    return "".join(out)


def _is_benchmark(name: str) -> bool:
    return bool(BENCHMARK_NAME.match(name))


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
    """Bound name -> (module, original name) for ``from x import y`` in
    ``pm``'s module scope (under ``if``/``try`` too)."""
    out: dict[str, tuple[str, str]] = {}
    for node in iter_scope_statements(pm.tree.body):
        if not isinstance(node, ast.ImportFrom):
            continue
        source = pm.import_source(node)
        for alias in node.names:
            if alias.name != "*":
                out[alias.asname or alias.name] = (source, alias.name)
    return out


def _aliases(body: list[ast.stmt]) -> list[tuple[str, str]]:
    """``(name, other)`` for each ``name = other`` in a scope (under
    ``if``/``try`` too): another attribute bound to what ``other`` is."""
    return [
        (stmt.targets[0].id, stmt.value.id)
        for stmt in iter_scope_statements(body)
        if isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Name)
    ]


def _class_methods(cls: ast.ClassDef, class_id: str, methods: dict[str, str]) -> None:
    """A class's functions by attribute name, aliases included
    (``time_alias = time_a`` is a member ``inspect.getmembers`` returns)."""
    defs = {f.name: f for f in scope_functions(cls.body)}
    for name in defs:
        methods[name] = f"{class_id}.{name}"
    for name, other in _aliases(cls.body):
        if other in defs:
            methods[name] = f"{class_id}.{other}"


def _bind_def(
    origin: ParsedModule,
    original: str,
    bound: str,
    functions: dict[str, tuple[Any, ParsedModule]],
    classes: dict[str, tuple[ast.ClassDef, ParsedModule]],
) -> None:
    for node in iter_scope_statements(origin.tree.body):
        if isinstance(node, ast.ClassDef) and node.name == original:
            classes[bound] = (node, origin)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == original:
            functions[bound] = (node, origin)


def _public_defs(pm: ParsedModule) -> list[str]:
    """What ``from m import *`` binds of ``m``'s definitions."""
    for stmt in iter_scope_statements(pm.tree.body):
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets
        ):
            if isinstance(stmt.value, (ast.List, ast.Tuple)):
                return [
                    e.value
                    for e in stmt.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
    return [
        n.name
        for n in iter_scope_statements(pm.tree.body)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not n.name.startswith("_")
    ]


def _benchmark_names(body: list[ast.stmt]) -> dict[str, str]:
    """``time_x.benchmark_name = "custom.name"`` in a module or class body:
    the name the statement sets it on -> the name ASV gives the benchmark
    instead (an attribute of the function, so it follows the function
    wherever it is bound, and into subclasses)."""
    out: dict[str, str] = {}
    for stmt in iter_scope_statements(body):
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Attribute)
            and stmt.targets[0].attr == "benchmark_name"
            and isinstance(stmt.targets[0].value, ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            out[stmt.targets[0].value.id] = stmt.value.value
    return out


# A ``timeraw_`` benchmark whose code could not be read: an unknown
# dependency, so the benchmark is always selected.
TIMERAW_UNANALYSED = "timeraw:unanalysed"


def _timeraw_deps(
    func: ast.FunctionDef | ast.AsyncFunctionDef, index: SourceIndex, search: str
) -> list[str]:
    """What a ``timeraw_`` benchmark runs: it returns code (a string, or a
    pair of code and setup strings) that ASV runs in a fresh interpreter, so
    its dependencies are what that code imports (``dynamic:<module>`` for
    each in-scope module it names). Code that is not a literal cannot be
    read: TIMERAW_UNANALYSED. ``search`` is the name asv_runner matches the
    benchmark types on (the attribute's, or a ``benchmark_name``'s last
    part), not the function's own."""
    if not TIMERAW_NAME.match(search):
        return []
    deps: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        values = list(node.value.elts) if isinstance(node.value, ast.Tuple) else [node.value]
        for value in values:
            if isinstance(value, ast.Call) and value.args:  # ``textwrap.dedent("...")``
                value = value.args[0]
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return [TIMERAW_UNANALYSED]
            try:
                code = ast.parse(textwrap.dedent(value.value))
            except (SyntaxError, ValueError, RecursionError):
                return [TIMERAW_UNANALYSED]
            for inner in ast.walk(code):
                names = (
                    [a.name for a in inner.names]
                    if isinstance(inner, ast.Import)
                    else [inner.module or ""]
                    if isinstance(inner, ast.ImportFrom) and not inner.level
                    else []
                )
                for name in names:
                    parts = name.split(".")
                    for i in range(len(parts), 0, -1):
                        if ".".join(parts[:i]) in index.modules:
                            deps.append(f"{DYNAMIC_DEPENDENCY}{'.'.join(parts[:i])}")
                            break
    return deps


def discover_asv(
    snapshot: Snapshot, index: SourceIndex, options: DiscoveryOptions
) -> DiscoveryResult:
    result = DiscoveryResult(runner=RUNNER)
    config = read_asv_config(snapshot)
    # The report shows ``source``; ``path`` is for tools that run asv there.
    result.config = {k: v for k, v in config.items() if k != "path"}
    if "error" in config:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "unparsable_config",
                f"the asv configuration could not be read ({config['error']}), so its "
                f"benchmark_dir is not known; using {config['benchmark_dir']!r}, and "
                "benchmarks elsewhere are not targets",
            )
        )
    bench_dir = config["benchmark_dir"]
    if _leaves_repository(bench_dir):
        # Benchmarks the repository does not hold: nothing here can be read.
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "test_file_outside_roots",
                f"{config['source']} sets benchmark_dir to {bench_dir!r}, outside the "
                "repository, so its benchmarks cannot be read and are not targets",
                config["path"] or "",
            )
        )
        return result
    # ``"."``: the configuration's own directory, the repository root.
    prefix = "" if bench_dir == "." else bench_dir + "/"
    paths = [p for p in snapshot.files if p.startswith(prefix) and p.endswith(".py")]
    excluded = set(excluded_paths(snapshot))
    outside = [p for p in snapshot.python_paths if p.startswith(prefix) and p not in snapshot.files]
    kept_out = [p for p in outside if p in excluded]
    outside = [p for p in outside if p not in excluded]
    if kept_out:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "test_file_outside_roots",
                f"{len(kept_out)} benchmark file(s) under {bench_dir!r} are kept out of the "
                f"analysis by diffcone.toml's [index] exclude (first: {kept_out[0]}), so their "
                "benchmarks are not targets; narrow the exclude pattern that matches them",
                kept_out[0],
            )
        )
    if outside:
        result.notes.append(
            DiscoveryNote(
                RUNNER,
                "test_file_outside_roots",
                f"{len(outside)} benchmark file(s) under {bench_dir!r} are outside the source "
                f"roots (first: {outside[0]}), so their benchmarks are not targets; add a source "
                "root that contains them",
                outside[0],
            )
        )
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

    def bases_of(
        cls: ast.ClassDef, pm: ParsedModule, runner_id: str
    ) -> list[tuple[ast.ClassDef, ParsedModule]]:
        """Base classes of ``cls``, nearest first, each with the module that
        defines it; a base that resolves nowhere is reported."""
        chain: list[tuple[ast.ClassDef, ParsedModule]] = []
        seen: set[tuple[str, str]] = set()
        queue = deque((base, pm) for base in cls.bases)
        while queue:
            node, owner = queue.popleft()
            parts, _ = decorator_chain(node)
            name = parts[-1] if parts else ""
            if name in ("object", "") or (owner.module, name) in seen:
                continue
            seen.add((owner.module, name))
            here = {c.name: c for c in scope_classes(owner.tree.body)}
            found_cls, found_mod = here.get(name), owner
            if found_cls is None:
                target = _imported_names(owner).get(name)
                source = module_for(target[0]) if target is not None else None
                if target is not None and source is not None:
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

    for path, why in failed.items():
        result.notes.append(
            DiscoveryNote(
                RUNNER, "unparsed_file", f"{path}: {why or 'cannot be named from any source root'}"
            )
        )

    def qualified(*names: str) -> str:
        return ".".join(n for n in names if n)

    # Runner id -> position in result.targets.
    added: dict[str, int] = {}

    def add(runner_id: str, entry: str, deps: list[str]) -> None:
        if runner_id in added:
            # Two attributes bound to one function, or two functions given
            # one ``benchmark_name``: one name, whatever either runs.
            i = added[runner_id]
            known = result.targets[i]
            merged = tuple(sorted({*known.lifecycle_dependencies, *deps}))
            result.targets[i] = Target(RUNNER, runner_id, known.entry_symbol, merged)
            return
        added[runner_id] = len(result.targets)
        if entry not in index.symbols:
            result.notes.append(
                DiscoveryNote(RUNNER, "missing_symbol", f"{runner_id}: {entry} is not in the index")
            )
        result.targets.append(Target(RUNNER, runner_id, entry, tuple(sorted(set(deps)))))

    def benchmark(search: str, renamed: str | None) -> bool:
        """Whether asv_runner makes a benchmark of an attribute: it matches
        the attribute's name, or the last part of a ``benchmark_name``."""
        return _is_benchmark(renamed.split(".")[-1] if renamed else search)

    for pm in parsed:
        rel = PurePosixPath(pm.path[len(prefix) :]).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        # The package's own __init__ has no module prefix in ASV's names.
        bench_module = ".".join(parts)
        body = pm.tree.body

        # Public module attributes: (function or class node, its module).
        functions: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, ParsedModule]] = {}
        classes: dict[str, tuple[ast.ClassDef, ParsedModule]] = {}
        for node in iter_scope_statements(body):
            if not isinstance(node, ast.ImportFrom):
                continue
            origin = module_for(pm.import_source(node))
            if origin is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    for name in _public_defs(origin):
                        _bind_def(origin, name, name, functions, classes)
                elif not (alias.asname or alias.name).startswith("_"):
                    _bind_def(origin, alias.name, alias.asname or alias.name, functions, classes)
        for func in scope_functions(body):
            functions[func.name] = (func, pm)
        for cls in scope_classes(body):
            classes[cls.name] = (cls, pm)
        # ``time_alias = _impl``: another attribute bound to the function.
        for name, other in _aliases(body):
            if other in functions:
                functions[name] = functions[other]
        # The module's ``setup``/``teardown``, defined there or imported
        # (``from .common import setup``), run around each benchmark.
        module_deps = [pm.module] + [
            owner.member_id(func.name)
            for name, (func, owner) in sorted(functions.items())
            if _lifecycle(name)
        ]
        # ``benchmark_name`` set on a function, here or in the module it
        # comes from: by the function, wherever it is bound.
        renamed: dict[str, str] = {}
        owners = {id(pm): pm, **{id(o): o for _, o in functions.values()}}
        for owner in owners.values():
            local = {f.name: (f, owner) for f in scope_functions(owner.tree.body)}
            for name, value in _benchmark_names(owner.tree.body).items():
                found = functions.get(name) if owner is pm else local.get(name)
                if found is not None:
                    renamed[found[1].member_id(found[0].name)] = value
        for bound, (func, owner) in sorted(functions.items()):
            if bound.startswith("_"):
                continue
            # ASV names the benchmark by its ``benchmark_name`` or the
            # function's own name (``func.__name__``).
            name = renamed.get(owner.member_id(func.name))
            if not benchmark(bound, name):
                continue
            search = name.split(".")[-1] if name else bound
            add(
                name or qualified(bench_module, func.name),
                owner.member_id(func.name),
                module_deps + _timeraw_deps(func, index, search),
            )
        for bound, (cls, owner) in sorted(classes.items()):
            if bound.startswith("_"):
                continue
            runner_prefix = qualified(bench_module, cls.name)  # ``klass.__name__``
            # ASV reads the class's attributes, inherited ones included; the
            # subclass's own definitions win, so bases are applied first. A
            # ``benchmark_name`` set in a base's body stays on its function.
            methods: dict[str, str] = {}
            nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            class_renamed: dict[str, str] = {}
            for base_cls, base_mod in [
                *reversed(bases_of(cls, owner, runner_prefix)),
                (cls, owner),
            ]:
                base_id = base_mod.member_id(base_cls.name)
                own: dict[str, str] = {}
                _class_methods(base_cls, base_id, own)
                methods.update(own)
                nodes.update({f"{base_id}.{f.name}": f for f in scope_functions(base_cls.body)})
                for name, value in _benchmark_names(base_cls.body).items():
                    if name in own:
                        class_renamed[own[name]] = value
            deps = module_deps + [
                symbol
                for name, symbol in methods.items()
                if _lifecycle(name) or name in CONSTRUCTION_NAMES
            ]
            if owner is not pm:
                deps.append(owner.module)
            for name, symbol in sorted(methods.items()):
                custom = class_renamed.get(symbol)
                if not benchmark(name, custom):
                    continue
                search = custom.split(".")[-1] if custom else name
                code_deps = _timeraw_deps(nodes[symbol], index, search) if symbol in nodes else []
                add(custom or f"{runner_prefix}.{name}", symbol, deps + code_deps)
    result.targets.sort()
    return result

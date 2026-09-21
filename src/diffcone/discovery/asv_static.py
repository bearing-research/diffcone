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

Not modelled: ``params`` expansion (a benchmark is one target), benchmarks
inherited from base classes, ``benchmark_dir`` outside the source roots.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from diffcone.discovery import DiscoveryNote, DiscoveryOptions, DiscoveryResult
from diffcone.discovery.common import parse_modules, scope_classes, scope_functions
from diffcone.manifest import Target
from diffcone.model import SourceIndex
from diffcone.snapshot import Snapshot

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
            deps = module_deps + [
                f"{class_id}.{f.name}"
                for f in scope_functions(cls.body)
                if f.name in LIFECYCLE_NAMES
            ]
            for func in scope_functions(cls.body):
                if _is_benchmark(func.name):
                    add(f"{bench_module}.{cls.name}.{func.name}", f"{class_id}.{func.name}", deps)
    result.targets.sort()
    return result

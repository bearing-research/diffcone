"""Prototype of a tolerant Cython block reader (roadmap item 7): function and
method spans in .pyx/.pxd/.pxi files, from indentation alone.

A function is a `def`, `cpdef` or `cdef` statement with a parameter list and a
body (the header may span lines; it ends with ':' at bracket depth 0). `cdef
class`/`class` open a scope whose functions are methods. Struct, enum, extern,
fused and ctypedef blocks and variable declarations are not functions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HEAD = re.compile(r"^(\s*)(cpdef|cdef|def)\b(.*)$")
CLASS = re.compile(r"^(\s*)(?:cdef\s+(?:public\s+|readonly\s+)?class|class)\s+(\w+)")
NOT_FUNC = re.compile(
    r"^\s*cdef\s+(class|struct|enum|union|extern|packed|fused|public\s+(struct|enum))\b"
)
# Every name a body mentions: a call, or a function taken as a pointer
# (pandas' period.pyx returns its asfreq converters from get_asfreq_func).
CALL = re.compile(r"\b([A-Za-z_]\w*)\b")


@dataclass
class Func:
    path: str
    qualname: str
    start: int  # the def line (first decorator excluded)
    end: int
    nogil: bool
    calls: set[str] = field(default_factory=set)
    header: str = ""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def read(path: Path, rel: str) -> list[Func]:
    lines = path.read_text(errors="replace").split("\n")
    out: list[Func] = []
    scopes: list[tuple[int, str]] = []  # (indent, class name)
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        ind = _indent(line)
        while scopes and ind <= scopes[-1][0]:
            scopes.pop()
        m = CLASS.match(line)
        if m and stripped.endswith(":"):
            scopes.append((ind, m.group(2)))
            i += 1
            continue
        m = HEAD.match(line)
        if m and not NOT_FUNC.match(line) and "(" in line:
            # Gather the header up to ':' at bracket depth 0.
            header, j, depth = "", i, 0
            while j < n:
                part = lines[j].split("#", 1)[0]
                header += " " + part.strip()
                depth += part.count("(") + part.count("[") - part.count(")") - part.count("]")
                if depth <= 0 and part.rstrip().endswith(":"):
                    break
                if depth <= 0 and j > i and not part.rstrip().endswith((",", "(", "\\")):
                    j = -1  # a declaration without a body
                    break
                j += 1
            if j == -1 or j >= n or not header.rstrip().endswith(":"):
                i += 1
                continue
            before = header.split("(", 1)[0]
            name_part = before.split()
            name = name_part[-1] if name_part else "?"
            if "=" in before or not re.fullmatch(r"[A-Za-z_]\w*", name):
                i += 1  # a declaration such as `cdef int64_t **x = <int64_t**>malloc(...)`
                continue
            body_start = j + 1
            k = body_start
            last = j
            while k < n:
                s = lines[k]
                if s.strip() and not s.strip().startswith("#"):
                    if _indent(s) <= ind:
                        break
                    last = k
                k += 1
            qual = ".".join([c for _, c in scopes] + [name])
            # The span starts at the first decorator, as code objects do.
            first = i
            while (
                first > 0
                and lines[first - 1].strip().startswith("@")
                and _indent(lines[first - 1]) == ind
            ):
                first -= 1
            calls = set()
            for b in range(body_start, last + 1):
                calls.update(CALL.findall(lines[b].split("#", 1)[0]))
            out.append(
                Func(rel, qual, first + 1, last + 1, " nogil" in header, calls, header.strip())
            )
            i += 1  # nested functions are read too; owners fold them into their parent
            continue
        i += 1
    return out


def read_tree(root: Path) -> list[Func]:
    funcs = []
    for p in sorted((root / "pandas" / "_libs").rglob("*")):
        if p.suffix in (".pyx", ".pxd", ".pxi"):
            funcs.extend(read(p, p.relative_to(root).as_posix()))
    return funcs


if __name__ == "__main__":
    import sys

    funcs = read_tree(Path(sys.argv[1]))
    print(len(funcs), "functions;", sum(f.nogil for f in funcs), "nogil")
    by_file = {}
    for f in funcs:
        by_file.setdefault(f.path, []).append(f)
    for path in sys.argv[2:]:
        for f in by_file.get(path, [])[:60]:
            print(f"  {f.start:5}-{f.end:<5} {'nogil ' if f.nogil else ''}{f.qualname}")

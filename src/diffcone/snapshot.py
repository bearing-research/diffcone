"""Git snapshot reader.

Reads Python sources at a committed revision straight from the object store.
It never checks anything out and never touches the working tree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(Exception):
    """Raised when git cannot supply the requested snapshot."""


@dataclass
class Snapshot:
    revision: str
    commit: str
    source_roots: list[str]
    files: dict[str, bytes] = field(default_factory=dict)  # repo-relative path -> content


def _git(repo: Path, args: list[str], stdin: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            input=stdin,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment problem
        raise GitError("git executable not found") from exc
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or "git command failed"
        raise GitError(f"git {' '.join(args[:2])}: {message}")
    return proc.stdout


def resolve_commit(repo: Path, revision: str) -> str:
    out = _git(repo, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
    commit = out.decode().strip()
    if not commit:
        raise GitError(f"revision {revision!r} does not name a commit")
    return commit


def _normalise_root(root: str) -> str:
    root = root.strip().strip("/")
    return "" if root in ("", ".") else root


def list_python_files(repo: Path, commit: str, source_roots: list[str]) -> list[str]:
    roots = [_normalise_root(r) for r in source_roots]
    args = ["ls-tree", "-r", "--name-only", "-z", commit]
    pathspecs = [r for r in roots if r]
    if pathspecs and "" not in roots:
        args += ["--", *pathspecs]
    out = _git(repo, args)
    paths = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    return sorted(p for p in paths if p.endswith(".py"))


def read_files(repo: Path, commit: str, paths: list[str]) -> dict[str, bytes]:
    if not paths:
        return {}
    request = "".join(f"{commit}:{p}\n" for p in paths).encode("utf-8", "surrogateescape")
    out = _git(repo, ["cat-file", "--batch"], stdin=request)
    files: dict[str, bytes] = {}
    pos = 0
    for path in paths:
        newline = out.index(b"\n", pos)
        header = out[pos:newline].decode("utf-8", "replace")
        pos = newline + 1
        parts = header.split()
        if len(parts) < 3 or parts[-1] == "missing":
            raise GitError(f"cannot read {path} at {commit}: {header}")
        size = int(parts[2])
        files[path] = out[pos : pos + size]
        pos += size + 1  # trailing newline after each object
    return files


def read_snapshot(repo: Path, revision: str, source_roots: list[str]) -> Snapshot:
    commit = resolve_commit(repo, revision)
    paths = list_python_files(repo, commit, source_roots)
    return Snapshot(
        revision=revision,
        commit=commit,
        source_roots=list(source_roots),
        files=read_files(repo, commit, paths),
    )


def module_name_for(path: str, source_roots: list[str]) -> str | None:
    """Map a repo-relative ``.py`` path to a dotted module name.

    The longest matching source root wins. Returns ``None`` when the path lies
    outside every root.
    """
    best: str | None = None
    best_len = -1
    for raw in source_roots:
        root = _normalise_root(raw)
        if root == "":
            rel = path
        elif path.startswith(root + "/"):
            rel = path[len(root) + 1 :]
        else:
            continue
        if len(root) > best_len:
            best, best_len = rel, len(root)
    if best is None:
        return None
    rel = best[: -len(".py")]
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)

"""Git snapshot reader.

Three kinds of snapshot can be read, and every one says what it is:

* ``commit`` (any git revision): sources come straight from the object store;
  nothing is checked out and the working tree is not touched.
* ``INDEX``: the staged content of every tracked file (what ``git commit``
  would record right now).
* ``WORKTREE``: the files on disk, tracked or untracked, excluding ignored
  ones; tracked files deleted from disk are absent.

The last two are always reported as uncommitted state on top of ``HEAD``.
"""

from __future__ import annotations

import os
import posixpath
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from diffcone.model import (
    KIND_COMMIT,
    KIND_INDEX,
    KIND_WORKTREE,
    AnalysisError,
    SnapshotInfo,
)


class GitError(Exception):
    """Raised when git cannot supply the requested snapshot."""


CONFIG_FILES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "asv.conf.json")

WORKTREE = "WORKTREE"
INDEX = "INDEX"


@dataclass
class Snapshot:
    info: SnapshotInfo
    source_roots: list[str]
    files: dict[str, bytes] = field(default_factory=dict)  # repo-relative path -> content
    # Root-level runner configuration files, when present (see CONFIG_FILES).
    config_files: dict[str, bytes] = field(default_factory=dict)
    # Problems reading the snapshot itself (e.g. unmerged index entries).
    errors: list[AnalysisError] = field(default_factory=list)

    @property
    def revision(self) -> str:
        return self.info.revision

    @property
    def commit(self) -> str:
        return self.info.commit

    @property
    def kind(self) -> str:
        return self.info.kind

    @property
    def description(self) -> str:
        return self.info.description


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


def split_root(spec: str) -> tuple[str, str]:
    """``(directory, module prefix)`` of a source-root spec.

    A spec is a repo-relative directory, optionally followed by ``=PREFIX``:
    modules under the directory are then named ``PREFIX.<path>`` instead of
    ``<path>``. That gives a monorepo's per-package test trees, whose files
    share names (``opentelemetry-api/tests/trace/test_globals.py`` and
    ``opentelemetry-sdk/tests/trace/test_globals.py``), distinct identities
    when one pytest session collects them (``--import-mode=importlib``). A
    prefixed name is diffcone's, never Python's: it is not used to resolve
    imports. The directory is normalised (``.`` and ``""`` are the root).
    """
    directory, sep, prefix = spec.partition("=")
    directory = directory.strip().strip("/")
    directory = "" if directory in ("", ".") else directory
    prefix = prefix.strip() if sep else ""
    if sep and (not prefix or not all(p.isidentifier() for p in prefix.split("."))):
        raise ValueError(f"source root {spec!r}: the module prefix must be a dotted identifier")
    return directory, prefix


def _normalise_root(root: str) -> str:
    return split_root(root)[0]


SYMLINK_MODE = "120000"


def _ls_tree(repo: Path, commit: str, pathspecs: list[str]) -> list[tuple[str, str]]:
    """(mode, path) of every blob under ``pathspecs`` (all when empty)."""
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if pathspecs:
        args += ["--", *pathspecs]
    entries: list[tuple[str, str]] = []
    for record in _git(repo, args).split(b"\0"):
        if not record:
            continue
        meta, _, path = record.decode("utf-8", "surrogateescape").partition("\t")
        entries.append((meta.split()[0], path))
    return entries


def _root_pathspecs(source_roots: list[str]) -> list[str]:
    roots = [_normalise_root(r) for r in source_roots]
    return [] if "" in roots else [r for r in roots if r]


def list_python_files(repo: Path, commit: str, source_roots: list[str]) -> list[str]:
    entries = _ls_tree(repo, commit, _root_pathspecs(source_roots))
    return sorted(p for m, p in entries if p.endswith(".py") and m != SYMLINK_MODE)


def _link_target(link: str, target: str) -> str | None:
    """The repository path a relative symlink at ``link`` points to, or None
    when it is absolute, leaves the repository or is the repository root."""
    if not target or target.startswith("/"):
        return None
    real = posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
    if real in (".", "..") or real.startswith("../"):
        return None
    return real


def expand_symlinks(
    links: dict[str, str], files_under: Callable[[str], list[str]]
) -> dict[str, str]:
    """Python files reached through tracked symlinks inside the repository:
    ``{path through the link: real path}``. A file link maps itself; a
    directory link maps every file under its target to the same relative
    path under the link (pytest collects them there). ``files_under`` lists
    the non-link files at or under a real path, so links inside an expanded
    tree are not followed again."""
    aliases: dict[str, str] = {}
    for link, target in sorted(links.items()):
        real = _link_target(link, target)
        if real is None:
            continue
        for path in files_under(real):
            if path == real:
                alias = link
            elif path.startswith(real + "/"):
                alias = link + path[len(real) :]
            else:
                continue
            if alias.endswith(".py"):
                aliases.setdefault(alias, path)
    return aliases


def read_files(
    repo: Path, commit: str, paths: list[str], *, label: str | None = None
) -> dict[str, bytes]:
    """Read blobs ``<commit>:<path>``; ``commit=""`` reads the index (``:path``)."""
    if not paths:
        return {}
    label = label or commit
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
            raise GitError(f"cannot read {path} at {label}: {header}")
        size = int(parts[2])
        files[path] = out[pos : pos + size]
        pos += size + 1  # trailing newline after each object
    return files


def list_root_files(repo: Path, commit: str) -> set[str]:
    out = _git(repo, ["ls-tree", "--name-only", "-z", commit])
    return {p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p}


def _pathspec(source_roots: list[str]) -> list[str]:
    roots = [_normalise_root(r) for r in source_roots]
    if "" in roots:
        return []
    return ["--", *[r for r in roots if r]]


# ``git ls-files -t`` tags: H cached, S skip-worktree (sparse checkout), M unmerged,
# ? untracked (with --others).
TAG_CACHED, TAG_SKIP_WORKTREE, TAG_UNMERGED, TAG_OTHER = "H", "S", "M", "?"


def _ls_files_tagged(repo: Path, args: list[str], source_roots: list[str]) -> list[tuple[str, str]]:
    out = _git(repo, ["ls-files", "-z", "-t", *args, *_pathspec(source_roots)])
    entries: set[tuple[str, str]] = set()
    for record in out.split(b"\0"):
        if not record:
            continue
        tag, _, path = record.decode("utf-8", "surrogateescape").partition(" ")
        entries.add((tag, path))
    return sorted(entries, key=lambda e: (e[1], e[0]))


def _ls_files_staged(repo: Path, source_roots: list[str]) -> dict[str, str]:
    """Stage-0 index entries under the roots: path -> mode."""
    out = _git(repo, ["ls-files", "-z", "--stage", *_pathspec(source_roots)])
    entries: dict[str, str] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.decode("utf-8", "surrogateescape").partition("\t")
        mode, _, stage = meta.split()
        if stage == "0":
            entries[path] = mode
    return entries


def _staged_config_files(repo: Path) -> dict[str, bytes]:
    out = _git(repo, ["ls-files", "-z", "--cached", "--", *CONFIG_FILES])
    names = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    return read_files(repo, "", names, label=INDEX)


def read_commit_snapshot(
    repo: Path, revision: str, source_roots: list[str], *, with_config: bool = False
) -> Snapshot:
    commit = resolve_commit(repo, revision)
    entries = _ls_tree(repo, commit, _root_pathspecs(source_roots))
    paths = sorted(p for m, p in entries if p.endswith(".py") and m != SYMLINK_MODE)
    link_paths = [p for m, p in entries if m == SYMLINK_MODE]
    targets = read_files(repo, commit, link_paths)

    tree: list[str] | None = None

    def files_under(real: str) -> list[str]:
        nonlocal tree
        if tree is None:  # one listing of the whole tree, only when there are links
            tree = [p for m, p in _ls_tree(repo, commit, []) if m != SYMLINK_MODE]
        return [p for p in tree if p == real or p.startswith(real + "/")]

    aliases = expand_symlinks(
        {k: v.decode("utf-8", "surrogateescape") for k, v in targets.items()}, files_under
    )
    listed = set(paths)
    aliases = {a: r for a, r in aliases.items() if a not in listed}
    files = read_files(repo, commit, paths)
    real_files = read_files(repo, commit, sorted(set(aliases.values())))
    files.update({alias: real_files[real] for alias, real in aliases.items()})
    config_files: dict[str, bytes] = {}
    if with_config:
        root = list_root_files(repo, commit)
        config_files = read_files(repo, commit, [n for n in CONFIG_FILES if n in root])
    return Snapshot(
        info=SnapshotInfo(
            revision=revision,
            commit=commit,
            kind=KIND_COMMIT,
            description=f"commit {commit[:12]} ({revision})",
        ),
        source_roots=list(source_roots),
        files=dict(sorted(files.items())),
        config_files=config_files,
    )


def read_index_snapshot(
    repo: Path, source_roots: list[str], *, with_config: bool = False
) -> Snapshot:
    """The staged content of tracked files (git's index).

    Unmerged paths (a merge in progress) have no stage-0 blob; they are
    recorded as analysis errors so the plan degrades instead of failing.
    """
    head = resolve_commit(repo, "HEAD")
    errors: list[AnalysisError] = []
    paths: list[str] = []
    for tag, path in _ls_files_tagged(repo, ["--cached"], source_roots):
        if not path.endswith(".py"):
            continue
        if tag == TAG_UNMERGED:
            if path not in paths and not any(e.path == path for e in errors):
                errors.append(
                    AnalysisError(
                        INDEX, path, "unmerged in the index (merge in progress); no staged content"
                    )
                )
        elif path not in paths:
            paths.append(path)
    paths = [p for p in paths if not any(e.path == p for e in errors)]
    staged = _ls_files_staged(repo, source_roots)
    link_paths = [p for p, m in staged.items() if m == SYMLINK_MODE]
    paths = [p for p in paths if staged.get(p) != SYMLINK_MODE]
    targets = read_files(repo, "", link_paths, label=INDEX)

    staged_all: list[str] | None = None

    def files_under(real: str) -> list[str]:
        nonlocal staged_all
        if staged_all is None:
            staged_all = [p for p, m in _ls_files_staged(repo, []).items() if m != SYMLINK_MODE]
        return [p for p in staged_all if p == real or p.startswith(real + "/")]

    aliases = expand_symlinks(
        {k: v.decode("utf-8", "surrogateescape") for k, v in targets.items()}, files_under
    )
    listed = set(paths)
    aliases = {a: r for a, r in aliases.items() if a not in listed}
    files = read_files(repo, "", paths, label=INDEX)
    real_files = read_files(repo, "", sorted(set(aliases.values())), label=INDEX)
    files.update({alias: real_files[real] for alias, real in aliases.items()})
    config_files = _staged_config_files(repo) if with_config else {}
    return Snapshot(
        info=SnapshotInfo(
            revision=INDEX,
            commit=head,
            kind=KIND_INDEX,
            description=f"git index (staged content) on top of commit {head[:12]}; uncommitted",
        ),
        source_roots=list(source_roots),
        files=dict(sorted(files.items())),
        config_files=config_files,
        errors=errors,
    )


def read_worktree_snapshot(
    repo: Path, source_roots: list[str], *, with_config: bool = False
) -> Snapshot:
    """Files on disk: tracked and untracked, minus ignored ones.

    Skip-worktree entries (sparse checkouts) are not on disk by design and
    are read from the index instead of being treated as deletions.
    """
    head = resolve_commit(repo, "HEAD")
    listed = _ls_files_tagged(repo, ["--cached", "--others", "--exclude-standard"], source_roots)
    files: dict[str, bytes] = {}
    from_index: list[str] = []
    for tag, path in listed:
        if not path.endswith(".py") or path in files or path in from_index:
            continue
        full = repo / path
        if full.is_file():
            files[path] = full.read_bytes()
        elif tag == TAG_SKIP_WORKTREE:
            from_index.append(path)
        # otherwise: a tracked file deleted on disk is absent from the snapshot
    files.update(read_files(repo, "", from_index, label=INDEX))
    # Symlinked directories (a file link was read through above).
    links = {
        path: os.readlink(repo / path)
        for _, path in listed
        if (repo / path).is_symlink() and (repo / path).is_dir()
    }

    def files_under(real: str) -> list[str]:
        found = _ls_files_tagged(repo, ["--cached", "--others", "--exclude-standard"], [real])
        return sorted({p for _, p in found if (repo / p).is_file() and not (repo / p).is_symlink()})

    for alias, real in expand_symlinks(links, files_under).items():
        if alias not in files:
            files[alias] = (repo / real).read_bytes()
    files = dict(sorted(files.items()))
    config_files: dict[str, bytes] = {}
    if with_config:
        for name in CONFIG_FILES:
            full = repo / name
            if full.is_file():
                config_files[name] = full.read_bytes()
    return Snapshot(
        info=SnapshotInfo(
            revision=WORKTREE,
            commit=head,
            kind=KIND_WORKTREE,
            description=(
                "working tree (tracked and untracked files, ignored files excluded) on top of "
                f"commit {head[:12]}; uncommitted"
            ),
        ),
        source_roots=list(source_roots),
        files=files,
        config_files=config_files,
    )


def read_snapshot(
    repo: Path, revision: str, source_roots: list[str], *, with_config: bool = False
) -> Snapshot:
    """Read the Python sources at ``revision``: a commit, ``INDEX`` or ``WORKTREE``.

    ``with_config`` also reads the root-level runner configuration files;
    only discovery needs them, so the base snapshot skips the extra work.
    """
    if revision == WORKTREE:
        return read_worktree_snapshot(repo, source_roots, with_config=with_config)
    if revision == INDEX:
        return read_index_snapshot(repo, source_roots, with_config=with_config)
    return read_commit_snapshot(repo, revision, source_roots, with_config=with_config)


def module_name_for(path: str, source_roots: list[str]) -> str | None:
    """Map a repo-relative ``.py`` path to a dotted module name.

    The longest matching source root wins. Returns ``None`` when the path lies
    outside every root.
    """
    best: tuple[str, str] | None = None
    best_len = -1
    for raw in source_roots:
        root, prefix = split_root(raw)
        if root == "":
            rel = path
        elif path.startswith(root + "/"):
            rel = path[len(root) + 1 :]
        else:
            continue
        if len(root) > best_len:
            best, best_len = (rel, prefix), len(root)
    if best is None:
        return None
    rel, prefix = best
    rel = rel[: -len(".py")]
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if (not parts and not prefix) or not all(p.isidentifier() for p in parts):
        return None
    return ".".join([*prefix.split("."), *parts] if prefix else parts)


def child_modules(snapshot: Snapshot) -> dict[str, frozenset[str]]:
    """Immediate submodule names per module, from the files present
    (whether or not they parse): what a package binding can shadow."""
    children: dict[str, set[str]] = {}
    for path in snapshot.files:
        module = module_name_for(path, snapshot.source_roots) if path.endswith(".py") else None
        if module is None:
            continue
        parent, _, child = module.rpartition(".")
        if parent:
            children.setdefault(parent, set()).add(child)
    return {k: frozenset(v) for k, v in children.items()}


def member_symbol_id(module: str, name: str, submodules: frozenset[str] | set[str]) -> str:
    """Identity of the top-level binding ``name`` of ``module``. When the
    module is a package with a submodule of that name (``pkg/__init__.py``
    defining ``retry`` next to ``pkg/retry.py``) the module keeps
    ``pkg.retry`` and the binding is ``pkg.__init__.retry``."""
    if name in submodules:
        return f"{module}.__init__.{name}"
    return f"{module}.{name}"

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

import subprocess
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


def _staged_config_files(repo: Path) -> dict[str, bytes]:
    out = _git(repo, ["ls-files", "-z", "--cached", "--", *CONFIG_FILES])
    names = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    return read_files(repo, "", names, label=INDEX)


def read_commit_snapshot(
    repo: Path, revision: str, source_roots: list[str], *, with_config: bool = False
) -> Snapshot:
    commit = resolve_commit(repo, revision)
    paths = list_python_files(repo, commit, source_roots)
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
        files=read_files(repo, commit, paths),
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
    config_files = _staged_config_files(repo) if with_config else {}
    return Snapshot(
        info=SnapshotInfo(
            revision=INDEX,
            commit=head,
            kind=KIND_INDEX,
            description=f"git index (staged content) on top of commit {head[:12]}; uncommitted",
        ),
        source_roots=list(source_roots),
        files=read_files(repo, "", paths, label=INDEX),
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

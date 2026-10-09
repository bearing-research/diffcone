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

from diffcone.cython import is_cython
from diffcone.model import (
    KIND_COMMIT,
    KIND_INDEX,
    KIND_WORKTREE,
    AnalysisError,
    SnapshotInfo,
)


class GitError(Exception):
    """Raised when git cannot supply the requested snapshot."""


CONFIG_FILES = (
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "asv.conf.json",
)
# ASV projects usually keep their configuration beside the benchmarks rather
# than at the repository root (numpy and networkx use ``benchmarks/``, pandas
# ``asv_bench/``), and ``benchmark_dir`` is relative to it, so nested copies
# are read too -- shallowest first, and only a few levels down.
ASV_CONFIG = "asv.conf.json"
ASV_CONFIG_DEPTH = 3
NESTED_CONFIGS = (ASV_CONFIG, "pyproject.toml", "setup.cfg")

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
    # The other (non-Python) files under the source roots: path -> git blob
    # id, so a change to one is visible without reading it; and (read only
    # with ``with_config``) the content of the text files among them that
    # pytest could collect as doctests.
    other_files: dict[str, str] = field(default_factory=dict)
    # The content of the Cython sources among them (diffcone.cython).
    cython_files: dict[str, bytes] = field(default_factory=dict)
    text_files: dict[str, bytes] = field(default_factory=dict)
    # Every ``.py`` path in the whole tree, roots or not (read only with
    # ``with_config``): discovery reports test files pytest would collect
    # outside the source roots instead of silently missing them.
    python_paths: tuple[str, ...] = ()

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


def file_id(repo: Path, revision: str, path: str) -> str | None:
    """The git blob id of ``path`` in a snapshot (a revision, ``INDEX`` or
    ``WORKTREE``), or None when it is not there."""
    try:
        if revision == WORKTREE:
            if not (repo / path).is_file():
                return None
            out = _git(repo, ["hash-object", "--", path])
        else:
            spec = f":0:{path}" if revision == INDEX else f"{revision}:{path}"
            out = _git(repo, ["rev-parse", "--verify", "--quiet", spec])
    except GitError:
        return None
    return out.decode().strip() or None


def changed_paths(repo: Path, commit: str, revision: str, kind: str) -> dict[str, str]:
    """Every path whose content differs between ``commit`` and a snapshot
    (``kind``: a commit, the index or the working tree, untracked files
    included), as path -> "added", "deleted" or "edited". ``commit`` may be
    ``INDEX`` when the snapshot is the working tree: the staged content is
    then what it is compared with."""
    if commit == INDEX and kind == KIND_WORKTREE:
        args = ["diff", "--name-status", "-z", "--no-renames"]
    elif kind == KIND_WORKTREE:
        args = ["diff", "--name-status", "-z", "--no-renames", commit]
    elif kind == KIND_INDEX:
        args = ["diff", "--cached", "--name-status", "-z", "--no-renames", commit]
    else:
        args = ["diff", "--name-status", "-z", "--no-renames", commit, revision]
    fields = _git(repo, args).decode("utf-8", "surrogateescape").split("\0")
    out: dict[str, str] = {}
    for status, path in zip(fields[0::2], fields[1::2], strict=False):
        if path:
            out[path] = {"A": "added", "D": "deleted"}.get(status[:1], "edited")
    if kind == KIND_WORKTREE:
        untracked = _git(repo, ["ls-files", "-z", "--others", "--exclude-standard"])
        for raw in untracked.split(b"\0"):
            path = raw.decode("utf-8", "surrogateescape")
            if path and not is_bytecode(path):
                out.setdefault(path, "added")
        # ``git diff`` does not look at a file flagged assume-unchanged or
        # skip-worktree: compare what is on disk with the commit.
        for path, skip_worktree in sorted(_flagged_paths(repo, []).items()):
            full = repo / path
            present = full.is_file() or full.is_symlink()
            if path in out or (skip_worktree and not present):
                continue  # a skip-worktree entry is not checked out by design
            then = file_id(repo, commit, path)
            if not present:
                if then is not None:  # assume-unchanged, deleted on disk
                    out[path] = "deleted"
            elif then is None:
                out[path] = "added"
            elif file_id(repo, WORKTREE, path) != then:
                out[path] = "edited"
    return out


def resolve_commit(repo: Path, revision: str) -> str:
    try:
        out = _git(repo, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
    except GitError:
        out = b""
    commit = out.decode().strip()
    if not commit:
        hint = ""
        if is_shallow(repo):
            hint = (
                "; this clone is shallow and may not have it: fetch more history (git fetch "
                "--deepen=1 for a parent such as HEAD^1, or git fetch origin <branch>; in GitHub "
                "Actions, actions/checkout with fetch-depth: 2 or 0)"
            )
        raise GitError(f"revision {revision!r} does not name a commit in {repo}{hint}")
    return commit


def is_bytecode(path: str) -> bool:
    """Whether ``path`` is compiled bytecode Python writes beside the code
    (``__pycache__``, ``.pyc``): never part of a snapshot, even in a
    repository that does not ignore it."""
    return "__pycache__" in path.rstrip("/").split("/") or path.endswith((".pyc", ".pyo"))


def is_shallow(repo: Path) -> bool:
    """Whether ``repo`` is a shallow clone (its history is cut off)."""
    try:
        return _git(repo, ["rev-parse", "--is-shallow-repository"]).strip() == b"true"
    except GitError:
        return False


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
    directory = directory.strip()
    while directory.startswith("./"):
        directory = directory[2:]
    directory = directory.strip("/")
    directory = "" if directory in ("", ".") else directory
    prefix = prefix.strip() if sep else ""
    if sep and (not prefix or not all(p.isidentifier() for p in prefix.split("."))):
        raise ValueError(f"source root {spec!r}: the module prefix must be a dotted identifier")
    return directory, prefix


def _normalise_root(root: str) -> str:
    return split_root(root)[0]


SYMLINK_MODE = "120000"
# A submodule's entry (a gitlink): its id is the submodule's commit.
GITLINK_MODE = "160000"

# Python files nobody imports that decide what is installed or how the tests
# run (planner.is_build_script): build scripts and build backends' hooks,
# the task runners' files that run the tests (nox, tox's plugin file), and
# the modules the interpreter runs at start when they are on ``sys.path``.
BUILD_SCRIPTS = frozenset(
    {
        "setup.py",
        "hatch_build.py",
        "pdm_build.py",
        "noxfile.py",
        "toxfile.py",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
# ``build.py`` is also an ordinary module name. It is a build script
# (poetry's ``build`` setting) at the repository root or beside a
# ``pyproject.toml``/``setup.cfg``.
PROJECT_BUILD_SCRIPTS = frozenset({"build.py"})


def _other_file(path: str, source_roots: list[str]) -> bool:
    """Whether a file under the roots belongs among ``other_files`` (those
    the index does not read): anything but Python, and a build script no
    module name maps to (``packages/my-api/setup.py``), which would
    otherwise not be seen at all."""
    if not path.endswith(".py"):
        return True
    name = path.rpartition("/")[2]
    return (name in BUILD_SCRIPTS or name in PROJECT_BUILD_SCRIPTS) and module_name_for(
        path, source_roots
    ) is None


def _ls_tree_ids(repo: Path, commit: str, pathspecs: list[str]) -> list[tuple[str, str, str]]:
    """(mode, blob id, path) of every blob under ``pathspecs`` (all when empty)."""
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if pathspecs:
        args += ["--", *pathspecs]
    entries: list[tuple[str, str, str]] = []
    for record in _git(repo, args).split(b"\0"):
        if not record:
            continue
        meta, _, path = record.decode("utf-8", "surrogateescape").partition("\t")
        mode, _, oid = meta.split()
        entries.append((mode, oid, path))
    return entries


def _ls_tree(repo: Path, commit: str, pathspecs: list[str]) -> list[tuple[str, str]]:
    """(mode, path) of every blob under ``pathspecs`` (all when empty)."""
    return [(mode, path) for mode, _, path in _ls_tree_ids(repo, commit, pathspecs)]


def _root_pathspecs(source_roots: list[str]) -> list[str]:
    roots = [_normalise_root(r) for r in source_roots]
    return [] if "" in roots else [r for r in roots if r]


# Suffixes of files pytest's ``--doctest-glob`` commonly collects.
TEXT_DOCTEST_SUFFIXES = (".txt", ".rst", ".md")


def _text_paths(paths: list[str] | tuple[str, ...]) -> list[str]:
    return [p for p in paths if p.endswith(TEXT_DOCTEST_SUFFIXES)]


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
    """Read blobs ``<commit>:<path>``; ``commit=""`` reads the index
    (``:0:<path>``, so a path such as ``1:x`` is not read as a stage).

    ``cat-file --batch`` reads one object name per line, so a path holding a
    line break (or a carriage return, which git strips from a line's end) is
    resolved to its blob id first and asked for by id."""
    if not paths:
        return {}
    label = label or commit
    lines: list[str] = []
    for p in paths:
        spec = f":0:{p}" if commit == "" else f"{commit}:{p}"
        if "\n" in p or "\r" in p:
            try:
                oid = _git(repo, ["rev-parse", "--verify", "--quiet", spec]).decode().strip()
            except GitError:
                oid = ""
            spec = oid or "0" * 40  # an unknown id: cat-file says "missing"
        lines.append(spec)
    request = "".join(f"{line}\n" for line in lines).encode("utf-8", "surrogateescape")
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


def _ls_files_staged_ids(repo: Path, source_roots: list[str]) -> dict[str, tuple[str, str]]:
    """Stage-0 index entries under the roots: path -> (mode, blob id)."""
    out = _git(repo, ["ls-files", "-z", "--stage", *_pathspec(source_roots)])
    entries: dict[str, tuple[str, str]] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.decode("utf-8", "surrogateescape").partition("\t")
        mode, oid, stage = meta.split()
        if stage == "0":
            entries[path] = (mode, oid)
    return entries


def _ls_files_staged(repo: Path, source_roots: list[str]) -> dict[str, str]:
    """Stage-0 index entries under the roots: path -> mode."""
    return {path: mode for path, (mode, _) in _ls_files_staged_ids(repo, source_roots).items()}


def _flagged_paths(repo: Path, pathspec: list[str]) -> dict[str, bool]:
    """Tracked paths flagged assume-unchanged or skip-worktree, each with
    whether it is skip-worktree. git trusts the flag instead of looking at
    the file, so ``ls-files -m`` and ``git diff`` say nothing about an edit
    to one; the file on disk is what runs, and only hashing it tells.
    (``ls-files -v`` tags an assume-unchanged entry in lower case, a
    skip-worktree one ``S``.)"""
    out = _git(repo, ["ls-files", "-z", "-v", "--cached", *pathspec])
    flagged: dict[str, bool] = {}
    for record in out.split(b"\0"):
        tag, _, path = record.decode("utf-8", "surrogateescape").partition(" ")
        if path and (tag.islower() or tag.upper() == TAG_SKIP_WORKTREE):
            flagged[path] = flagged.get(path, False) or tag.upper() == TAG_SKIP_WORKTREE
    return flagged


def _submodule_id(repo: Path, path: str, recorded: str) -> str:
    """What a submodule's gitlink would record if added now: the commit
    checked out in it, as a commit or the index records it (``recorded``
    when it is not checked out, as git itself treats it). Uncommitted edits
    inside it change what runs without changing that commit, so they make
    the id differ from every commit's (``<commit>-dirty``)."""
    full = repo / path
    if not (full / ".git").exists():
        return recorded
    try:
        head = _git(full, ["rev-parse", "--verify", "--quiet", "HEAD"]).decode().strip()
        dirty = _git(full, ["status", "--porcelain", "--untracked-files=normal"]).strip()
    except GitError:
        return recorded + "-unreadable"
    if not head:
        return recorded + "-unreadable"
    return head + ("-dirty" if dirty else "")


def _worktree_blob_ids(repo: Path, paths: list[str], source_roots: list[str]) -> dict[str, str]:
    """The git blob id each of ``paths`` would have if added now. A file git
    reports unmodified has its staged id; a modified or untracked one, or
    one flagged assume-unchanged or skip-worktree that is on disk (git does
    not look at those), is hashed by ``git hash-object``, which applies the
    same filters (line endings, clean filters) and object format as ``git
    add`` would, so equal content gives the id a commit has. A symbolic link
    is the id of its target path, as git stores it."""
    staged = _ls_files_staged_ids(repo, source_roots)
    listed = _git(
        repo, ["ls-files", "-z", "-m", "--others", "--exclude-standard", *_pathspec(source_roots)]
    )
    dirty = {p.decode("utf-8", "surrogateescape") for p in listed.split(b"\0") if p}
    dirty.update(p for p in _flagged_paths(repo, _pathspec(source_roots)) if (repo / p).is_file())
    ids: dict[str, str] = {}
    to_hash: list[str] = []
    for path in paths:
        full = repo / path
        if staged.get(path, ("", ""))[0] == GITLINK_MODE:
            ids[path] = _submodule_id(repo, path, staged[path][1])
        elif full.is_symlink():
            target = os.readlink(full).encode("utf-8", "surrogateescape")
            ids[path] = _git(repo, ["hash-object", "--stdin"], stdin=target).decode().strip()
        elif path in staged and path not in dirty:
            ids[path] = staged[path][1]
        elif "\n" in path:  # ``--stdin-paths`` is line-based
            args = ["hash-object", "--stdin", f"--path={path}"]
            ids[path] = _git(repo, args, stdin=full.read_bytes()).decode().strip()
        else:
            to_hash.append(path)
    if to_hash:
        request = "\n".join(to_hash).encode("utf-8", "surrogateescape") + b"\n"
        out = _git(repo, ["hash-object", "--stdin-paths"], stdin=request).decode().split()
        ids.update(zip(to_hash, out, strict=True))
    return ids


def _listed(listing: bytes) -> list[str]:
    """The paths of a NUL-separated git listing (``-z``: names are neither
    quoted nor split at a line break in them)."""
    return [raw.decode("utf-8", "surrogateescape") for raw in listing.split(b"\0") if raw]


def _nested_configs(listing: bytes) -> list[str]:
    """Paths of ``asv.conf.json``, and of the package metadata a sibling
    package declares its pytest plugins in (``pyproject.toml``,
    ``setup.cfg``), below the root in a NUL-separated file listing,
    shallowest first and no deeper than ASV_CONFIG_DEPTH."""
    found = []
    for path in _listed(listing):
        parts = path.split("/")
        if len(parts) > 1 and parts[-1] in NESTED_CONFIGS and len(parts) <= ASV_CONFIG_DEPTH:
            found.append(path)
    return sorted(found, key=lambda p: (p.count("/"), p))


def _python_paths(listing: bytes) -> tuple[str, ...]:
    """The ``.py`` paths in a NUL-separated file listing, sorted."""
    return tuple(sorted(p for p in _listed(listing) if p.endswith(".py")))


def _staged_config_files(repo: Path) -> dict[str, bytes]:
    out = _git(repo, ["ls-files", "-z", "--cached", "--", *CONFIG_FILES])
    names = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    return read_files(repo, "", names, label=INDEX)


def commit_description(commit: str, revision: str) -> str:
    """How a report describes a committed snapshot."""
    return f"commit {commit[:12]} ({revision})"


def read_commit_snapshot(
    repo: Path, revision: str, source_roots: list[str], *, with_config: bool = False
) -> Snapshot:
    commit = resolve_commit(repo, revision)
    entries_ids = _ls_tree_ids(repo, commit, _root_pathspecs(source_roots))
    entries = [(m, p) for m, _, p in entries_ids]
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
    python_paths: tuple[str, ...] = ()
    if with_config:
        root = list_root_files(repo, commit)
        names = [n for n in CONFIG_FILES if n in root]
        whole_tree = _git(repo, ["ls-tree", "-r", "-z", "--name-only", commit])
        nested = _nested_configs(whole_tree)
        config_files = read_files(repo, commit, names + nested)
        python_paths = _python_paths(whole_tree)
    return Snapshot(
        info=SnapshotInfo(
            revision=revision,
            commit=commit,
            kind=KIND_COMMIT,
            description=commit_description(commit, revision),
        ),
        source_roots=list(source_roots),
        files=dict(sorted(files.items())),
        config_files=config_files,
        python_paths=python_paths,
        other_files={
            p: oid
            for _, oid, p in sorted(entries_ids, key=lambda e: e[2])
            if _other_file(p, source_roots)
        },
        cython_files=read_files(
            repo, commit, sorted(p for m, p in entries if is_cython(p) and m != SYMLINK_MODE)
        ),
        text_files=read_files(
            repo, commit, _text_paths([p for m, p in entries if m != SYMLINK_MODE])
        )
        if with_config
        else {},
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
    python_paths: tuple[str, ...] = ()
    if with_config:
        listing = _git(repo, ["ls-files", "-z", "--cached"])
        nested = _nested_configs(listing)
        config_files.update(read_files(repo, "", nested, label=INDEX))
        python_paths = _python_paths(listing)
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
        python_paths=python_paths,
        errors=errors,
        other_files={
            p: oid
            for p, (_, oid) in sorted(_ls_files_staged_ids(repo, source_roots).items())
            if _other_file(p, source_roots)
        },
        cython_files=read_files(
            repo,
            "",
            sorted(p for p, m in staged.items() if is_cython(p) and m != SYMLINK_MODE),
            label=INDEX,
        ),
        text_files=read_files(
            repo, "", _text_paths([p for p, m in staged.items() if m != SYMLINK_MODE]), label=INDEX
        )
        if with_config
        else {},
    )


def read_worktree_snapshot(
    repo: Path, source_roots: list[str], *, with_config: bool = False
) -> Snapshot:
    """Files on disk: tracked and untracked, minus ignored ones.

    Skip-worktree entries (sparse checkouts) are not on disk by design and
    are read from the index instead of being treated as deletions; when one
    is on disk, as an assume-unchanged entry is, the file on disk is read
    (git does not look at either). A submodule is listed with the commit
    checked out in it, as a commit or the index lists its gitlink.
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
    python_paths: tuple[str, ...] = ()
    if with_config:
        for name in CONFIG_FILES:
            full = repo / name
            if full.is_file():
                config_files[name] = full.read_bytes()
        listing = _git(repo, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
        for name in _nested_configs(listing):
            full = repo / name
            if full.is_file():
                config_files[name] = full.read_bytes()
        python_paths = tuple(p for p in _python_paths(listing) if (repo / p).is_file())
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
        python_paths=python_paths,
        other_files=_worktree_blob_ids(
            repo,
            sorted(
                {
                    p
                    for tag, p in listed
                    if _other_file(p, source_roots)
                    and not (tag == TAG_OTHER and is_bytecode(p))
                    and (
                        (repo / p).is_file()
                        or (repo / p).is_symlink()
                        or tag == TAG_SKIP_WORKTREE  # not on disk by design: the staged id
                        # A tracked directory is a submodule (a gitlink), which
                        # commits and the index list too.
                        or (tag != TAG_OTHER and (repo / p).is_dir())
                    )
                }
            ),
            source_roots,
        ),
        cython_files={
            p: (repo / p).read_bytes()
            for p in sorted({p for _, p in listed if is_cython(p)})
            if (repo / p).is_file() and not (repo / p).is_symlink()
        },
        text_files={
            p: (repo / p).read_bytes()
            for p in _text_paths(sorted({p for _, p in listed}))
            if (repo / p).is_file()
        }
        if with_config
        else {},
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
    (whether or not they parse): what a package binding can shadow.
    Computed once per snapshot: discovery parses modules one at a time and
    asks for every one (about 100 times a plan on pandas)."""
    cached = snapshot.__dict__.get("_child_modules")
    if cached is not None:
        return cached
    children: dict[str, set[str]] = {}
    for path in snapshot.files:
        module = module_name_for(path, snapshot.source_roots) if path.endswith(".py") else None
        if module is None:
            continue
        parent, _, child = module.rpartition(".")
        if parent:
            children.setdefault(parent, set()).add(child)
    result = {k: frozenset(v) for k, v in children.items()}
    snapshot.__dict__["_child_modules"] = result
    return result


def member_symbol_id(module: str, name: str, submodules: frozenset[str] | set[str]) -> str:
    """Identity of the top-level binding ``name`` of ``module``. When the
    module is a package with a submodule of that name (``pkg/__init__.py``
    defining ``retry`` next to ``pkg/retry.py``) the module keeps
    ``pkg.retry`` and the binding is ``pkg.__init__.retry``."""
    if name in submodules:
        return f"{module}.__init__.{name}"
    return f"{module}.{name}"

"""Independently collect Git metadata; never store file contents or command output.

Working-tree bytes are read only to hash them. Git never reads the working tree
on the observer's behalf, so no repository-configured filter can run.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

from ._signals import ObservationRejected
from .models import SUSPICIOUS, safe_strings

# The observed repository's own config and refs are controlled by the observed
# actor. Command-line options outrank them: never run its fsmonitor hook, which
# executes arbitrary programs and can report which paths git treats as unchanged,
# and never honour refs/replace/, which substitutes one object for another, so a
# replaced baseline or HEAD commit could make committed or staged changes vanish.
# Staged and committed deltas compare raw index/tree entries rather than asking
# Git's diff policy, so submodule ignore settings cannot drop a changed gitlink.
# Case-insensitive matching (core.ignoreCase) would hide a new file whose name
# differs only in case from a tracked path or an ignore rule; on a
# case-insensitive filesystem, turning it off over-reports case-only renames
# as untracked instead.
HARDENED_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.ignoreCase=false",
    "--no-replace-objects",
)


def environment() -> dict[str, str]:
    # Inherited GIT_* variables can redirect -C to another repository or index.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # Observation must not write the observed repository's index.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(
    repo: Path, *args: str, work_tree: Path | None = None
) -> bytes:
    command = ["git", *HARDENED_CONFIG, "-C", str(repo)]
    if work_tree is not None:
        # Command-line --work-tree outranks an observed repository's core.worktree.
        command.append(f"--work-tree={work_tree}")
    result = subprocess.run(
        [*command, *args],
        capture_output=True,
        check=False,
        env=environment(),
        timeout=15,
    )
    if result.returncode:
        raise ValueError("Git metadata collection failed")
    return result.stdout


def paths(
    repo: Path, *args: str, work_tree: Path | None = None
) -> list[str]:
    return sorted(
        {
            p.decode("utf-8", "surrogateescape")
            for p in git(repo, *args, work_tree=work_tree).split(b"\0")
            if p
        }
    )


def repository_root(repo: Path) -> Path:
    """Find the nearest filesystem worktree root without trusting core.worktree."""

    current = repo
    while True:
        marker = current / ".git"
        try:
            # Like Git, follow a symlinked .git (some tools link it to a shared
            # store). This grants nothing: a .git file can point anywhere too.
            mode = os.stat(marker).st_mode
        except FileNotFoundError:
            if os.path.lexists(marker):
                raise ValueError("Git metadata collection failed") from None
        else:
            if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                return current
            raise ValueError("Git metadata collection failed")
        parent = current.parent
        if parent == current:
            raise ValueError("Git metadata collection failed")
        current = parent


def blob_id(algorithm: str, chunks, size: int) -> str | None:
    """Git's object ID for a blob with these raw bytes; None if the size moved."""
    digest = hashlib.new(algorithm)
    digest.update(b"blob %d\0" % size)
    seen = 0
    for chunk in chunks:
        seen += len(chunk)
        digest.update(chunk)
    return digest.hexdigest() if seen == size else None


# Opening working-tree paths must never follow a symlink or block: the observed
# actor controls the tree and could otherwise point the observer outside the
# repository or at a FIFO.
OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
ABSENT = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)


class Worktree:
    """Working-tree access relative to the root, one directory handle at a time.

    Like Git, a leading path component that is a symlink or not a directory
    means the entry is absent: it is never followed.
    """

    def __init__(self, root: Path):
        self.handles = {b"": os.open(root, OPEN_DIRECTORY)}

    def directory(self, parent: bytes) -> int | None:
        # `ls-files` lists paths sorted, so only the current path's ancestors are
        # needed again; keeping every directory open would exhaust descriptors
        # on repositories with many directories.
        stale = [
            key
            for key in self.handles
            if key and parent != key and not parent.startswith(key + b"/")
        ]
        for key in stale:
            handle = self.handles.pop(key)
            if handle is not None:
                os.close(handle)
        return self.open(parent)

    def open(self, parent: bytes) -> int | None:
        if parent not in self.handles:
            head, _, name = parent.rpartition(b"/")
            base = self.open(head)
            handle = None
            if base is not None:
                try:
                    handle = os.open(name, OPEN_DIRECTORY, dir_fd=base)
                except OSError as error:
                    if error.errno not in ABSENT:
                        raise
            self.handles[parent] = handle
        return self.handles[parent]

    def close(self):
        for handle in self.handles.values():
            if handle is not None:
                os.close(handle)


def read_chunks(handle: int):
    while chunk := os.read(handle, 1 << 20):
        yield chunk


def entry_changed(
    tree: Worktree,
    root: Path,
    path: bytes,
    mode: int,
    oid: str,
    algorithm: str,
    sparse: bool,
) -> bool:
    """Compare one stage-0 index entry with the working tree, without Git filters.

    This is raw-byte equality: content that differs only through a legitimate
    clean filter (for example LFS or line-ending conversion) counts as changed,
    and assume-unchanged flags and core.fileMode are ignored rather than trusted:
    a filesystem without executable bits over-reports mode changes instead of
    letting the observed repository switch the check off. A skip-worktree
    (sparse) entry that is absent is expected; one that is present is compared,
    so the flag cannot hide an edit.
    """
    parent, _, name = path.rpartition(b"/")
    base = tree.directory(parent)
    if base is None:
        return not sparse
    try:
        info = os.stat(name, dir_fd=base, follow_symlinks=False)
    except OSError as error:
        if error.errno in ABSENT:
            return not sparse
        raise
    kind = mode & 0o170000
    if kind == 0o160000:
        # A submodule counts as changed only when a different commit is checked
        # out; its own working tree is not inspected.
        location = os.fsencode(root) + b"/" + path
        if not stat.S_ISDIR(info.st_mode) or not os.path.lexists(location + b"/.git"):
            return False
        try:
            checked_out = git(
                Path(os.fsdecode(location)), "rev-parse", "--verify", "HEAD"
            )
        except ValueError:
            return True  # for example an empty repository with no commit
        return checked_out.decode().strip() != oid
    if kind == 0o120000:
        if not stat.S_ISLNK(info.st_mode):
            return True
        target = os.readlink(name, dir_fd=base)
        return blob_id(algorithm, [target], len(target)) != oid
    if not stat.S_ISREG(info.st_mode):
        return True
    try:
        handle = os.open(name, OPEN_FILE, dir_fd=base)
    except OSError as error:
        if error.errno in ABSENT:
            return True  # replaced since the stat
        raise
    try:
        opened = os.fstat(handle)
        if not stat.S_ISREG(opened.st_mode):
            return True
        if bool(opened.st_mode & 0o100) != bool(mode & 0o100):
            return True
        return blob_id(algorithm, read_chunks(handle), opened.st_size) != oid
    finally:
        os.close(handle)


def unstaged_paths(root: Path) -> list[str]:
    """Tracked paths whose working tree differs from the index.

    `git diff` would compare content through the repository's clean filters,
    which are programs the observed actor can configure; hash raw bytes instead.
    """
    algorithm = (
        git(root, "rev-parse", "--show-object-format", work_tree=root).decode().strip()
    )
    if algorithm not in ("sha1", "sha256"):
        raise ValueError("unsupported Git object format")
    changed = set()
    tree = Worktree(root)
    try:
        # -t prefixes each entry with a status tag; "S" marks skip-worktree.
        for record in git(
            root, "ls-files", "--stage", "-t", "-z", work_tree=root
        ).split(b"\0"):
            if not record:
                continue
            meta, path = record.split(b"\t", 1)
            tag, mode, oid, stage = meta.split(b" ")
            if stage != b"0" or entry_changed(
                tree,
                root,
                path,
                int(mode, 8),
                oid.decode(),
                algorithm,
                sparse=tag == b"S",
            ):
                changed.add(path.decode("utf-8", "surrogateescape"))
    finally:
        tree.close()
    return sorted(changed)


def tree_entries(root: Path, commit: str) -> dict[str, tuple[bytes, bytes]]:
    """Raw path -> (mode, object ID) for one commit tree."""

    entries = {}
    for record in git(
        root, "ls-tree", "-r", "--full-tree", "-z", commit, work_tree=root
    ).split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, _kind, oid = meta.split(b" ")
        entries[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    return entries


def intent_to_add(root: Path, head: str) -> set[str]:
    """Paths recorded with `git add -N`: in the index, but nothing staged yet.

    `ls-files --stage` lists them as ordinary empty blobs, so compare HEAD with
    the index twice: `--ita-invisible-in-index` changes only how these entries
    are reported. Neither run reads the working tree, so no filter runs.
    """

    def delta(*options: str) -> set[bytes]:
        output = git(
            root,
            "diff-index",
            "--cached",
            "--no-renames",
            "--name-status",
            "-z",
            *options,
            head,
            "--",
            work_tree=root,
        ).split(b"\0")
        return {status + b"\0" + path for status, path in zip(output[::2], output[1::2])}

    return {
        record.split(b"\0", 1)[1].decode("utf-8", "surrogateescape")
        for record in delta("--ita-visible-in-index")
        ^ delta("--ita-invisible-in-index")
    }


def index_entries(
    root: Path, head: str
) -> tuple[dict[str, tuple[bytes, bytes]], set[str]]:
    """Raw stage-0 index entries plus paths with unresolved stages.

    Intent-to-add (`git add -N`) entries are left out, as in Git: nothing is
    staged, and the unstaged comparison reports the path against the placeholder
    empty blob.
    """

    placeholders = intent_to_add(root, head)
    entries = {}
    unmerged = set()
    for record in git(root, "ls-files", "--stage", "-z", work_tree=root).split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, oid, stage = meta.split(b" ")
        name = path.decode("utf-8", "surrogateescape")
        if stage != b"0":
            unmerged.add(name)
        elif name not in placeholders:
            entries[name] = (mode, oid)
    return entries, unmerged


def entry_delta(
    left: dict[str, tuple[bytes, bytes]],
    right: dict[str, tuple[bytes, bytes]],
    *,
    always: set[str] | None = None,
) -> list[str]:
    """Paths added, deleted, mode-changed, object-changed or explicitly unresolved."""

    candidates = set(left) | set(right) | (always or set())
    return sorted(
        path
        for path in candidates
        if path in (always or set()) or left.get(path) != right.get(path)
    )


def untracked_paths(root: Path) -> list[str]:
    """Untracked paths, hidden only by ignore rules that are themselves visible.

    `.gitignore` files live in the working tree, so editing one shows up as an
    unstaged or untracked change. `.git/info/exclude` and `core.excludesFile` do
    not, so they are never applied. Untracked `.gitignore` files are listed even
    when they ignore themselves, so a new one cannot hide its directory silently.
    """
    return sorted(
        set(
            paths(
                root,
                "ls-files",
                "--others",
                "--exclude-per-directory=.gitignore",
                "-z",
                work_tree=root,
            )
        )
        | set(
            paths(
                root,
                "ls-files",
                "--others",
                "-z",
                "--",
                ":(glob)**/.gitignore",
                work_tree=root,
            )
        )
    )


def index_fingerprint(root: Path) -> str | None:
    """SHA-256 of the index file, or None when there is none yet.

    Staged and unstaged changes come from separate reads of the index; if it
    changed in between, a change staged in that window would be in neither.
    """
    location = git(root, "rev-parse", "--git-path", "index", work_tree=root)
    try:
        handle = os.open(root / os.fsdecode(location.rstrip(b"\n")), OPEN_FILE)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise ValueError("Git index is not a regular file")
        digest = hashlib.sha256()
        for chunk in read_chunks(handle):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(handle)


# Stands in for a path or branch name that looks like a credential. Rejecting the
# whole snapshot instead would let the observed actor blind the observer with one
# file named, say, `token=1`. No hash is kept: a short secret would be guessable.
REDACTED = "[redacted: credential-shaped name]"


def redact(result: dict) -> dict:
    """Replace credential-shaped names; keep one marker per replaced entry."""

    def name(value: str) -> str:
        return REDACTED if SUSPICIOUS.search(value) else value

    return {
        key: (
            sorted(name(item) for item in value)
            if isinstance(value, list)
            else name(value)
            if isinstance(value, str)
            else value
        )
        for key, value in result.items()
    }


def collect_git(repo: str | Path, baseline: str | None = None) -> dict:
    location = Path(repo).expanduser().resolve()
    root = repository_root(location)
    index = index_fingerprint(root)
    head = (
        git(root, "rev-parse", "--verify", "HEAD^{commit}", work_tree=root)
        .decode()
        .strip()
    )
    before = None
    if baseline is not None:
        # Full object IDs only: no options, pathspecs, or moving symbolic refs.
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", baseline):
            raise ValueError("baseline must be a full commit object ID")
        before = (
            git(
                root,
                "rev-parse",
                "--verify",
                baseline + "^{commit}",
                work_tree=root,
            )
            .decode()
            .strip()
        )
    head_entries = tree_entries(root, head)
    indexed, unmerged = index_entries(root, head)
    staged = entry_delta(head_entries, indexed, always=unmerged)
    unstaged = unstaged_paths(root)
    result = {
        "commit_before": before,
        "commit_after": head,
        "branch": git(root, "branch", "--show-current", work_tree=root).decode().strip(),
        "committed_delta": entry_delta(tree_entries(root, before), head_entries)
        if before
        else None,
        "staged_delta": staged,
        "unstaged_delta": unstaged,
        "working_tree_delta": sorted(set(staged) | set(unstaged)),
        "untracked_files": untracked_paths(root),
    }
    if git(root, "rev-parse", "HEAD", work_tree=root).decode().strip() != head:
        raise ValueError("Git HEAD changed during capture; retry")
    if index_fingerprint(root) != index:
        raise ValueError("Git index changed during capture; retry")
    result = redact(result)
    try:
        safe_strings(result)
    except ValueError:
        # Unreachable after redaction; kept so nothing credential-shaped is stored.
        raise ObservationRejected("sensitive-shaped Git metadata") from None
    return result

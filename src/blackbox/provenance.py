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
from .models import safe_strings

# The observed repository's own config and refs are controlled by the observed
# actor. Command-line options outrank them: never run its fsmonitor hook, which
# executes arbitrary programs and can report which paths git treats as unchanged,
# and never honour refs/replace/, which substitutes one object for another, so a
# replaced baseline or HEAD commit could make committed or staged changes vanish.
HARDENED_CONFIG = ("-c", "core.fsmonitor=false", "--no-replace-objects")


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
    filemode: bool,
    sparse: bool,
) -> bool:
    """Compare one stage-0 index entry with the working tree, without Git filters.

    This is raw-byte equality: content that differs only through a legitimate
    clean filter (for example LFS or line-ending conversion) counts as changed,
    and assume-unchanged flags are ignored rather than trusted. A skip-worktree
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
        if filemode and bool(opened.st_mode & 0o100) != bool(mode & 0o100):
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
    filemode = (
        git(
            root,
            "config",
            "--type=bool",
            "--default=true",
            "--get",
            "core.fileMode",
            work_tree=root,
        )
        .decode()
        .strip()
        == "true"
    )
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
                filemode,
                sparse=tag == b"S",
            ):
                changed.add(path.decode("utf-8", "surrogateescape"))
    finally:
        tree.close()
    return sorted(changed)


def collect_git(repo: str | Path, baseline: str | None = None) -> dict:
    location = Path(repo).expanduser().resolve()
    root = repository_root(location)
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
    staged = paths(
        root,
        "diff",
        "--cached",
        "--name-only",
        "--no-renames",
        "-z",
        head,
        "--",
        work_tree=root,
    )
    unstaged = unstaged_paths(root)
    result = {
        "commit_before": before,
        "commit_after": head,
        "branch": git(root, "branch", "--show-current", work_tree=root).decode().strip(),
        "committed_delta": paths(
            root,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            before,
            head,
            "--",
            work_tree=root,
        )
        if before
        else None,
        "staged_delta": staged,
        "unstaged_delta": unstaged,
        "working_tree_delta": sorted(set(staged) | set(unstaged)),
        "untracked_files": paths(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            work_tree=root,
        ),
    }
    if git(root, "rev-parse", "HEAD", work_tree=root).decode().strip() != head:
        raise ValueError("Git HEAD changed during capture; retry")
    try:
        safe_strings(result)
    except ValueError:
        # Deterministic until the repository is changed: not a transient failure.
        raise ObservationRejected("sensitive-shaped Git metadata") from None
    return result

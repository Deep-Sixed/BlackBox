"""Independently collect Git metadata; never store file contents or command output.

Working-tree bytes are read only to hash them. Git never reads the working tree
on the observer's behalf, so no repository-configured filter can run.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

from ._signals import ObservationRejected
from .models import safe_strings

# The observed repository's own config is controlled by the observed actor.
# Command-line config outranks it: never run its fsmonitor hook, which executes
# arbitrary programs and can report which paths git treats as unchanged.
HARDENED_CONFIG = ("-c", "core.fsmonitor=false")


def environment() -> dict[str, str]:
    # Inherited GIT_* variables can redirect -C to another repository or index.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # Observation must not write the observed repository's index.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *HARDENED_CONFIG, "-C", str(repo), *args],
        capture_output=True,
        check=False,
        env=environment(),
        timeout=15,
    )
    if result.returncode:
        raise ValueError("Git metadata collection failed")
    return result.stdout


def paths(repo: Path, *args: str) -> list[str]:
    return sorted(
        {
            p.decode("utf-8", "surrogateescape")
            for p in git(repo, *args).split(b"\0")
            if p
        }
    )


def blob_id(algorithm: str, chunks, size: int) -> str | None:
    """Git's object ID for a blob with these raw bytes; None if the size moved."""
    digest = hashlib.new(algorithm)
    digest.update(b"blob %d\0" % size)
    seen = 0
    for chunk in chunks:
        seen += len(chunk)
        digest.update(chunk)
    return digest.hexdigest() if seen == size else None


def read_chunks(path: bytes):
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            yield chunk


def entry_changed(
    root: Path, path: bytes, mode: int, oid: str, algorithm: str, filemode: bool
) -> bool:
    """Compare one stage-0 index entry with the working tree, without Git filters.

    This is raw-byte equality: content that differs only through a legitimate
    clean filter (for example LFS or line-ending conversion) counts as changed,
    and assume-unchanged flags are ignored rather than trusted.
    """
    location = os.fsencode(root) + b"/" + path
    try:
        info = os.lstat(location)
    except FileNotFoundError, NotADirectoryError:
        return True
    kind = mode & 0o170000
    if kind == 0o160000:
        # A submodule counts as changed only when a different commit is checked
        # out; its own working tree is not inspected.
        if not stat.S_ISDIR(info.st_mode) or not os.path.lexists(location + b"/.git"):
            return False
        checked_out = git(Path(os.fsdecode(location)), "rev-parse", "--verify", "HEAD")
        return checked_out.decode().strip() != oid
    if kind == 0o120000:
        if not stat.S_ISLNK(info.st_mode):
            return True
        target = os.readlink(location)
        return blob_id(algorithm, [target], len(target)) != oid
    if not stat.S_ISREG(info.st_mode):
        return True
    if filemode and bool(info.st_mode & 0o100) != bool(mode & 0o100):
        return True
    return blob_id(algorithm, read_chunks(location), info.st_size) != oid


def unstaged_paths(root: Path) -> list[str]:
    """Tracked paths whose working tree differs from the index.

    `git diff` would compare content through the repository's clean filters,
    which are programs the observed actor can configure; hash raw bytes instead.
    """
    algorithm = git(root, "rev-parse", "--show-object-format").decode().strip()
    if algorithm not in ("sha1", "sha256"):
        raise ValueError("unsupported Git object format")
    filemode = (
        git(root, "config", "--type=bool", "--default=true", "--get", "core.fileMode")
        .decode()
        .strip()
        == "true"
    )
    changed = set()
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, oid, stage = meta.split(b" ")
        if stage != b"0" or entry_changed(
            root, path, int(mode, 8), oid.decode(), algorithm, filemode
        ):
            changed.add(path.decode("utf-8", "surrogateescape"))
    return sorted(changed)


def collect_git(repo: str | Path, baseline: str | None = None) -> dict:
    root = Path(repo).expanduser().resolve()
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    before = None
    if baseline is not None:
        # Full object IDs only: no options, pathspecs, or moving symbolic refs.
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", baseline):
            raise ValueError("baseline must be a full commit object ID")
        before = (
            git(root, "rev-parse", "--verify", baseline + "^{commit}").decode().strip()
        )
    staged = paths(
        root, "diff", "--cached", "--name-only", "--no-renames", "-z", head, "--"
    )
    unstaged = unstaged_paths(root)
    result = {
        "commit_before": before,
        "commit_after": head,
        "branch": git(root, "branch", "--show-current").decode().strip(),
        "committed_delta": paths(
            root, "diff", "--name-only", "--no-renames", "-z", before, head, "--"
        )
        if before
        else None,
        "staged_delta": staged,
        "unstaged_delta": unstaged,
        "working_tree_delta": sorted(set(staged) | set(unstaged)),
        "untracked_files": paths(
            root, "ls-files", "--others", "--exclude-standard", "-z"
        ),
    }
    if git(root, "rev-parse", "HEAD").decode().strip() != head:
        raise ValueError("Git HEAD changed during capture; retry")
    try:
        safe_strings(result)
    except ValueError:
        # Deterministic until the repository is changed: not a transient failure.
        raise ObservationRejected("sensitive-shaped Git metadata") from None
    return result

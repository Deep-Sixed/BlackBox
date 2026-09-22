"""Independently collect Git metadata; never read file contents or command output."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import safe_strings


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
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


def collect_git(repo: str | Path, baseline: str | None = None) -> dict:
    root = Path(repo).expanduser().resolve()
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    before = None
    if baseline is not None:
        # Full object IDs only: no options, pathspecs, or moving symbolic refs.
        import re

        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", baseline):
            raise ValueError("baseline must be a full commit object ID")
        before = (
            git(root, "rev-parse", "--verify", baseline + "^{commit}").decode().strip()
        )
    staged = paths(
        root, "diff", "--cached", "--name-only", "--no-renames", "-z", head, "--"
    )
    unstaged = paths(root, "diff", "--name-only", "--no-renames", "-z", "--")
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
    safe_strings(result)
    return result

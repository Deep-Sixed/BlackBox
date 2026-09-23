"""Independently collect Git metadata; never read file contents or command output."""

from __future__ import annotations

import os
import re
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
    try:
        safe_strings(result)
    except ValueError:
        # Deterministic until the repository is changed: not a transient failure.
        raise ObservationRejected("sensitive-shaped Git metadata") from None
    return result

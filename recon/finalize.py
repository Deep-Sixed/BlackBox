"""Finalize one observed session into full Recon artifacts.

Runs the same git evidence commands the original Snitch-era finalizer recorded
(rev-parse, branch, status, diff --stat), wraps each as a self-verifying
evidence receipt, and writes the four Recon artifacts: canonical JSON
record, sha256 digest, recon_<session>.md audit, request reservation.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from . import config
from .session_record import (
    RequestIdCollisionError,
    SessionRecordError,
    build_record,
    make_evidence_receipt,
    write_session_artifacts,
)

STDOUT_LIMIT = 4096

EVIDENCE_COMMANDS = (
    ("rev-parse", "--show-toplevel"),
    ("rev-parse", "HEAD"),
    ("branch", "--show-current"),
    ("status", "--porcelain=v1", "--untracked-files=all"),
    ("diff", "--stat", "--"),
)


def derive_request_id(surface: str, session_id: str) -> str:
    """Deterministic per-session request ID so repeat stop events dedup."""
    material = f"recon:{surface}:{session_id}".encode("utf-8")
    return "req_" + hashlib.sha256(material).hexdigest()[:32]


def collect_git_receipts(repo: Path) -> list[dict[str, Any]]:
    receipts = []
    for args in EVIDENCE_COMMANDS:
        command = ["git", "-C", str(repo), *args]
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        receipts.append(
            make_evidence_receipt(
                "git",
                {
                    "command": " ".join(command),
                    "exit_code": result.returncode,
                    "stdout": result.stdout.rstrip()[:STDOUT_LIMIT],
                    "stderr": result.stderr.rstrip()[:STDOUT_LIMIT],
                },
            )
        )
    return receipts


def finalize_session(
    *,
    surface: str,
    session_id: str,
    cwd: str,
    model_or_tool: str = "default",
) -> dict[str, Any]:
    """Build and persist one Recon session record. Returns a status dict."""
    repo = Path(cwd).expanduser()
    if not (repo / ".git").exists():
        probe = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if probe.returncode != 0:
            return {"status": "skipped", "reason": "cwd is not a git repository"}
        repo = Path(probe.stdout.strip())

    claims = {
        "session_id": session_id,
        "request_id": derive_request_id(surface, session_id),
        "agent": surface,
        "model_or_tool": model_or_tool,
        "commands_claimed": [],
        "tests_claimed": [],
        "artifacts_written": [],
        "failures": [],
        "blockers": [],
        "deferred_work": [],
        "risk_flags": [],
    }
    evidence = {
        "commands_verified": collect_git_receipts(repo),
        "tests_verified": [],
        "database_writes": [],
    }

    try:
        record = build_record(claims, repo=repo, evidence=evidence)
        result = write_session_artifacts(
            record,
            records_dir=config.records_dir(),
            audit_dir=config.audit_dir(),
            reservations_dir=config.reservations_dir(),
        )
    except RequestIdCollisionError:
        return {"status": "duplicate", "request_id": claims["request_id"]}
    except SessionRecordError as exc:
        return {"status": "error", "reason": str(exc)}

    _grant_reader_access(Path(result["audit"]))
    return {"status": "finalized", **result}


READER_IDENTITY = "agentsync-svc"


def _grant_reader_access(audit_path: Path) -> None:
    """Additive named-user ACL so the Recall composer (ledger runtime,
    agentsync-svc) can read session reports the engine writes 0600.
    Best-effort: recording must never fail on an ACL error."""
    for target, mode in ((audit_path.parent, "rx"), (audit_path, "r")):
        subprocess.run(
            ["setfacl", "-m", f"u:{READER_IDENTITY}:{mode}", str(target)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

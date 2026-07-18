"""Normalized, redacted, append-only Recon session records.

Engine lineage: Project Snitch (third-party/GitHubs/Recon), restored
verbatim 2026-07-16 then rebranded the same day. Only the user-facing
report heading ("Recon Session") and the audit filename prefix
(recon_<session>.md) differ from the original; ``snitch_`` survives
solely as a read-side legacy alias for pre-rename artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any


SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
UUID_V4_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
REQUEST_HEX_PATTERN = re.compile(r"^req_[0-9a-fA-F]{16,64}$")
HASH_PATTERN = re.compile(r"^[a-f0-9]{40,64}$")
RECEIPT_HASH_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
SENSITIVE_KEY_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "database_url",
    "password",
    "secret",
    "session_cookie",
    "token",
}
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bpostgres(?:ql)?://[^\s]+"),
)
REQUIRED_FIELDS = (
    "session_id",
    "request_id",
    "agent",
    "model_or_tool",
    "repo",
    "branch",
    "commit_before",
    "commit_after",
    "files_changed",
    "commands_claimed",
    "commands_verified",
    "tests_claimed",
    "tests_verified",
    "artifacts_written",
    "database_writes",
    "failures",
    "blockers",
    "deferred_work",
    "risk_flags",
    "redaction_applied",
    "content_capture",
)
STRING_FIELDS = (
    "session_id",
    "request_id",
    "agent",
    "model_or_tool",
    "repo",
    "branch",
    "commit_before",
    "commit_after",
)
LIST_FIELDS = (
    "files_changed",
    "commands_claimed",
    "commands_verified",
    "tests_claimed",
    "tests_verified",
    "artifacts_written",
    "database_writes",
    "failures",
    "blockers",
    "deferred_work",
    "risk_flags",
)
CLAIM_INPUT_FIELDS = {
    "session_id",
    "request_id",
    "agent",
    "model_or_tool",
    "commands_claimed",
    "tests_claimed",
    "artifacts_written",
    "failures",
    "blockers",
    "deferred_work",
    "risk_flags",
}
EVIDENCE_INPUT_FIELDS = {
    "commands_verified",
    "tests_verified",
    "database_writes",
}
VERIFIED_FIELDS = (
    "commands_verified",
    "tests_verified",
    "database_writes",
)
CLAIM_FIELDS = (
    "commands_claimed",
    "tests_claimed",
)


class SessionRecordError(ValueError):
    """Raised when a session record violates the public v1 contract."""


class RequestIdCollisionError(SessionRecordError):
    """Raised when a request ID already belongs to a finalized session."""


def validate_and_normalize_request_id(request_id: Any) -> str:
    """Validate and canonicalize a strict UUIDv4 or req_-hex request ID."""
    if not isinstance(request_id, str) or not request_id:
        raise SessionRecordError("request_id must be a non-empty string")
    normalized = request_id.strip().lower()

    if REQUEST_HEX_PATTERN.fullmatch(normalized):
        return normalized

    if UUID_V4_PATTERN.fullmatch(normalized):
        parsed = uuid.UUID(normalized)
        if parsed.version == 4 and parsed.variant == uuid.RFC_4122:
            return str(parsed)

    raise SessionRecordError(
        "request_id must be UUIDv4 or req_ followed by 16-64 hex characters"
    )


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def redact_value(value: Any) -> Any:
    """Recursively redact secret-shaped keys and values."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in SENSITIVE_KEY_PARTS or normalized.endswith(
                ("_token", "_secret", "_password", "_key", "_cookie")
            ):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        result = value
        for pattern in SENSITIVE_VALUE_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result
    return value


def _json_identity(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def make_evidence_receipt(source: str, observation: Any) -> dict[str, Any]:
    """Create a redacted, self-verifying receipt for observer-produced evidence."""
    if not isinstance(source, str) or not source:
        raise SessionRecordError("evidence source must be a non-empty string")
    material = {
        "source": source,
        "observation": redact_value(observation),
    }
    return {
        **material,
        "evidence_sha256": "sha256:"
        + hashlib.sha256(canonical_json(material)).hexdigest(),
    }


def verify_evidence_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise SessionRecordError("verified evidence entries must be objects")
    if set(receipt) != {"source", "observation", "evidence_sha256"}:
        raise SessionRecordError("verified evidence receipt fields are invalid")
    source = receipt["source"]
    if not isinstance(source, str) or not source:
        raise SessionRecordError("evidence source must be a non-empty string")
    supplied_hash = receipt["evidence_sha256"]
    if not isinstance(supplied_hash, str) or not RECEIPT_HASH_PATTERN.fullmatch(
        supplied_hash
    ):
        raise SessionRecordError("verified evidence hash is missing or malformed")
    material = {
        "source": source,
        "observation": receipt["observation"],
    }
    expected_hash = "sha256:" + hashlib.sha256(canonical_json(material)).hexdigest()
    if supplied_hash != expected_hash:
        raise SessionRecordError("verified evidence hash does not match its receipt")
    return receipt


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SessionRecordError("session record must be a JSON object")

    missing = [field for field in REQUIRED_FIELDS if field not in record]
    extra = sorted(set(record) - set(REQUIRED_FIELDS))
    if missing:
        raise SessionRecordError(f"missing fields: {', '.join(missing)}")
    if extra:
        raise SessionRecordError(f"unknown fields: {', '.join(extra)}")

    session_id = record["session_id"]
    if not isinstance(session_id, str) or not SESSION_PATTERN.fullmatch(session_id):
        raise SessionRecordError("session_id is invalid")
    request_id = validate_and_normalize_request_id(record["request_id"])
    if record["request_id"] != request_id:
        raise SessionRecordError("request_id must use canonical lowercase form")

    for field in STRING_FIELDS:
        if not isinstance(record[field], str):
            raise SessionRecordError(f"{field} must be a string")

    for field in LIST_FIELDS:
        if not isinstance(record[field], list):
            raise SessionRecordError(f"{field} must be an array")
    if record["files_changed"] != sorted(set(record["files_changed"])):
        raise SessionRecordError("files_changed must be sorted and unique")
    if not all(isinstance(path, str) and path for path in record["files_changed"]):
        raise SessionRecordError("files_changed must contain non-empty strings")

    for field in VERIFIED_FIELDS:
        for receipt in record[field]:
            verify_evidence_receipt(receipt)

    for field in ("commit_before", "commit_after"):
        value = record[field]
        if value and not HASH_PATTERN.fullmatch(value):
            raise SessionRecordError(f"{field} is not a Git object ID")

    if record["redaction_applied"] is not True:
        raise SessionRecordError("redaction_applied must be true")
    if record["content_capture"] is not False:
        raise SessionRecordError("content_capture must be false")

    return record


def _validate_input_fields(
    payload: Any,
    *,
    allowed: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SessionRecordError(f"{label} input must be a JSON object")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SessionRecordError(
            f"{label} input contains unsupported fields: {', '.join(unknown)}"
        )
    return payload


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or "Git command failed"
        raise SessionRecordError(message)
    return result.stdout.rstrip()


def collect_git_evidence(
    repo: str | Path,
    *,
    commit_before: str | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.is_dir():
        raise SessionRecordError(f"repository is not a directory: {repo_path}")

    root = Path(_git(repo_path, "rev-parse", "--show-toplevel")).resolve()
    commit_after = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "branch", "--show-current")

    if commit_before:
        _git(root, "rev-parse", "--verify", f"{commit_before}^{{commit}}")
        changed = _git(
            root,
            "diff",
            "--name-only",
            "--no-renames",
            commit_before,
            commit_after,
            "--",
        ).splitlines()
    else:
        commit_before = commit_after
        changed = _git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).splitlines()
        changed = [line[3:] for line in changed if len(line) > 3]

    return {
        "repo": root.name,
        "branch": branch,
        "commit_before": commit_before,
        "commit_after": commit_after,
        "files_changed": sorted(set(changed)),
    }


def build_record(
    claims: dict[str, Any],
    *,
    repo: str | Path,
    commit_before: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    claims = _validate_input_fields(
        claims,
        allowed=CLAIM_INPUT_FIELDS,
        label="claims",
    )
    observer_evidence = _validate_input_fields(
        evidence or {},
        allowed=EVIDENCE_INPUT_FIELDS,
        label="evidence",
    )
    claim_objects = [item for field in CLAIM_FIELDS for item in claims.get(field, [])]
    claim_identities = {id(item) for item in claim_objects}
    claim_values = {_json_identity(item) for item in claim_objects}
    normalized_evidence: dict[str, list[dict[str, Any]]] = {
        field: [] for field in VERIFIED_FIELDS
    }
    for field in VERIFIED_FIELDS:
        for receipt in observer_evidence.get(field, []):
            if id(receipt) in claim_identities:
                raise SessionRecordError(
                    "verified evidence reuses an unverified claim object"
                )
            verified = verify_evidence_receipt(receipt)
            observation = verified["observation"]
            if id(observation) in claim_identities:
                raise SessionRecordError(
                    "verified evidence reuses an unverified claim observation"
                )
            if _json_identity(observation) in claim_values:
                raise SessionRecordError(
                    "verified evidence is canonically identical to an unverified claim"
                )
            normalized_evidence[field].append(
                make_evidence_receipt(verified["source"], observation)
            )
    git_evidence = collect_git_evidence(repo, commit_before=commit_before)
    candidate = {
        "session_id": claims.get("session_id", ""),
        "request_id": validate_and_normalize_request_id(claims.get("request_id")),
        "agent": claims.get("agent", ""),
        "model_or_tool": claims.get("model_or_tool", ""),
        **git_evidence,
        "commands_claimed": claims.get("commands_claimed", []),
        "commands_verified": normalized_evidence["commands_verified"],
        "tests_claimed": claims.get("tests_claimed", []),
        "tests_verified": normalized_evidence["tests_verified"],
        "artifacts_written": claims.get("artifacts_written", []),
        "database_writes": normalized_evidence["database_writes"],
        "failures": claims.get("failures", []),
        "blockers": claims.get("blockers", []),
        "deferred_work": claims.get("deferred_work", []),
        "risk_flags": claims.get("risk_flags", []),
        "redaction_applied": True,
        "content_capture": False,
    }
    redacted = redact_value(candidate)
    return validate_record(redacted)


def record_digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(validate_record(record))).hexdigest()


def render_audit(record: dict[str, Any], digest: str) -> str:
    validate_record(record)

    def section(title: str, field: str) -> list[str]:
        values = record[field]
        lines = [f"## {title}", ""]
        if not values:
            lines.append("- None recorded.")
        else:
            lines.extend(
                f"- `{json.dumps(item, sort_keys=True, ensure_ascii=False)}`"
                for item in values
            )
        lines.append("")
        return lines

    lines = [
        f"# Recon Session {record['session_id']}",
        "",
        "## Session Context",
        "",
        f"- Session ID: `{record['session_id']}`",
        f"- Request Correlation ID: `{record['request_id']}`",
        "- Flight Recorder Role: `evidence-only`",
        "",
        f"- Agent: `{record['agent']}`",
        f"- Model/Tool: `{record['model_or_tool']}`",
        f"- Repository: `{record['repo']}`",
        f"- Branch: `{record['branch']}`",
        f"- Commit before: `{record['commit_before']}`",
        f"- Commit after: `{record['commit_after']}`",
        f"- Record SHA-256: `{digest}`",
        "- Redaction applied: `true`",
        "- Content capture: `false`",
        "",
    ]
    for title, field in (
        ("Files Changed", "files_changed"),
        ("Commands Claimed", "commands_claimed"),
        ("Commands Verified", "commands_verified"),
        ("Tests Claimed", "tests_claimed"),
        ("Tests Verified", "tests_verified"),
        ("Artifacts Written", "artifacts_written"),
        ("Database Writes", "database_writes"),
        ("Failures", "failures"),
        ("Blockers", "blockers"),
        ("Deferred Work", "deferred_work"),
        ("Risk Flags", "risk_flags"),
    ):
        lines.extend(section(title, field))
    return "\n".join(lines).rstrip() + "\n"


def _exclusive_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def reserve_request_id(
    reservations_dir: str | Path,
    *,
    request_id: str,
    session_id: str,
    record_sha256: str,
) -> Path:
    normalized = validate_and_normalize_request_id(request_id)
    path = Path(reservations_dir).expanduser().resolve() / f"{normalized}.reservation"
    try:
        _exclusive_write(
            path,
            canonical_json(
                {
                    "request_id": normalized,
                    "session_id": session_id,
                    "record_sha256": record_sha256,
                }
            )
            + b"\n",
        )
    except FileExistsError as exc:
        raise RequestIdCollisionError(
            f"request_id collision: {normalized} is already reserved"
        ) from exc
    return path


def write_session_artifacts(
    record: dict[str, Any],
    *,
    records_dir: str | Path,
    audit_dir: str | Path,
    reservations_dir: str | Path | None = None,
) -> dict[str, str]:
    record = validate_record(record)
    digest = record_digest(record)
    stem = record["session_id"]
    records_root = Path(records_dir).expanduser().resolve()
    json_path = records_root / f"{stem}.json"
    digest_path = json_path.with_suffix(".sha256")
    audit_path = Path(audit_dir).expanduser().resolve() / f"recon_{stem}.md"
    reservations_root = (
        Path(reservations_dir).expanduser().resolve()
        if reservations_dir is not None
        else records_root.parent / "reservations"
    )

    created: list[Path] = []
    try:
        reservation_path = reserve_request_id(
            reservations_root,
            request_id=record["request_id"],
            session_id=record["session_id"],
            record_sha256=digest,
        )
        created.append(reservation_path)
        _exclusive_write(json_path, canonical_json(record) + b"\n")
        created.append(json_path)
        _exclusive_write(digest_path, f"{digest}  {json_path.name}\n".encode())
        created.append(digest_path)
        _exclusive_write(audit_path, render_audit(record, digest).encode("utf-8"))
        created.append(audit_path)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise

    return {
        "record": str(json_path),
        "digest": str(digest_path),
        "audit": str(audit_path),
        "request_reservation": str(reservation_path),
        "sha256": digest,
    }

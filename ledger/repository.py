from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ledger.models import ClaimRecord, SourceRef
from ledger.parse import parse_claim_file

TRANSACTION_DIR = ".transactions"
TRANSACTION_INTENT_SUFFIX = ".intent.json"

CLAIM_SCHEMA_VERSION = "ledger.claim.v1"
CLAIM_FILENAME_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")


def resolve_repo(root: Path | None) -> Path:
    if root is None:
        return Path.cwd()
    return root.expanduser().resolve()


def _date_archive(repo: Path) -> bool:
    """True when `repo` is the flight-recorder date-first archive root.

    Claims then live under <repo>/YYYY/MM/DD/ledger/. Enabled explicitly via
    FLIGHT_RECORDER_STORE matching the repo root, so tests and legacy
    single-root layouts are unaffected.
    """
    store = os.environ.get("FLIGHT_RECORDER_STORE")
    if not store:
        return False
    try:
        return Path(store).expanduser().resolve() == repo.resolve()
    except OSError:
        return False


def claims_dir(repo: Path) -> Path:
    if (repo / "claims").is_dir() or repo.name == "ledger":
        return repo / "claims"
    return repo / "storage" / "ledger" / "claims"


def day_claims_dir(repo: Path, day: date | None = None) -> Path:
    """Date-first home for claims recorded on `day`.

    Defaults to the operator-local day so ledger, recon, and recall bucket
    into the SAME daily folder — the archive's complete-view guarantee.
    """
    day = day or date.today()
    return repo / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "ledger"


def legacy_claims_dir(repo: Path) -> Path:
    return repo / "30-ledger" / "claims"


def receipts_jsonl(repo: Path) -> Path:
    if _date_archive(repo):
        return repo / "receipts" / "claims.jsonl"
    if (repo / "receipts").is_dir() or repo.name == "ledger":
        return repo / "receipts" / "claims.jsonl"
    return repo / "storage" / "ledger" / "receipts" / "claims.jsonl"


def load_claims(repo: Path) -> list[ClaimRecord]:
    if _date_archive(repo):
        claims: list[ClaimRecord] = []
        for path in sorted(repo.rglob("ledger/*.md")):
            claims.extend(parse_claim_file(path))
        return claims

    directory = claims_dir(repo)
    if not directory.is_dir():
        directory = legacy_claims_dir(repo)
    if not directory.is_dir():
        return []

    claims = []
    for path in sorted(directory.rglob("*.md")):
        claims.extend(parse_claim_file(path))
    return claims


def _claim_id_exists(repo: Path, claim_id: str) -> bool:
    """Check if a claim ID already exists anywhere in the canonical store."""
    for claim in load_claims(repo):
        if claim.id == claim_id:
            return True
    return False


def add_claim(repo: Path, claim: ClaimRecord) -> ClaimRecord:
    # Check for duplicate ID across entire store (all date folders)
    if claim.id and _claim_id_exists(repo, claim.id):
        raise ValueError(f"claim id '{claim.id}' already exists in store")
    _write_claim(repo, _prepare_claim(claim, operation="add_claim"))
    return claim


def supersede(repo: Path, old_claim_id: str, new_claim: ClaimRecord) -> ClaimRecord:
    """Supersede a claim with a new one using transaction safety.

    Uses a lock and transaction intent file for crash recovery.
    """
    # Acquire lock for the old claim
    lock_path = repo / f".lock-{old_claim_id}"
    lock_fd = _acquire_lock(lock_path)

    try:
        now = _now()
        old_claim = _find_claim(repo, old_claim_id)
        if old_claim is None:
            raise ValueError(f"cannot supersede unknown claim id: {old_claim_id}")

        # Re-check state after acquiring lock
        if old_claim.status == "superseded":
            raise ValueError(f"claim {old_claim_id} is already superseded")

        if not new_claim.supersedes:
            new_claim.supersedes = old_claim_id
        if new_claim.supersedes != old_claim_id:
            raise ValueError("new claim supersedes a different claim id")
        if not new_claim.created:
            new_claim.created = now
        new_claim.updated = now

        # Prepare new claim to get its ID, then check for duplicate
        new_prepped = _prepare_claim(new_claim, operation="add_claim")
        if _claim_id_exists(repo, new_prepped.id):
            raise ValueError(f"claim id '{new_prepped.id}' already exists in store")
        old_prepped = _prepare_claim(old_claim, operation="supersede")

        # Write transaction intent FIRST
        old_claim_data = _claim_to_intent_dict(old_prepped)
        new_claim_data = _claim_to_intent_dict(new_prepped)
        _write_transaction_intent(repo, "supersede", old_claim_id, new_prepped.id, old_claim_data, new_claim_data)

        # Update old claim
        old_prepped.status = "superseded"
        old_prepped.updated = now
        old_prepped.superseded_by = new_prepped.id

        # Write new claim FIRST (so it exists if we crash)
        _write_claim(repo, new_prepped)

        # Update transaction state
        _update_transaction_state(repo, "supersede", old_claim_id, new_prepped.id, "new_written")

        # Update old claim
        _write_claim(repo, old_prepped)

        # Mark transaction committed and clean up
        _update_transaction_state(repo, "supersede", old_claim_id, new_prepped.id, "committed")
        _cleanup_transaction_intent(repo, "supersede", old_claim_id, new_prepped.id)

        return new_prepped
    finally:
        _release_lock(lock_fd)


def contest(repo: Path, claim_id: str, *, reason: str, contested_by: str | None = None) -> ClaimRecord:
    now = _now()
    claim = _find_claim(repo, claim_id)
    if claim is None:
        raise ValueError(f"cannot contest unknown claim id: {claim_id}")

    note = reason if not contested_by else f"{reason} (contested_by={contested_by})"
    claim.status = "contested"
    claim.confidence = "contested"
    claim.updated = now
    claim.note = note
    _write_claim(repo, _prepare_claim(claim, operation="contest"))
    return claim


def contest_linked(
    repo: Path,
    target_id: str,
    *,
    rationale: str,
    sources: list[SourceRef],
    topic: str | None = None,
    confidence: str = "high",
    contest_id: str | None = None,
) -> tuple[ClaimRecord, ClaimRecord]:
    """Contest a claim by writing a new atomic contest claim with transaction safety.

    Contest evidence lives in the new claim (relations ``disputes:<target_id>``,
    ``contradicts=[target_id]``), never in the target's body. The target only
    changes state: status/confidence become contested plus a pointer note.

    Uses a transaction intent file for crash recovery.
    """
    if not rationale.strip():
        raise ValueError("contest requires a non-empty rationale")
    if not sources:
        raise ValueError("contest requires at least one evidence source")

    # Acquire a simple file-based lock for the target claim
    lock_path = repo / f".lock-{target_id}"
    lock_fd = _acquire_lock(lock_path)

    try:
        now = _now()
        target = _find_claim(repo, target_id)
        if target is None:
            raise ValueError(f"cannot contest unknown claim id: {target_id}")

        # Re-check target state after acquiring lock
        if target.status == "contested":
            # Already contested, return existing state
            existing_contest_id = None
            if target.note:
                for part in target.note.split(";"):
                    part = part.strip()
                    if part.startswith("contested_by="):
                        existing_contest_id = part.split("=", 1)[1]
                        break
            if existing_contest_id:
                existing = _find_claim(repo, existing_contest_id)
                if existing:
                    return existing, target

        contest_claim = ClaimRecord(
            id=contest_id or "",
            statement=rationale,
            topic=topic or target.topic,
            type="claim",
            sources=list(sources),
            confidence=confidence,
            status="active",
            created=now,
            updated=now,
            contradicts=[target_id],
            relations=[f"disputes:{target_id}"],
        )

        # Prepare new claim to get its ID, then check for duplicate
        new_prepped = _prepare_claim(contest_claim, operation="contest")
        if _claim_id_exists(repo, new_prepped.id):
            raise ValueError(f"claim id '{new_prepped.id}' already exists in store")

        # Prepare both claims
        target_prepped = _prepare_claim(target, operation="contest")
        target_prepped = _prepare_claim(target, operation="contest")

        # Write transaction intent FIRST
        old_claim_data = _claim_to_intent_dict(target_prepped)
        new_claim_data = _claim_to_intent_dict(new_prepped)
        _write_transaction_intent(repo, "contest_linked", target_id, new_prepped.id, old_claim_data, new_claim_data)

        # Write contest claim
        _write_claim(repo, new_prepped)

        # Update transaction state
        _update_transaction_state(repo, "contest_linked", target_id, new_prepped.id, "contest_written")

        # Update target
        pointer = f"contested_by={new_prepped.id}"
        target_prepped.status = "contested"
        target_prepped.confidence = "contested"
        target_prepped.updated = now
        target_prepped.note = pointer if not target_prepped.note else f"{target_prepped.note}; {pointer}"

        # Write target
        _write_claim(repo, target_prepped)

        # Mark transaction committed and clean up
        _update_transaction_state(repo, "contest_linked", target_id, new_prepped.id, "committed")
        _cleanup_transaction_intent(repo, "contest_linked", target_id, new_prepped.id)

        return new_prepped, target_prepped
    finally:
        _release_lock(lock_fd)


def append_receipt(repo: Path, receipt: dict[str, Any]) -> None:
    path = receipts_jsonl(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")


def claim_path(repo: Path, claim_id: str) -> Path:
    safe = CLAIM_FILENAME_PATTERN.sub("-", claim_id).strip(".-") or f"claim-{uuid4().hex}"
    if _date_archive(repo):
        return day_claims_dir(repo) / f"{safe}.md"
    return claims_dir(repo) / f"{safe}.md"


def _prepare_claim(claim: ClaimRecord, *, operation: str) -> ClaimRecord:
    now = _now()
    if not claim.id:
        claim.id = f"clm-{datetime.now(timezone.utc).year}-{uuid4().hex[:8]}"
    if not claim.created:
        claim.created = now
    if not claim.updated:
        claim.updated = claim.created
    claim.raw = {**claim.raw, "schema_version": CLAIM_SCHEMA_VERSION, "operation": operation, "recorded_at": now}
    return claim


def _write_claim(repo: Path, claim: ClaimRecord) -> None:
    # Append-only store: an existing claim (status/link updates from
    # supersede/contest) is rewritten where it lives; only genuinely new
    # claims land in today's folder of the date-first archive.
    if claim.source_file and Path(claim.source_file).is_file():
        path = Path(claim.source_file)
    else:
        path = claim_path(repo, claim.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _render_claim_markdown(claim)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o644)  # mkstemp defaults to 0600; claims hold no secrets and the operator must read them
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _render_claim_markdown(claim: ClaimRecord) -> str:
    return f"# Claim {claim.id}\n\n```yaml\n{_render_yaml(_claim_to_mapping(claim))}```\n"


def _claim_to_mapping(claim: ClaimRecord) -> dict[str, Any]:
    data = asdict(claim)
    data.pop("source_file", None)
    raw = data.pop("raw", {}) or {}
    ordered: dict[str, Any] = {}
    for key in (
        "schema_version",
        "operation",
        "recorded_at",
        "id",
        "statement",
        "topic",
        "type",
        "sources",
        "confidence",
        "valid_from",
        "valid_until",
        "status",
        "supersedes",
        "superseded_by",
        "contradicts",
        "relations",
        "created",
        "updated",
        "source_type",
        "source_ref",
        "source_hash",
        "note",
    ):
        if key in {"schema_version", "operation", "recorded_at"} and key in raw:
            ordered[key] = raw[key]
        elif key in data:
            ordered[key] = data[key]
    return _json_ready(ordered)


def _render_yaml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        lines.extend(_render_yaml_value(key, value, indent=0))
    return "".join(lines)


def _render_yaml_value(key: str, value: Any, *, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []\n"]
        if not any(isinstance(item, dict) for item in value):
            rendered = ", ".join(_format_scalar(item) for item in value)
            return [f"{pad}{key}: [{rendered}]\n"]
        lines = [f"{pad}{key}:\n"]
        for item in value:
            if isinstance(item, dict):
                item_keys = list(item)
                first = item_keys[0]
                lines.append(f"{pad}  - {first}: {_format_scalar(item[first])}\n")
                for nested_key in item_keys[1:]:
                    lines.append(f"{pad}    {nested_key}: {_format_scalar(item[nested_key])}\n")
            else:
                lines.append(f"{pad}  - {_format_scalar(item)}\n")
        return lines
    return [f"{pad}{key}: {_format_scalar(value)}\n"]


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # JSON strings are valid YAML double-quoted scalars. Always quote strings so
    # YAML cannot coerce evidence such as "yes", "12", or "2026-08-08" into a
    # bool, number, or date, and cannot interpret "-" as sequence syntax.
    return json.dumps(str(value), ensure_ascii=False)


def _find_claim(repo: Path, claim_id: str) -> ClaimRecord | None:
    for claim in load_claims(repo):
        if claim.id == claim_id:
            return claim
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_ready(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _acquire_lock(lock_path: Path, timeout: float = 30.0) -> int:
    """Acquire an OS advisory lock (fcntl.flock) on a lock file.

    Returns the file descriptor, which must be kept open until release.
    The lock is automatically released when the FD is closed or the process dies.
    """
    import fcntl
    import time

    # Open the lock file (create if needed)
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)

    start = time.time()
    while True:
        try:
            # Try to acquire exclusive lock (non-blocking)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.time() - start > timeout:
                os.close(fd)
                raise TimeoutError(f"Could not acquire lock on {lock_path}")
            time.sleep(0.05)


def _release_lock(fd: int) -> None:
    """Release an OS advisory lock by closing the file descriptor.

    The kernel automatically releases the lock when the FD is closed.
    """
    try:
        os.close(fd)
    except OSError:
        pass


def _cleanup_transaction_intent(repo: Path, operation: str, old_id: str, new_id: str) -> None:
    """Remove a transaction intent file after successful completion."""
    path = _transaction_intent_path(repo, operation, old_id, new_id)
    path.unlink(missing_ok=True)


def _transaction_dir(repo: Path) -> Path:
    """Get the transaction directory for a repo."""
    tx_dir = repo / TRANSACTION_DIR
    tx_dir.mkdir(parents=True, exist_ok=True)
    return tx_dir


def _transaction_intent_path(repo: Path, operation: str, old_id: str, new_id: str) -> Path:
    """Generate path for a transaction intent file."""
    tx_dir = _transaction_dir(repo)
    # Sanitize IDs for filesystem
    safe_old = re.sub(r"[^a-zA-Z0-9_.-]", "-", old_id)
    safe_new = re.sub(r"[^a-zA-Z0-9_.-]", "-", new_id)
    filename = f"{operation}-{safe_old}-{safe_new}{TRANSACTION_INTENT_SUFFIX}"
    return tx_dir / filename


def _write_transaction_intent(repo: Path, operation: str, old_id: str, new_id: str,
                               old_claim_data: dict, new_claim_data: dict) -> Path:
    """Write a durable transaction intent before executing the operation."""
    intent = {
        "schema_version": "ledger.transaction.v1",
        "operation": operation,
        "old_id": old_id,
        "new_id": new_id,
        "old_claim": old_claim_data,
        "new_claim": new_claim_data,
        "state": "prepared",
        "created_at": _now(),
    }
    path = _transaction_intent_path(repo, operation, old_id, new_id)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(intent, f, sort_keys=True, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return path


def _read_transaction_intent(repo: Path, operation: str, old_id: str, new_id: str) -> dict | None:
    """Read a transaction intent file if it exists."""
    path = _transaction_intent_path(repo, operation, old_id, new_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _update_transaction_state(repo: Path, operation: str, old_id: str, new_id: str,
                               state: str) -> None:
    """Update the state of a transaction intent."""
    path = _transaction_intent_path(repo, operation, old_id, new_id)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["state"] = state
        data["updated_at"] = _now()
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError):
        pass


def _claim_to_intent_dict(claim: ClaimRecord) -> dict:
    """Convert a ClaimRecord to a dict suitable for transaction intent storage."""
    # Use the same serialization as _claim_to_mapping but include all fields
    data = asdict(claim)
    data.pop("source_file", None)
    raw = data.pop("raw", {}) or {}
    result = {}
    for key in (
        "id", "statement", "topic", "type", "sources", "confidence", "valid_from",
        "valid_until", "status", "supersedes", "superseded_by", "contradicts",
        "relations", "created", "updated", "source_type", "source_ref", "source_hash", "note",
    ):
        if key in data:
            result[key] = _json_ready(data[key])
    # Include raw metadata
    for key in ("schema_version", "operation", "recorded_at"):
        if key in raw:
            result[key] = raw[key]
    return result


def _intent_dict_to_claim(data: dict) -> ClaimRecord:
    """Reconstruct a ClaimRecord from transaction intent dict."""
    # Parse dates
    valid_from = None
    valid_until = None
    if data.get("valid_from"):
        try:
            valid_from = date.fromisoformat(str(data["valid_from"])[:10])
        except ValueError:
            pass
    if data.get("valid_until"):
        try:
            valid_until = date.fromisoformat(str(data["valid_until"])[:10])
        except ValueError:
            pass

    return ClaimRecord(
        id=data.get("id", ""),
        statement=data.get("statement", ""),
        topic=data.get("topic", ""),
        type=data.get("type", ""),
        sources=[
            SourceRef(
                ref=s.get("ref", ""),
                quote=s.get("quote", ""),
                locator=s.get("locator"),
                source_type=s.get("source_type"),
                source_hash=s.get("source_hash"),
            )
            for s in (data.get("sources") or []) if isinstance(s, dict)
        ],
        confidence=data.get("confidence", ""),
        status=data.get("status", ""),
        created=data.get("created", ""),
        updated=data.get("updated", ""),
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes=data.get("supersedes"),
        superseded_by=data.get("superseded_by"),
        contradicts=data.get("contradicts", []),
        relations=data.get("relations", []),
        note=data.get("note"),
        source_type=data.get("source_type"),
        source_ref=data.get("source_ref"),
        source_hash=data.get("source_hash"),
        raw=data.get("raw", {}),
    )


def recover_transactions(repo: Path) -> list[str]:
    """Recover incomplete transactions from intent files.

    Returns list of actions taken.
    """
    tx_dir = _transaction_dir(repo)
    if not tx_dir.is_dir():
        return []

    actions = []
    for path in tx_dir.glob(f"*{TRANSACTION_INTENT_SUFFIX}"):
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if intent.get("state") == "committed":
            # Already done, clean up
            path.unlink(missing_ok=True)
            continue

        operation = intent.get("operation")
        old_id = intent.get("old_id")
        new_id = intent.get("new_id")
        old_claim_data = intent.get("old_claim")
        new_claim_data = intent.get("new_claim")
        state = intent.get("state", "prepared")

        # Check current state of claims
        old_claim = _find_claim(repo, old_id) if old_id else None
        new_claim = _find_claim(repo, new_id) if new_id else None

        try:
            if operation == "supersede":
                if state == "prepared":
                    # Operation was started but neither claim written
                    # Check if old claim is still active
                    if old_claim and old_claim.status == "active" and not new_claim:
                        # Nothing done, operation never materialized
                        _update_transaction_state(repo, operation, old_id, new_id, "aborted")
                        path.unlink(missing_ok=True)
                        actions.append(f"aborted supersede {old_id}->{new_id} (never started)")
                    elif old_claim and old_claim.status == "active" and new_claim:
                        # Old claim exists, new claim was written, need to finish old
                        if new_claim.supersedes == old_id:
                            old_claim.status = "superseded"
                            old_claim.superseded_by = new_id
                            old_claim.updated = _now()
                            _write_claim(repo, _prepare_claim(old_claim, operation="supersede"))
                            _update_transaction_state(repo, operation, old_id, new_id, "committed")
                            path.unlink(missing_ok=True)
                            actions.append(f"recovered supersede {old_id}->{new_id} (wrote old claim)")
                        else:
                            actions.append(f"inconsistent supersede {old_id}->{new_id}: new claim supersedes={new_claim.supersedes}")
                    elif old_claim and old_claim.status == "superseded" and new_claim:
                        # Both claims already in final state
                        if old_claim.superseded_by == new_id and new_claim.supersedes == old_id:
                            _update_transaction_state(repo, operation, old_id, new_id, "committed")
                            path.unlink(missing_ok=True)
                            actions.append(f"completed supersede {old_id}->{new_id} (already consistent)")
                        else:
                            actions.append(f"inconsistent supersede {old_id}->{new_id}: reciprocal links mismatch")
                    else:
                        actions.append(f"cannot recover supersede {old_id}->{new_id}: unexpected state")

            elif operation == "contest_linked":
                if state == "prepared":
                    if old_claim and old_claim.status == "active" and not new_claim:
                        # Contest claim never written
                        _update_transaction_state(repo, operation, old_id, new_id, "aborted")
                        path.unlink(missing_ok=True)
                        actions.append(f"aborted contest {old_id}->{new_id} (never started)")
                    elif old_claim and old_claim.status == "active" and new_claim:
                        # Contest claim written but target not updated
                        if new_claim.contradicts == [old_id] and f"disputes:{old_id}" in new_claim.relations:
                            old_claim.status = "contested"
                            old_claim.confidence = "contested"
                            old_claim.updated = _now()
                            pointer = f"contested_by={new_id}"
                            old_claim.note = pointer if not old_claim.note else f"{old_claim.note}; {pointer}"
                            _write_claim(repo, _prepare_claim(old_claim, operation="contest"))
                            _update_transaction_state(repo, operation, old_id, new_id, "committed")
                            path.unlink(missing_ok=True)
                            actions.append(f"recovered contest {old_id}->{new_id} (updated target)")
                        else:
                            actions.append(f"inconsistent contest {old_id}->{new_id}: new claim links mismatch")
                    elif old_claim and old_claim.status == "contested" and new_claim:
                        if f"contested_by={new_id}" in (old_claim.note or ""):
                            _update_transaction_state(repo, operation, old_id, new_id, "committed")
                            path.unlink(missing_ok=True)
                            actions.append(f"completed contest {old_id}->{new_id} (already consistent)")
                        else:
                            actions.append(f"inconsistent contest {old_id}->{new_id}: note mismatch")
                    else:
                        actions.append(f"cannot recover contest {old_id}->{new_id}: unexpected state")
        except Exception as e:
            actions.append(f"error recovering {path.name}: {e}")

    return actions

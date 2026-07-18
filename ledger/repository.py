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


def add_claim(repo: Path, claim: ClaimRecord) -> ClaimRecord:
    _write_claim(repo, _prepare_claim(claim, operation="add_claim"))
    return claim


def supersede(repo: Path, old_claim_id: str, new_claim: ClaimRecord) -> ClaimRecord:
    now = _now()
    old_claim = _find_claim(repo, old_claim_id)
    if old_claim is None:
        raise ValueError(f"cannot supersede unknown claim id: {old_claim_id}")

    if not new_claim.supersedes:
        new_claim.supersedes = old_claim_id
    if new_claim.supersedes != old_claim_id:
        raise ValueError("new claim supersedes a different claim id")
    if not new_claim.created:
        new_claim.created = now
    new_claim.updated = now
    _prepare_claim(new_claim, operation="add_claim")  # assigns the id before it is linked

    old_claim.status = "superseded"
    old_claim.updated = now
    old_claim.superseded_by = new_claim.id

    _write_claim(repo, _prepare_claim(old_claim, operation="supersede"))
    _write_claim(repo, new_claim)
    return new_claim


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
    """Contest a claim by writing a new atomic contest claim.

    Contest evidence lives in the new claim (relations ``disputes:<target_id>``,
    ``contradicts=[target_id]``), never in the target's body. The target only
    changes state: status/confidence become contested plus a pointer note.
    """
    if not rationale.strip():
        raise ValueError("contest requires a non-empty rationale")
    if not sources:
        raise ValueError("contest requires at least one evidence source")

    now = _now()
    target = _find_claim(repo, target_id)
    if target is None:
        raise ValueError(f"cannot contest unknown claim id: {target_id}")

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
    _write_claim(repo, _prepare_claim(contest_claim, operation="contest"))

    pointer = f"contested_by={contest_claim.id}"
    target.status = "contested"
    target.confidence = "contested"
    target.updated = now
    target.note = pointer if not target.note else f"{target.note}; {pointer}"
    _write_claim(repo, _prepare_claim(target, operation="contest"))
    return contest_claim, target


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
    text = str(value)
    if not text:
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_.:/@+-]+", text):
        return text
    return json.dumps(text, ensure_ascii=False)


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

from __future__ import annotations

from datetime import date
from pathlib import Path

from ledger.models import ClaimRecord, SourceRef
from ledger.yaml_subset import extract_yaml_blocks, parse_yaml_subset


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() == "unknown":
        return None
    return date.fromisoformat(text)


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_sources(raw: object) -> list[SourceRef]:
    if not isinstance(raw, list):
        return []
    sources: list[SourceRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = _coerce_str(item.get("ref"))
        quote = _coerce_str(item.get("quote"))
        if not ref or not quote:
            continue
        sources.append(
            SourceRef(
                ref=ref,
                quote=quote,
                locator=_coerce_str(item.get("locator")),
                source_type=_coerce_str(item.get("source_type")),
                source_hash=_coerce_str(item.get("source_hash")),
            )
        )
    return sources


def claim_from_mapping(data: dict, *, source_file: str | None = None) -> ClaimRecord:
    return ClaimRecord(
        id=str(data.get("id", "")).strip(),
        statement=str(data.get("statement", "")).strip(),
        topic=str(data.get("topic", "")).strip(),
        type=str(data.get("type", "")).strip(),
        sources=_parse_sources(data.get("sources")),
        confidence=str(data.get("confidence", "")).strip(),
        status=str(data.get("status", "")).strip(),
        created=str(data.get("created", "")).strip(),
        updated=str(data.get("updated", "")).strip(),
        valid_from=_parse_date(data.get("valid_from")),
        valid_until=_parse_date(data.get("valid_until")),
        supersedes=_coerce_str(data.get("supersedes")),
        superseded_by=_coerce_str(data.get("superseded_by")),
        contradicts=[str(x) for x in (data.get("contradicts") or []) if x],
        relations=[str(x) for x in (data.get("relations") or []) if x],
        note=_coerce_str(data.get("note")),
        source_type=_coerce_str(data.get("source_type")),
        source_ref=_coerce_str(data.get("source_ref")),
        source_hash=_coerce_str(data.get("source_hash")),
        source_file=source_file,
        raw=data,
    )


def parse_claim_file(path: Path) -> list[ClaimRecord]:
    text = path.read_text(encoding="utf-8")
    claims: list[ClaimRecord] = []
    for block in extract_yaml_blocks(text):
        data = parse_yaml_subset(block)
        if not data.get("id"):
            continue
        claims.append(claim_from_mapping(data, source_file=str(path)))
    return claims

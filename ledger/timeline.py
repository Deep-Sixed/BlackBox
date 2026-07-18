"""Chronological reconstruction over the claim store.

Read-only. A timeline entry is one claim in temporal order; the optional
as-of snapshot lists what was valid on a given date, so an auditor can
reconstruct exactly what the ledger believed at any point in time.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ledger.models import ClaimRecord


def _sort_key(claim: ClaimRecord) -> tuple:
    anchor = claim.valid_from.isoformat() if claim.valid_from else claim.created or ""
    return (anchor, claim.id)


def supersession_chain(claims: list[ClaimRecord], claim_id: str) -> list[ClaimRecord]:
    """Walk supersedes/superseded_by links to both ends from claim_id."""
    by_id = {claim.id: claim for claim in claims}
    if claim_id not in by_id:
        return []

    chain = [by_id[claim_id]]
    seen = {claim_id}
    cursor = by_id[claim_id]
    while cursor.supersedes and cursor.supersedes in by_id and cursor.supersedes not in seen:
        cursor = by_id[cursor.supersedes]
        seen.add(cursor.id)
        chain.insert(0, cursor)
    cursor = by_id[claim_id]
    while cursor.superseded_by and cursor.superseded_by in by_id and cursor.superseded_by not in seen:
        cursor = by_id[cursor.superseded_by]
        seen.add(cursor.id)
        chain.append(cursor)
    return chain


def timeline_entry(claim: ClaimRecord) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "statement": claim.statement,
        "topic": claim.topic,
        "type": claim.type,
        "status": claim.status,
        "confidence": claim.confidence,
        "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
        "valid_until": claim.valid_until.isoformat() if claim.valid_until else None,
        "supersedes": claim.supersedes,
        "superseded_by": claim.superseded_by,
        "contradicts": claim.contradicts,
        "relations": claim.relations,
        "created": claim.created,
        "updated": claim.updated,
    }


def build_timeline(
    claims: list[ClaimRecord],
    *,
    topic: str | None = None,
    claim_id: str | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Chronological view of a topic or a claim's supersession chain.

    Exactly one of topic/claim_id selects the scope; with neither, the whole
    store is returned chronologically. as_of adds a validity snapshot.
    """
    if claim_id:
        selected = supersession_chain(claims, claim_id)
        if not selected:
            raise ValueError(f"unknown claim id: {claim_id}")
    elif topic:
        selected = [claim for claim in claims if claim.topic == topic]
    else:
        selected = list(claims)

    selected = sorted(selected, key=_sort_key)
    result: dict[str, Any] = {
        "ok": True,
        "scope": {"topic": topic, "claim_id": claim_id},
        "entries": [timeline_entry(claim) for claim in selected],
    }
    if as_of is not None:
        result["as_of"] = as_of.isoformat()
        result["valid_as_of"] = [
            timeline_entry(claim) for claim in selected if claim.valid_on(as_of)
        ]
    return result

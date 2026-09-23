"""Explicit canonical records and an unauthenticated, append-only SHA-256 chain."""

import hashlib
import json

from .models import canonical, identity

# Order is part of the v1 backfill contract. Field names are part of v2 material.
V2_RECORD_FIELDS = {
    "sessions": ("id", "request_id", "fingerprint", "producer", "created_at"),
    "sources": ("id", "session_id", "identity", "authority"),
    "observations": ("id", "session_id", "source_id", "kind", "data", "recorded_at"),
    "evidence": ("id", "observation_id", "digest", "verification"),
    "claims": (
        "id",
        "session_id",
        "source_id",
        "topic",
        "statement",
        "target_id",
        "relation",
    ),
    "artifacts": ("id", "session_id", "source_id", "path", "digest", "verification"),
    "events": ("sequence", "id", "session_id", "kind", "entity_id", "recorded_at"),
    "failures": ("id", "session_id", "event_id", "code", "retryable"),
}
V3_RECORD_FIELDS = {
    "claim_relations": ("id", "claim_id", "target_id", "relation", "event_id"),
    "evidence_links": (
        "id",
        "session_id",
        "source_id",
        "claim_id",
        "observation_id",
        "evidence_id",
        "artifact_id",
        "relation",
        "recorded_at",
    ),
}
RECORD_FIELDS = {**V2_RECORD_FIELDS, **V3_RECORD_FIELDS}
GENESIS = "0" * 64


def digest(table, row, sequence, previous):
    material = {field: row[field] for field in RECORD_FIELDS[table]}
    if table == "observations":
        material["data"] = json.loads(material["data"])
    return hashlib.sha256(
        canonical(
            {
                "format": "blackbox.record.v3"
                if table in V3_RECORD_FIELDS
                else "blackbox.record.v2",
                "sequence": sequence,
                "record_type": table,
                "record_id": row["id"],
                "material": material,
                "previous_digest": previous,
            }
        ).encode()
    ).hexdigest()


def append_receipt(connection, table, key):
    if not connection.in_transaction:
        raise ValueError("receipt requires a transaction")
    row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
    tail = connection.execute(
        "SELECT sequence,digest FROM record_receipts ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence, previous = (tail[0] + 1, tail[1]) if tail else (1, GENESIS)
    connection.execute(
        "INSERT INTO record_receipts VALUES (?,?,?,?,?)",
        (
            sequence,
            table,
            key,
            digest(table, row, sequence, previous),
            previous,
        ),
    )


def evidence_errors(connection):
    errors = set()
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        errors.add("sqlite_integrity")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        errors.add("foreign_keys")
    observations = {
        row["id"]: row for row in connection.execute("SELECT * FROM observations")
    }
    seen = set()
    for receipt in connection.execute("SELECT * FROM evidence"):
        key = receipt["observation_id"]
        if key not in observations or key in seen:
            errors.add("receipt_coverage")
            continue
        seen.add(key)
        row = observations[key]
        try:
            material = {
                "session": row["session_id"],
                "source": row["source_id"],
                "kind": row["kind"],
                "data": json.loads(row["data"]),
            }
            expected = hashlib.sha256(canonical(material).encode()).hexdigest()
            if expected != receipt["digest"]:
                errors.add("receipt_integrity")
        except ValueError, TypeError:
            errors.add("receipt_integrity")
    if seen != observations.keys():
        errors.add("receipt_coverage")
    return errors


def record_errors(connection, *, version=3):
    errors = set()
    records = {
        (table, row["id"]): row
        for table in (V2_RECORD_FIELDS if version == 2 else RECORD_FIELDS)
        for row in connection.execute(f"SELECT * FROM {table}")
    }
    seen = set()
    previous = GENESIS
    for sequence, receipt in enumerate(
        connection.execute("SELECT * FROM record_receipts ORDER BY sequence"), 1
    ):
        key = (receipt["record_type"], receipt["record_id"])
        if receipt["sequence"] != sequence:
            errors.add("sequence_continuity")
        if receipt["previous_digest"] != previous:
            errors.add("chain_integrity")
        if key in seen:
            errors.add("duplicate_receipt")
        seen.add(key)
        if key not in records:
            errors.add("orphan_receipt")
        else:
            try:
                expected = digest(
                    key[0],
                    records[key],
                    receipt["sequence"],
                    receipt["previous_digest"],
                )
                if receipt["digest"] != expected:
                    errors.add("record_integrity")
            except ValueError, TypeError:
                errors.add("record_integrity")
        previous = receipt["digest"]
    if records.keys() - seen:
        errors.add("record_coverage")
    return errors


def relationship_errors(connection):
    """Check attribution, relation coverage and local chronology, beyond FKs."""
    errors = set()
    claims = {r["id"]: r for r in connection.execute("SELECT * FROM claims")}
    sources = {r["id"]: r for r in connection.execute("SELECT * FROM sources")}
    events = {r["id"]: r for r in connection.execute("SELECT * FROM events")}
    claim_events = {}
    link_events = {}
    for event in events.values():
        if event["kind"] == "CLAIM":
            claim_events.setdefault(event["entity_id"], []).append(event)
        if event["kind"] == "EVIDENCE_LINK":
            link_events.setdefault(event["entity_id"], []).append(event)
    if claim_events.keys() != claims.keys():
        errors.add("relationship_integrity")
    relations = {
        r["claim_id"]: r for r in connection.execute("SELECT * FROM claim_relations")
    }
    for key, claim in claims.items():
        source = sources.get(claim["source_id"])
        recorded = claim_events.get(key, [])
        if (
            source is None
            or source["session_id"] != claim["session_id"]
            or source["authority"] != "caller_asserted"
            or len(recorded) != 1
            or recorded[0]["session_id"] != claim["session_id"]
        ):
            errors.add("relationship_integrity")
        relation = relations.get(key)
        if source is not None:
            material = {
                "source": source["identity"],
                "topic": claim["topic"],
                "statement": claim["statement"],
            }
            target = relation["target_id"] if relation else None
            kind = relation["relation"] if relation else None
            if identity("clm", [claim["session_id"], material, target, kind]) != key:
                errors.add("relationship_integrity")
        if relation is not None and relation["id"] != identity(
            "rel", [key, relation["target_id"], relation["relation"]]
        ):
            errors.add("relationship_integrity")
        if claim["target_id"] is not None and (
            relation is None
            or relation["target_id"] != claim["target_id"]
            or relation["relation"] != claim["relation"]
        ):
            errors.add("relationship_integrity")
        if relation is None:
            continue
        target = claims.get(relation["target_id"])
        target_events = claim_events.get(relation["target_id"], [])
        if (
            target is None
            or len(recorded) != 1
            or len(target_events) != 1
            or relation["event_id"] != recorded[0]["id"]
            or target_events[0]["sequence"] >= recorded[0]["sequence"]
            or (relation["relation"] != "retracts" and claim["target_id"] is None)
            or (relation["relation"] == "retracts" and claim["target_id"] is not None)
        ):
            errors.add("relationship_integrity")
    if relations.keys() - claims.keys():
        errors.add("relationship_integrity")
    links = list(connection.execute("SELECT * FROM evidence_links"))
    if link_events.keys() != {r["id"] for r in links}:
        errors.add("relationship_integrity")
    for link in links:
        source = sources.get(link["source_id"])
        recorded = link_events.get(link["id"], [])
        target_events = claim_events.get(link["claim_id"], [])
        if (
            source is None
            or source["session_id"] != link["session_id"]
            or source["authority"] != "caller_asserted"
            or len(recorded) != 1
            or len(target_events) != 1
            or recorded[0]["session_id"] != link["session_id"]
            or recorded[0]["recorded_at"] != link["recorded_at"]
            or target_events[0]["sequence"] >= recorded[0]["sequence"]
        ):
            errors.add("relationship_integrity")
    return errors

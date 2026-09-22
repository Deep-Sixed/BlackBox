"""Explicit canonical records and an unauthenticated, append-only SHA-256 chain."""

import hashlib
import json

from .models import canonical

# Order is part of the v1 backfill contract. Field names are part of v2 material.
RECORD_FIELDS = {
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
GENESIS = "0" * 64


def digest(table, row, sequence, previous):
    material = {field: row[field] for field in RECORD_FIELDS[table]}
    if table == "observations":
        material["data"] = json.loads(material["data"])
    return hashlib.sha256(
        canonical(
            {
                "format": "blackbox.record.v2",
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


def record_errors(connection):
    errors = set()
    records = {
        (table, row["id"]): row
        for table in RECORD_FIELDS
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

"""Read-only reconstruction; derived views do not mutate canonical records."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ._signals import EvidenceIssue, MissingRecord
from .db import connect
from .ingest import state
from .integrity import evidence_errors, record_errors, relationship_errors
from .schema import VERSION

# A claim's status comes from the first relation, in this order, that targets it.
STATUS_PRECEDENCE = (
    ("retracts", "retracted"),
    ("supersedes", "superseded"),
    ("contests", "contested"),
)


def reconstruct(database: str | Path, session: str) -> dict:
    with closing(connect(database, readonly=True)) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT * FROM sessions WHERE id=?", (session,)
        ).fetchone()
        if row is None:
            raise MissingRecord("unknown session")
        result = {"session": dict(row), "status": state(connection, session)}
        for table in (
            "sources",
            "observations",
            "claims",
            "artifacts",
            "failures",
            "events",
        ):
            order = "sequence" if table == "events" else "id"
            result[table] = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE session_id=? ORDER BY {order}",
                    (session,),
                )
            ]
        for claim in result["claims"]:
            relation = connection.execute(
                "SELECT target_id,relation FROM claim_relations WHERE claim_id=?",
                (claim["id"],),
            ).fetchone()
            if relation:
                claim.update(dict(relation))
        result["evidence"] = [
            dict(row)
            for row in connection.execute(
                "SELECT e.* FROM evidence e JOIN observations o ON o.id=e.observation_id "
                "WHERE o.session_id=? ORDER BY e.id",
                (session,),
            )
        ]
        for row in result["observations"]:
            try:
                row["data"] = json.loads(row["data"])
            except ValueError, TypeError:
                raise EvidenceIssue("invalid stored observation") from None
        return result


def timeline(database: str | Path, *, through: int | None = None) -> list[dict]:
    with closing(connect(database, readonly=True)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM events WHERE (? IS NULL OR sequence<=?) ORDER BY sequence",
                (through, through),
            )
        ]


def claims(
    database: str | Path, *, through: int | None = None, topic: str | None = None
) -> list[dict]:
    with closing(connect(database, readonly=True)) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT c.id,c.session_id,c.source_id,c.topic,c.statement,r.target_id,r.relation "
                "FROM claims c LEFT JOIN claim_relations r ON r.claim_id=c.id "
                "JOIN events e ON e.entity_id=c.id AND e.kind='CLAIM' "
                "WHERE (? IS NULL OR e.sequence<=?) ORDER BY e.sequence",
                (through, through),
            )
        ]
        targets = {
            relation: {row["target_id"] for row in rows if row["relation"] == relation}
            for relation, _ in STATUS_PRECEDENCE
        }
        for row in rows:
            row["status"] = next(
                (
                    status
                    for relation, status in STATUS_PRECEDENCE
                    if row["id"] in targets[relation]
                ),
                "active",
            )
        return [row for row in rows if topic is None or row["topic"] == topic]


def integrity(database: str | Path) -> dict:
    try:
        connection = connect(database, readonly=True)
    except ValueError:
        return {"ok": False, "schema_version": None, "errors": ["schema_integrity"]}
    except sqlite3.Error, OSError:
        return {"ok": False, "schema_version": None, "errors": ["database_unavailable"]}
    try:
        connection.execute("BEGIN")
        errors = (
            evidence_errors(connection)
            | record_errors(connection)
            | relationship_errors(connection)
        )
        return {"ok": not errors, "schema_version": VERSION, "errors": sorted(errors)}
    except sqlite3.Error:
        return {"ok": False, "schema_version": VERSION, "errors": ["sqlite_integrity"]}
    finally:
        connection.close()


def evidence_links(database, *, claim_id=None, session=None, through=None):
    with closing(connect(database, readonly=True)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT l.id,l.claim_id,coalesce(l.observation_id,l.evidence_id,l.artifact_id) "
                "AS evidence_record_id, CASE WHEN l.observation_id IS NOT NULL THEN 'observation' "
                "WHEN l.evidence_id IS NOT NULL THEN 'evidence' ELSE 'artifact' END AS record_type, "
                "l.relation,l.source_id,l.session_id AS origin_session_id,l.recorded_at,e.sequence "
                "FROM evidence_links l JOIN events e ON e.entity_id=l.id AND e.kind='EVIDENCE_LINK' "
                "WHERE (? IS NULL OR l.claim_id=?) AND (? IS NULL OR l.session_id=?) "
                "AND (? IS NULL OR e.sequence<=?) ORDER BY e.sequence",
                (claim_id, claim_id, session, session, through, through),
            )
        ]


def claim_relations(
    database, *, claim_id=None, target_id=None, session=None, through=None
):
    with closing(connect(database, readonly=True)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT r.id,r.claim_id,r.target_id,r.relation,c.source_id,"
                "c.session_id AS origin_session_id,t.session_id AS target_session_id,"
                "e.recorded_at,e.sequence FROM claim_relations r "
                "JOIN claims c ON c.id=r.claim_id JOIN claims t ON t.id=r.target_id "
                "JOIN events e ON e.id=r.event_id "
                "WHERE (? IS NULL OR r.claim_id=?) AND (? IS NULL OR r.target_id=?) "
                "AND (? IS NULL OR c.session_id=?) AND (? IS NULL OR e.sequence<=?) ORDER BY e.sequence",
                (
                    claim_id,
                    claim_id,
                    target_id,
                    target_id,
                    session,
                    session,
                    through,
                    through,
                ),
            )
        ]

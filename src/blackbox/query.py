"""Read-only reconstruction; derived views do not mutate canonical records."""

import hashlib
import json
from pathlib import Path

from .db import connect
from .ingest import state
from .models import canonical


def reconstruct(database: str | Path, session: str) -> dict:
    connection = connect(database, readonly=True)
    try:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT * FROM sessions WHERE id=?", (session,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown session")
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
        result["evidence"] = [
            dict(row)
            for row in connection.execute(
                "SELECT e.* FROM evidence e JOIN observations o ON o.id=e.observation_id "
                "WHERE o.session_id=? ORDER BY e.id",
                (session,),
            )
        ]
        for row in result["observations"]:
            row["data"] = json.loads(row["data"])
        return result
    finally:
        connection.close()


def timeline(database: str | Path, *, through: int | None = None) -> list[dict]:
    connection = connect(database, readonly=True)
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM events WHERE (? IS NULL OR sequence<=?) ORDER BY sequence",
                (through, through),
            )
        ]
    finally:
        connection.close()


def claims(
    database: str | Path, *, through: int | None = None, topic: str | None = None
) -> list[dict]:
    connection = connect(database, readonly=True)
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT c.* FROM claims c JOIN events e ON e.entity_id=c.id AND e.kind='CLAIM' "
                "WHERE (? IS NULL OR e.sequence<=?) ORDER BY e.sequence",
                (through, through),
            )
        ]
        superseded = {
            row["target_id"] for row in rows if row["relation"] == "supersedes"
        }
        contested = {row["target_id"] for row in rows if row["relation"] == "contests"}
        for row in rows:
            row["status"] = (
                "superseded"
                if row["id"] in superseded
                else "contested"
                if row["id"] in contested
                else "active"
            )
        return [row for row in rows if topic is None or row["topic"] == topic]
    finally:
        connection.close()


def integrity(database: str | Path) -> dict:
    connection = connect(database, readonly=True)
    try:
        connection.execute("BEGIN")
        errors = []
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            errors.append("sqlite_integrity")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("foreign_keys")
        observations = connection.execute(
            "SELECT count(*) FROM observations"
        ).fetchone()[0]
        evidence = connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
        if observations != evidence:
            errors.append("receipt_coverage")
        for row in connection.execute(
            "SELECT o.*,e.digest FROM observations o JOIN evidence e ON e.observation_id=o.id"
        ):
            material = {
                "session": row["session_id"],
                "source": row["source_id"],
                "kind": row["kind"],
                "data": json.loads(row["data"]),
            }
            if (
                hashlib.sha256(canonical(material).encode()).hexdigest()
                != row["digest"]
            ):
                errors.append("receipt_integrity")
        return {"ok": not errors, "errors": errors}
    finally:
        connection.close()

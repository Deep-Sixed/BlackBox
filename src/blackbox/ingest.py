"""Transactional capture with append-only lifecycle and correction records."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

from ._signals import (
    ClaimConflict,
    MissingRecord,
    ObservationIssue,
    ObservationRejected,
    RequestConflict,
)
from .db import connect, now, transaction
from .integrity import append_receipt
from .models import Capture, Claim, EvidenceLink, canonical, identity
from .provenance import collect_git

LIFECYCLE = ("RESERVED", "COMMITTED", "FAILED_RETRYABLE")


def event(connection, session: str, kind: str, entity: str) -> str:
    ordinal = connection.execute(
        "SELECT count(*) FROM events WHERE session_id=?", (session,)
    ).fetchone()[0]
    key = identity("evt", [session, ordinal, kind, entity])
    connection.execute(
        "INSERT INTO events(id,session_id,kind,entity_id,recorded_at) VALUES (?,?,?,?,?)",
        (key, session, kind, entity, now()),
    )
    append_receipt(connection, "events", key)
    return key


def state(connection, session: str) -> str | None:
    row = connection.execute(
        "SELECT kind FROM events WHERE session_id=? AND kind IN (?,?,?) "
        "ORDER BY sequence DESC LIMIT 1",
        (session, *LIFECYCLE),
    ).fetchone()
    return row[0] if row else None


def source(connection, session: str, name: str, authority: str) -> str:
    key = identity("src", [session, name, authority])
    cursor = connection.execute(
        "INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (key, session, name, authority),
    )
    if cursor.rowcount:
        append_receipt(connection, "sources", key)
    return key


def observation(
    connection,
    session: str,
    source_id: str,
    kind: str,
    data: dict,
    *,
    local: bool = False,
) -> str:
    material = {"session": session, "source": source_id, "kind": kind, "data": data}
    key = identity("obs", material)
    cursor = connection.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (key, session, source_id, kind, canonical(data), now()),
    )
    if cursor.rowcount:
        append_receipt(connection, "observations", key)
        digest = hashlib.sha256(canonical(material).encode()).hexdigest()
        evidence_id = identity("evd", [key, digest])
        connection.execute(
            "INSERT INTO evidence VALUES (?,?,?,?)",
            (evidence_id, key, digest, "locally_observed" if local else "unverified"),
        )
        append_receipt(connection, "evidence", evidence_id)
        event(connection, session, "OBSERVATION", key)
    return key


def claim_row(
    connection,
    session: str,
    claim: Claim,
    target: str | None = None,
    relation: str | None = None,
) -> str:
    if target is not None:
        row = connection.execute(
            "SELECT session_id FROM claims WHERE id=?", (target,)
        ).fetchone()
        if row is None:
            raise MissingRecord("unknown correction target")
    source_id = source(connection, session, claim.source, "caller_asserted")
    key = identity("clm", [session, claim.model_dump(), target, relation])
    try:
        cursor = connection.execute(
            "INSERT INTO claims VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (
                key,
                session,
                source_id,
                claim.topic,
                claim.statement,
                target if relation != "retracts" else None,
                relation if relation != "retracts" else None,
            ),
        )
    except sqlite3.IntegrityError as error:
        if (
            relation == "supersedes"
            and error.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        ):
            raise ClaimConflict("claim already superseded") from None
        raise
    if cursor.rowcount:
        append_receipt(connection, "claims", key)
        event_id = event(connection, session, "CLAIM", key)
        if target is not None:
            relation_id = identity("rel", [key, target, relation])
            connection.execute(
                "INSERT INTO claim_relations VALUES (?,?,?,?,?)",
                (relation_id, key, target, relation, event_id),
            )
            append_receipt(connection, "claim_relations", relation_id)
    return key


def ingest(
    database: str | Path,
    capture: Capture | dict,
    *,
    repo: str | Path | None = None,
    baseline: str | None = None,
    host: bool = False,
) -> dict:
    """Record a capture. `host=True` is the host lifecycle-hook code path.

    Its observations get `host_reported` authority; it carries no claims or
    artifacts, and binds its request ID to that path so a caller submission
    with the same ID conflicts instead of being taken for the host's report.
    """
    # Revalidate even model instances: do not trust model_construct or mutable lists.
    capture = Capture.model_validate(
        capture.model_dump() if isinstance(capture, Capture) else capture
    )
    if baseline is not None and repo is None:
        raise ValueError("baseline requires a repository")
    if host and (capture.claims or capture.artifacts or repo is not None):
        raise ValueError("host reports carry observations only")
    request = capture.model_dump(mode="json")
    locator = str(Path(repo).expanduser().resolve()) if repo is not None else None
    material = [request, locator, baseline]
    # Caller fingerprints keep their pre-v4 form.
    fingerprint = identity("input", [*material, "host_reported"] if host else material)
    authority = "host_reported" if host else "caller_asserted"
    session = identity("ses", capture.request_id)
    connection = connect(database)
    try:
        with transaction(connection):
            existing = connection.execute(
                "SELECT fingerprint FROM sessions WHERE id=?", (session,)
            ).fetchone()
            if existing is not None and existing[0] != fingerprint:
                raise RequestConflict("request ID already binds different input")
            if existing is None:
                connection.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?)",
                    (session, capture.request_id, fingerprint, capture.producer, now()),
                )
                append_receipt(connection, "sessions", session)
                event(connection, session, "RESERVED", session)
        try:
            with transaction(connection):
                if state(connection, session) == "COMMITTED":
                    return {
                        "session_id": session,
                        "status": "COMMITTED",
                        "duplicate": True,
                    }
                # The write lock serializes capture/retry for this local database.
                if repo is not None:
                    try:
                        data = collect_git(repo, baseline)
                    except ObservationRejected:
                        raise
                    except ValueError, OSError, subprocess.SubprocessError:
                        raise ObservationIssue(
                            "Git metadata collection failed"
                        ) from None
                    source_id = source(connection, session, "blackbox.git", "local_git")
                    observation(connection, session, source_id, "git", data, local=True)
                for item in capture.observations:
                    source_id = source(connection, session, item.source, authority)
                    observation(
                        connection,
                        session,
                        source_id,
                        item.kind,
                        item.model_dump(mode="json"),
                    )
                for item in capture.claims:
                    claim_row(connection, session, item)
                for item in capture.artifacts:
                    source_id = source(
                        connection, session, item.source, "caller_asserted"
                    )
                    key = identity("art", [session, item.model_dump()])
                    cursor = connection.execute(
                        "INSERT INTO artifacts VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                        (key, session, source_id, item.path, item.digest, "unverified"),
                    )
                    if cursor.rowcount:
                        append_receipt(connection, "artifacts", key)
                        event(connection, session, "ARTIFACT", key)
                event(connection, session, "COMMITTED", session)
        except Exception as error:
            # No exception message, raw stdout, environment, or input is persisted.
            # FAILED_RETRYABLE means the reservation remains reusable after remediation;
            # the failure row records whether an unchanged automatic retry is appropriate.
            failure_retryable = 0 if isinstance(error, ObservationRejected) else 1
            # If even this transaction fails, RESERVED still supports a retry.
            with transaction(connection):
                if state(connection, session) != "COMMITTED":
                    failure_event = event(
                        connection, session, "FAILED_RETRYABLE", session
                    )
                    connection.execute(
                        "INSERT INTO failures VALUES (?,?,?,?,?)",
                        (
                            identity("fail", failure_event),
                            session,
                            failure_event,
                            "CAPTURE_FAILED",
                            failure_retryable,
                        ),
                    )
                    append_receipt(
                        connection, "failures", identity("fail", failure_event)
                    )
            raise
        return {"session_id": session, "status": "COMMITTED", "duplicate": False}
    finally:
        connection.close()


def append_claim(
    database: str | Path,
    session: str,
    claim: Claim | dict,
    *,
    target: str | None = None,
    relation: str | None = None,
) -> str:
    claim = Claim.model_validate(
        claim.model_dump() if isinstance(claim, Claim) else claim
    )
    if (target is None) != (relation is None) or relation not in (
        None,
        "supersedes",
        "contests",
        "retracts",
    ):
        raise ValueError("correction requires a target and supported relation")
    connection = connect(database)
    try:
        with transaction(connection):
            current = state(connection, session)
            if current is None:
                raise MissingRecord("unknown session")
            if current != "COMMITTED":
                raise RequestConflict("claim requires a committed session")
            return claim_row(connection, session, claim, target, relation)
    finally:
        connection.close()


def link_evidence(database, session, link: EvidenceLink):
    connection = connect(database)
    try:
        with transaction(connection):
            current = state(connection, session)
            if current is None:
                raise MissingRecord("unknown session")
            if current != "COMMITTED":
                raise RequestConflict("link requires a committed session")
            table = {
                "observation": "observations",
                "evidence": "evidence",
                "artifact": "artifacts",
            }[link.record_type]
            for name, key in (
                ("claims", link.claim_id),
                (table, link.evidence_record_id),
            ):
                if (
                    connection.execute(
                        f"SELECT 1 FROM {name} WHERE id=?", (key,)
                    ).fetchone()
                    is None
                ):
                    raise MissingRecord("unknown link reference")
            key = identity("link", [session, link.model_dump()])
            if connection.execute(
                "SELECT 1 FROM evidence_links WHERE id=?", (key,)
            ).fetchone():
                return key
            source_id = source(connection, session, link.source, "caller_asserted")
            event_id = event(connection, session, "EVIDENCE_LINK", key)
            timestamp = connection.execute(
                "SELECT recorded_at FROM events WHERE id=?", (event_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO evidence_links VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    session,
                    source_id,
                    link.claim_id,
                    link.evidence_record_id
                    if link.record_type == "observation"
                    else None,
                    link.evidence_record_id if link.record_type == "evidence" else None,
                    link.evidence_record_id if link.record_type == "artifact" else None,
                    link.relation,
                    timestamp,
                ),
            )
            append_receipt(connection, "evidence_links", key)
            return key
    finally:
        connection.close()

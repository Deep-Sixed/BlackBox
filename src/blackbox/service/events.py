"""API v1 event shapes and their mapping onto BlackBox captures.

An event is an observation (schema v4/v5) in a capture whose producer is the
authenticated principal. Nothing here bypasses the strict capture models: every
request still passes `Capture` validation, including the secret-shape scan.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, field_validator

from ..models import (
    EVENT_FIELDS,
    OUTCOMES,
    SEVERITIES,
    Capture,
    Digest,
    Identifier,
    StrictModel,
    UtcTimestamp,
    identity,
    parse_utc,
)

MAX_BATCH = 1000
MAX_PAGE = 500
DEFAULT_PAGE = 100
# A producer's clock may run slightly ahead of BlackBox's; beyond this it is wrong.
FUTURE_SKEW = timedelta(minutes=5)
# Request IDs are `<principal>:<idempotency key>` and must stay an Identifier.
IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,94}$")]
EVENT_KINDS = ("command", "test", "activity")


class EventIn(StrictModel):
    source: Identifier
    type: Identifier
    kind: Literal[EVENT_KINDS] = "activity"
    outcome: Literal[OUTCOMES] | None = None
    severity: Literal[SEVERITIES] | None = None
    occurred_at: UtcTimestamp | None = None
    correlation_id: Identifier | None = None
    exit_code: int | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    content_digest: Digest | None = None

    @field_validator("occurred_at")
    @classmethod
    def real_instant(cls, value):
        if value is not None:
            parse_utc(value)
        return value


class EventSubmission(EventIn):
    idempotency_key: IdempotencyKey


class BatchSubmission(StrictModel):
    idempotency_key: IdempotencyKey
    events: Annotated[list[EventIn], Field(min_length=1, max_length=MAX_BATCH)]


def normalize_instant(value: str) -> str:
    """One fixed-width form, so stored instants compare correctly as text."""
    return parse_utc(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def recorded_form(value: str) -> str:
    """The form BlackBox uses for `recorded_at` (`db.now()`)."""
    return parse_utc(value).astimezone(UTC).isoformat(timespec="microseconds")


def to_observation(event: EventIn, *, now: datetime) -> dict:
    data = {
        "source": event.source,
        "kind": event.kind,
        "name": event.type,
        "exit_code": event.exit_code,
        "duration_ms": event.duration_ms,
        "content_digest": event.content_digest,
        "outcome": event.outcome,
        "severity": event.severity,
        "correlation_id": event.correlation_id,
        "occurred_at": None,
    }
    if event.occurred_at is not None:
        if parse_utc(event.occurred_at) > now + FUTURE_SKEW:
            raise ValueError("occurred_at is in the future")
        data["occurred_at"] = normalize_instant(event.occurred_at)
    return {k: v for k, v in data.items() if not (k in EVENT_FIELDS and v is None)}


def request_id(principal: str, key: str) -> str:
    return f"{principal}:{key}"


def build_capture(principal: str, key: str, events: list[EventIn], *, now) -> dict:
    capture = {
        "request_id": request_id(principal, key),
        "producer": principal,
        "observations": [to_observation(e, now=now) for e in events],
    }
    Capture.model_validate(capture)
    return capture


def observation_ids(capture: dict, authority: str = "caller_asserted") -> list[str]:
    """The deterministic ids `ingest` gives a capture's caller observations.

    Mirrors `ingest.observation`; `tests/test_service.py` pins the two together.
    """
    validated = Capture.model_validate(capture)
    session = identity("ses", validated.request_id)
    ids = []
    for ordinal, item in enumerate(validated.observations):
        source_id = identity("src", [session, item.source, authority])
        material = {
            "session": session,
            "source": source_id,
            "kind": item.kind,
            "data": item.model_dump(mode="json"),
        }
        ids.append(identity("obs", [material, f"input:{ordinal}"]))
    return ids


# --- queries --------------------------------------------------------------------


class EventQuery(StrictModel):
    since: UtcTimestamp | None = None
    until: UtcTimestamp | None = None
    time: Literal["recorded", "occurred"] = "recorded"
    source: Identifier | None = None
    producer: Identifier | None = None
    type: Identifier | None = None
    kind: Literal[EVENT_KINDS] | None = None
    severity: Literal[SEVERITIES] | None = None
    outcome: Literal[OUTCOMES] | None = None
    correlation_id: Identifier | None = None
    authority: Literal["caller_asserted", "host_reported"] | None = None
    order: Literal["asc", "desc"] = "asc"
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE
    cursor: Annotated[str, Field(max_length=512)] | None = None


def encode_cursor(recorded_at: str, event_id: str) -> str:
    raw = json.dumps([recorded_at, event_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        recorded_at, event_id = json.loads(raw)
    except ValueError, TypeError:
        raise ValueError("invalid cursor") from None
    if not (
        isinstance(recorded_at, str)
        and isinstance(event_id, str)
        and len(recorded_at) <= 64
        and event_id.startswith("obs_")
        and len(event_id) == 68
    ):
        raise ValueError("invalid cursor")
    return recorded_at, event_id


EVENT_COLUMNS = (
    "SELECT o.id, o.session_id, o.kind, o.data, o.recorded_at, "
    "s.identity AS source, s.authority, se.producer, se.request_id, "
    "ev.verification FROM observations o "
    "JOIN sources s ON s.id = o.source_id "
    "JOIN sessions se ON se.id = o.session_id "
    "LEFT JOIN evidence ev ON ev.observation_id = o.id "
)
COMMITTED = (
    "EXISTS (SELECT 1 FROM events c WHERE c.session_id = o.session_id "
    "AND c.kind = 'COMMITTED')"
)


def build_query(query: EventQuery, readable: frozenset[str] | None):
    """Return SQL and parameters. Every value is a bound parameter."""
    where = [COMMITTED, "o.kind IN ('command','test','activity')"]
    parameters: list = []
    if readable is not None:
        if not readable:
            where.append("0")
        else:
            where.append(f"se.producer IN ({','.join('?' * len(readable))})")
            parameters.extend(sorted(readable))
    if query.time == "recorded":
        column = "o.recorded_at"
        bound = recorded_form
    else:
        column = "json_extract(o.data, '$.occurred_at')"
        bound = normalize_instant
    if query.since is not None:
        where.append(f"{column} >= ?")
        parameters.append(bound(query.since))
    if query.until is not None:
        where.append(f"{column} < ?")
        parameters.append(bound(query.until))
    exact = {
        "s.identity": query.source,
        "se.producer": query.producer,
        "json_extract(o.data, '$.name')": query.type,
        "o.kind": query.kind,
        "json_extract(o.data, '$.severity')": query.severity,
        "json_extract(o.data, '$.outcome')": query.outcome,
        "json_extract(o.data, '$.correlation_id')": query.correlation_id,
        "s.authority": query.authority,
    }
    for expression, value in exact.items():
        if value is not None:
            where.append(f"{expression} = ?")
            parameters.append(value)
    descending = query.order == "desc"
    if query.cursor is not None:
        recorded_at, event_id = decode_cursor(query.cursor)
        where.append(f"(o.recorded_at, o.id) {'<' if descending else '>'} (?, ?)")
        parameters.extend([recorded_at, event_id])
    direction = "DESC" if descending else "ASC"
    sql = (
        EVENT_COLUMNS
        + "WHERE "
        + " AND ".join(where)
        + f" ORDER BY o.recorded_at {direction}, o.id {direction} LIMIT ?"
    )
    parameters.append(query.limit + 1)
    return sql, parameters


def event_view(row) -> dict:
    data = json.loads(row["data"])
    return {
        "event_id": row["id"],
        "session_id": row["session_id"],
        "request_id": row["request_id"],
        "producer": row["producer"],
        "source": row["source"],
        "authority": row["authority"],
        "verification": row["verification"],
        "kind": row["kind"],
        "type": data.get("name"),
        "outcome": data.get("outcome"),
        "severity": data.get("severity"),
        "occurred_at": data.get("occurred_at"),
        "recorded_at": row["recorded_at"],
        "correlation_id": data.get("correlation_id"),
        "exit_code": data.get("exit_code"),
        "duration_ms": data.get("duration_ms"),
        "content_digest": data.get("content_digest"),
    }

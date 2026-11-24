"""Transport-independent BlackBox service: one persistence path for every client.

Every write goes through the public `blackbox` API (`capture`), so request-ID
fingerprints, the receipt chain and append-only storage behave exactly as for
library callers. The transport (`daemon.py`) supplies the caller's token and
peer uid and serializes writes on one thread; this module decides what the
caller may do and turns every outcome into an explicit status.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter
from pydantic import ValidationError as ModelValidationError

from .. import __version__
from .. import api as bb
from .. import db as _db
from ..errors import (
    BlackBoxError,
    BusyError,
    ConflictError,
    DatabaseError,
    NotFoundError,
    ObservationError,
    ObservationRejectedError,
    ValidationError,
)
from ..models import Capture, Identifier
from ..schema import VERSION
from .auth import Forbidden, Principal, Registry, Unauthorized
from .events import (
    BatchSubmission,
    EventQuery,
    EventSubmission,
    build_capture,
    build_query,
    encode_cursor,
    event_view,
    observation_ids,
    request_id,
)
from .metrics import Metrics, RateLimiter

MAX_REQUEST_ID = 128
EVENT_ID = TypeAdapter(Identifier)


@dataclass
class Outcome:
    status: int
    body: dict | str
    headers: dict[str, str] = field(default_factory=dict)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, *, retry_after: int | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def _translate(error: BaseException) -> ServiceError:
    if isinstance(error, ServiceError):
        return error
    if isinstance(error, Unauthorized):
        return ServiceError(401, "unauthorized")
    if isinstance(error, Forbidden):
        return ServiceError(403, "forbidden")
    if isinstance(error, (ModelValidationError, ValidationError, ValueError)):
        return ServiceError(422, "invalid_input")
    if isinstance(error, NotFoundError):
        return ServiceError(404, "not_found")
    if isinstance(error, BusyError):
        return ServiceError(503, error.code, retry_after=1)
    if isinstance(error, ConflictError):
        return ServiceError(409, error.code)
    if isinstance(error, ObservationRejectedError):
        return ServiceError(422, error.code)
    if isinstance(error, ObservationError):
        return ServiceError(503, error.code, retry_after=5)
    if isinstance(error, DatabaseError):
        return ServiceError(503, error.code, retry_after=5)
    if isinstance(error, BlackBoxError):
        return ServiceError(500, error.code)
    if isinstance(error, sqlite3.Error | OSError):
        return ServiceError(503, "database_unavailable", retry_after=5)
    return ServiceError(500, "internal_error")


def namespaced(principal: str, value: str) -> str:
    """`<principal>:<value>`, hashed down if it would exceed an Identifier."""
    candidate = f"{principal}:{value}"
    if len(candidate) <= MAX_REQUEST_ID:
        return candidate
    return f"{principal}:h-{hashlib.sha256(value.encode()).hexdigest()}"


class Service:
    def __init__(
        self,
        database: str | Path,
        registry: Registry,
        *,
        repositories: Iterable[str | Path] = (),
        metrics: Metrics | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.database = Path(database)
        self.registry = registry
        self.repositories = tuple(Path(r).resolve() for r in repositories)
        self.metrics = metrics or Metrics()
        self.limiter = RateLimiter()
        self.clock = clock
        self.ready_reason: str | None = "not_started"

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Open (initialize or migrate) the store and verify it before serving."""
        if self.repositories:
            # Git observation for allowlisted repositories is sandboxed or nothing.
            os.environ["BLACKBOX_GIT_SANDBOX"] = "required"
        try:
            bb.initialize(self.database)
            result = bb.check_integrity(self.database)
        except BlackBoxError as error:
            self.ready_reason = error.code
            return
        self.ready_reason = None if result.ok else "integrity_failure"
        self.refresh_gauges()

    def reload(self, registry: Registry) -> None:
        self.registry = registry
        self.metrics.inc("blackbox_config_reloads_total")

    def refresh_gauges(self) -> None:
        try:
            size = sum(
                p.stat().st_size
                for p in (
                    self.database,
                    self.database.with_name(self.database.name + "-wal"),
                )
                if p.exists()
            )
            self.metrics.set("blackbox_db_bytes", size)
            free = os.statvfs(self.database.parent)
            self.metrics.set(
                "blackbox_db_filesystem_free_bytes", free.f_bavail * free.f_frsize
            )
            self.metrics.set(
                "blackbox_chain_head_sequence",
                bb.get_chain_head(self.database).sequence,
            )
        except OSError, BlackBoxError:
            self.metrics.inc("blackbox_gauge_refresh_failures_total")

    # --- authorization --------------------------------------------------------

    def authorize(self, token: str | None, uid: int | None, scope: str) -> Principal:
        try:
            principal = self.registry.authenticate(token, uid)
            principal.require(scope)
        except Unauthorized:
            self.metrics.inc("blackbox_rejected_total", reason="unauthorized")
            raise
        except Forbidden:
            self.metrics.inc("blackbox_rejected_total", reason="forbidden")
            raise
        kind = "read" if scope in ("events:read", "metrics:read") else "write"
        rate = principal.read_rate if kind == "read" else principal.write_rate
        if not self.limiter.allow((principal.name, kind), rate):
            self.metrics.inc("blackbox_rejected_total", reason="rate_limited")
            raise ServiceError(429, "rate_limited", retry_after=1)
        return principal

    def _require_ready(self) -> None:
        if self.ready_reason is not None:
            raise ServiceError(503, "not_ready", retry_after=5)

    # --- writes ---------------------------------------------------------------

    def _record(self, principal: Principal, capture: dict, count: int) -> Outcome:
        self._require_ready()
        result = bb.capture(self.database, capture)
        ids = observation_ids(capture)
        status = 200 if result.duplicate else 201
        if not result.duplicate:
            self.metrics.inc(
                "blackbox_events_ingested_total", count, principal=principal.name
            )
        else:
            self.metrics.inc("blackbox_duplicates_total", principal=principal.name)
        return Outcome(
            status,
            {
                "session_id": result.session_id,
                "event_ids": ids,
                "duplicate": result.duplicate,
            },
        )

    def submit_event(self, principal: Principal, body: dict) -> Outcome:
        submission = EventSubmission.model_validate(body)
        capture = build_capture(
            principal.name, submission.idempotency_key, [submission], now=self.clock()
        )
        outcome = self._record(principal, capture, 1)
        outcome.body["event_id"] = outcome.body["event_ids"][0]
        return outcome

    def submit_batch(self, principal: Principal, body: dict) -> Outcome:
        batch = BatchSubmission.model_validate(body)
        capture = build_capture(
            principal.name, batch.idempotency_key, batch.events, now=self.clock()
        )
        return self._record(principal, capture, len(batch.events))

    def host_report(self, principal: Principal, body: dict) -> Outcome:
        """Record a host lifecycle-hook report; optionally snapshot an allowed repo."""
        self._require_ready()
        if not isinstance(body, dict) or set(body) - {"capture", "git_cwd"}:
            raise ServiceError(422, "invalid_input")
        report = Capture.model_validate(body.get("capture"))
        if report.claims or report.artifacts:
            raise ServiceError(422, "invalid_input")
        capture = report.model_dump(mode="json")
        capture["request_id"] = namespaced(principal.name, report.request_id)
        capture["producer"] = principal.name
        result = bb._capture_host_report(self.database, capture)
        ids = observation_ids(capture, authority="host_reported")
        response = {
            "session_id": result.session_id,
            "event_ids": ids,
            "duplicate": result.duplicate,
            "git": "not_requested",
        }
        if not result.duplicate:
            self.metrics.inc(
                "blackbox_events_ingested_total",
                len(ids),
                principal=principal.name,
            )
        cwd = body.get("git_cwd")
        if cwd is not None:
            response["git"] = self._git_snapshot(principal, capture, cwd)
        return Outcome(200 if result.duplicate else 201, response)

    def _allowed_repository(self, cwd: object) -> Path | None:
        if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\0" in cwd:
            return None
        try:
            resolved = Path(cwd).resolve(strict=True)
        except OSError, RuntimeError:
            return None
        for root in self.repositories:
            if resolved == root or resolved.is_relative_to(root):
                return root
        return None

    def _git_snapshot(self, principal: Principal, capture: dict, cwd) -> str:
        root = self._allowed_repository(cwd)
        if root is None:
            self.metrics.inc("blackbox_rejected_total", reason="git_not_allowlisted")
            return "not_allowlisted"
        snapshot = {
            "request_id": namespaced(principal.name, capture["request_id"] + ":git"),
            "producer": principal.name,
        }
        try:
            bb.capture(self.database, snapshot, repo=root)
        except ObservationError:
            self.metrics.inc("blackbox_ingest_failures_total", reason="git")
            return "failed"
        return "recorded"

    # --- reads ----------------------------------------------------------------

    def get_event(self, principal: Principal, event_id: str) -> Outcome:
        EVENT_ID.validate_python(event_id, strict=True)
        query = EventQuery(limit=1)
        sql, parameters = build_query(query, principal.readable())
        sql = sql.replace(" ORDER BY", " AND o.id = ? ORDER BY", 1)
        parameters.insert(len(parameters) - 1, event_id)
        with closing(_db.connect(self.database, readonly=True)) as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            # Unknown and unreadable look the same.
            raise ServiceError(404, "not_found")
        return Outcome(200, event_view(row))

    def list_events(self, principal: Principal, parameters: dict) -> Outcome:
        parameters = dict(parameters)
        limit = parameters.get("limit")
        if isinstance(limit, str):
            if not limit.isdigit() or len(limit) > 4:
                raise ServiceError(422, "invalid_input")
            parameters["limit"] = int(limit)
        query = EventQuery.model_validate(parameters)
        sql, values = build_query(query, principal.readable())
        with closing(_db.connect(self.database, readonly=True)) as connection:
            rows = connection.execute(sql, values).fetchall()
        more = len(rows) > query.limit
        rows = rows[: query.limit]
        events = [event_view(row) for row in rows]
        cursor = (
            encode_cursor(rows[-1]["recorded_at"], rows[-1]["id"])
            if more and rows
            else None
        )
        return Outcome(200, {"events": events, "next_cursor": cursor})

    def integrity(self, principal: Principal, parameters: dict) -> Outcome:
        anchor = None
        if parameters:
            if set(parameters) - {"sequence", "digest"}:
                raise ServiceError(422, "invalid_input")
            sequence = parameters.get("sequence")
            if (
                not isinstance(sequence, str)
                or not sequence.isdigit()
                or len(sequence) > 19
            ):
                raise ServiceError(422, "invalid_input")
            anchor = {"sequence": int(sequence), "digest": parameters.get("digest")}
        result = bb.check_integrity(self.database, anchor=anchor)
        self.metrics.set("blackbox_integrity_ok", 1 if result.ok else 0)
        return Outcome(200, result.model_dump(mode="json"))

    def chain_head(self, principal: Principal) -> Outcome:
        return Outcome(200, bb.get_chain_head(self.database).model_dump(mode="json"))

    # --- unauthenticated status -------------------------------------------------

    def health(self) -> Outcome:
        return Outcome(200, {"status": "ok"})

    def ready(self) -> Outcome:
        if self.ready_reason is None:
            return Outcome(200, {"status": "ready"})
        return Outcome(503, {"status": "not_ready", "reason": self.ready_reason})

    def version(self) -> Outcome:
        return Outcome(
            200,
            {"version": __version__, "schema_version": VERSION, "api": "v1"},
        )

    # --- dispatch ---------------------------------------------------------------

    def handle(
        self,
        method: str,
        route: str,
        *,
        token: str | None,
        uid: int | None,
        body: dict | None = None,
        parameters: dict | None = None,
        event_id: str | None = None,
    ) -> Outcome:
        """Authorize and run one operation; never raises, never echoes input."""
        started = time.monotonic()
        try:
            outcome = self._dispatch(
                method, route, token, uid, body, parameters or {}, event_id
            )
        except Exception as error:  # noqa: BLE001 - boundary: every failure is a bounded status
            failure = _translate(error)
            if failure.status >= 500:
                self.metrics.inc("blackbox_ingest_failures_total", reason=failure.code)
            elif failure.status not in (401, 403, 429):
                self.metrics.inc("blackbox_rejected_total", reason=failure.code)
            headers = {}
            if failure.retry_after is not None:
                headers["Retry-After"] = str(failure.retry_after)
            if failure.status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            outcome = Outcome(failure.status, {"error": failure.code}, headers)
        self.metrics.inc(
            "blackbox_requests_total", route=route, status=str(outcome.status)
        )
        self.metrics.observe(time.monotonic() - started)
        return outcome

    def _dispatch(self, method, route, token, uid, body, parameters, event_id):
        if route == "/api/v1/health":
            return self.health()
        if route == "/api/v1/ready":
            return self.ready()
        if route == "/api/v1/version":
            return self.version()
        if route == "/metrics":
            self.authorize(token, uid, "metrics:read")
            self.refresh_gauges()
            return Outcome(200, self.metrics.render())
        if route == "/api/v1/events" and method == "POST":
            principal = self.authorize(token, uid, "events:write")
            return self.submit_event(principal, body)
        if route == "/api/v1/events/batch":
            principal = self.authorize(token, uid, "events:write")
            return self.submit_batch(principal, body)
        if route == "/api/v1/host-reports":
            principal = self.authorize(token, uid, "host:report")
            return self.host_report(principal, body)
        if route == "/api/v1/events" and method == "GET":
            principal = self.authorize(token, uid, "events:read")
            return self.list_events(principal, parameters)
        if route == "/api/v1/events/{event_id}":
            principal = self.authorize(token, uid, "events:read")
            return self.get_event(principal, event_id)
        if route == "/api/v1/integrity":
            principal = self.authorize(token, uid, "events:read")
            return self.integrity(principal, parameters)
        if route == "/api/v1/chain-head":
            principal = self.authorize(token, uid, "events:read")
            return self.chain_head(principal)
        raise ServiceError(404, "not_found")


__all__ = ["Outcome", "Service", "ServiceError", "request_id"]

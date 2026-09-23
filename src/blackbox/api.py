"""Supported operation boundary. Only names exported by blackbox are public."""

import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from functools import wraps
from pathlib import Path

from pydantic import TypeAdapter
from pydantic import ValidationError as ModelValidationError

from . import _signals
from . import db as _db
from . import ingest as _ingest
from . import query as _query
from .errors import (
    BlackBoxError,
    BusyError,
    ConflictError,
    DatabaseError,
    IntegrityError,
    MigrationRequiredError,
    NotFoundError,
    ObservationError,
    ObservationRejectedError,
    SchemaError,
    ValidationError,
)
from .integrity import (
    anchor_errors,
    chain_head,
    evidence_errors,
    receipt_findings,
    relationship_errors,
)
from .models import (
    Capture,
    ChainAnchor,
    Claim,
    EvidenceLink,
    Identifier,
    safe_strings,
)
from .results import (
    CaptureResult,
    ChainHead,
    ClaimRelationView,
    ClaimResult,
    ClaimView,
    EvidenceLinkResult,
    EvidenceLinkView,
    InitializationResult,
    IntegrityResult,
    SessionView,
    TimelineEvent,
)
from .schema import VERSION

__all__ = [
    "append_claim",
    "capture",
    "check_integrity",
    "get_chain_head",
    "get_claim_relations",
    "get_claims",
    "get_evidence_links",
    "get_session",
    "get_timeline",
    "initialize",
    "link_evidence",
]


def _boundary[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except BlackBoxError:
            raise
        except _signals.MigrationRequired:
            raise MigrationRequiredError() from None
        except _signals.SchemaIssue:
            raise SchemaError() from None
        except _signals.EvidenceIssue:
            raise IntegrityError() from None
        except _signals.DatabaseIssue:
            raise DatabaseError() from None
        except _signals.RequestConflict, _signals.ClaimConflict:
            raise ConflictError() from None
        except _signals.MissingRecord:
            raise NotFoundError() from None
        except _signals.ObservationRejected:
            raise ObservationRejectedError() from None
        except _signals.ObservationIssue:
            raise ObservationError() from None
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise BusyError() from None
            if code in (
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
                sqlite3.SQLITE_CONSTRAINT,
            ):
                raise IntegrityError() from None
            raise DatabaseError() from None
        except OSError:
            raise DatabaseError() from None
        except subprocess.SubprocessError:
            raise ObservationError() from None
        except ValueError, TypeError, OverflowError:
            raise ValidationError() from None

    return call


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
        raise ValidationError()
    try:
        return Path(value).expanduser()
    except RuntimeError:
        raise ValidationError() from None


def _identifier(value):
    TypeAdapter(Identifier).validate_python(value, strict=True)
    safe_strings(value)
    return value


def _through(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise ValidationError()
    return value


def _input(model, value):
    if not isinstance(value, Mapping):
        raise ValidationError()
    return model.model_validate(dict(value))


def _output(model, value):
    try:
        return model.model_validate(value)
    except ModelValidationError:
        raise IntegrityError() from None


@_boundary
def initialize(database: str | Path) -> InitializationResult:
    """Create a v3 store or atomically migrate a supported older store."""
    _db.connect(_path(database)).close()
    return InitializationResult(schema_version=VERSION)


@_boundary
def capture(
    database: str | Path,
    request: Mapping[str, object],
    *,
    repo: str | Path | None = None,
    baseline: str | None = None,
) -> CaptureResult:
    """Validate and record a request; the writer may initialize/migrate storage."""
    _path(database)
    validated = _input(Capture, request)
    if repo is not None:
        _path(repo)
    if baseline is not None and (
        repo is None
        or not isinstance(baseline, str)
        or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", baseline) is None
    ):
        raise ValidationError()
    return _output(
        CaptureResult, _ingest.ingest(database, validated, repo=repo, baseline=baseline)
    )


@_boundary
def append_claim(
    database: str | Path,
    session: str,
    claim: Mapping[str, object],
    *,
    target: str | None = None,
    relation: str | None = None,
) -> ClaimResult:
    """Append a claim or correction. A writer may migrate; history stays immutable."""
    _path(database)
    _identifier(session)
    validated = _input(Claim, claim)
    if target is not None:
        _identifier(target)
    if (target is None) != (relation is None) or relation not in (
        None,
        "supersedes",
        "contests",
        "retracts",
    ):
        raise ValidationError()
    key = _ingest.append_claim(
        database, session, validated, target=target, relation=relation
    )
    return ClaimResult(claim_id=key)


@_boundary
def get_session(database: str | Path, session: str) -> SessionView:
    """Reconstruct a detached session view without initializing or migrating."""
    return _output(
        SessionView, _query.reconstruct(_path(database), _identifier(session))
    )


@_boundary
def get_timeline(
    database: str | Path, *, through: int | None = None
) -> tuple[TimelineEvent, ...]:
    """Read global event order, optionally through an inclusive sequence cutoff."""
    return tuple(
        _output(TimelineEvent, row)
        for row in _query.timeline(_path(database), through=_through(through))
    )


@_boundary
def get_claims(
    database: str | Path,
    *,
    through: int | None = None,
    topic: str | None = None,
) -> tuple[ClaimView, ...]:
    """Read claims with derived status as of an inclusive event cutoff."""
    if topic is not None:
        _identifier(topic)
    return tuple(
        _output(ClaimView, row)
        for row in _query.claims(
            _path(database), through=_through(through), topic=topic
        )
    )


@_boundary
def check_integrity(
    database: str | Path, *, anchor: Mapping[str, object] | None = None
) -> IntegrityResult:
    """Report inconsistencies; failures to open/validate raise bounded errors.

    An anchor is a chain head exported earlier by `get_chain_head` and kept
    outside the database; it detects rewrites and truncation up to that point.
    """
    path = _path(database)
    pinned = _input(ChainAnchor, anchor) if anchor is not None else None
    connection = _db.connect(path, readonly=True)
    try:
        connection.execute("BEGIN")
        chain, first_broken = receipt_findings(connection)
        errors = evidence_errors(connection) | chain | relationship_errors(connection)
        if pinned is not None:
            errors |= anchor_errors(connection, pinned.sequence, pinned.digest)
        return IntegrityResult(
            ok=not errors,
            schema_version=VERSION,
            errors=tuple(sorted(errors)),
            first_broken_sequence=first_broken,
        )
    finally:
        connection.close()


@_boundary
def get_chain_head(database: str | Path) -> ChainHead:
    """Export the receipt chain's current head for safekeeping outside BlackBox.

    Reads only; it does not verify the chain. Pass the head back to
    `check_integrity(anchor=...)` later to detect rewrites or truncation.
    """
    connection = _db.connect(_path(database), readonly=True)
    try:
        return _output(ChainHead, chain_head(connection))
    finally:
        connection.close()


@_boundary
def link_evidence(
    database: str | Path, session: str, link: Mapping[str, object]
) -> EvidenceLinkResult:
    """Record an attributed assertion about existing evidence; never adjudicate it."""
    path = _path(database)
    origin = _identifier(session)
    validated = _input(EvidenceLink, link)
    return EvidenceLinkResult(link_id=_ingest.link_evidence(path, origin, validated))


@_boundary
def get_evidence_links(
    database: str | Path,
    *,
    claim_id: str | None = None,
    session: str | None = None,
    through: int | None = None,
) -> tuple[EvidenceLinkView, ...]:
    """Read evidence assertions, filtered by claim, origin session or local order."""
    for value in (claim_id, session):
        if value is not None:
            _identifier(value)
    return tuple(
        _output(EvidenceLinkView, row)
        for row in _query.evidence_links(
            _path(database),
            claim_id=claim_id,
            session=session,
            through=_through(through),
        )
    )


@_boundary
def get_claim_relations(
    database: str | Path,
    *,
    claim_id: str | None = None,
    target_id: str | None = None,
    session: str | None = None,
    through: int | None = None,
) -> tuple[ClaimRelationView, ...]:
    """Read immutable newer-to-older assertions with both originating sessions."""
    for value in (claim_id, target_id, session):
        if value is not None:
            _identifier(value)
    return tuple(
        _output(ClaimRelationView, row)
        for row in _query.claim_relations(
            _path(database),
            claim_id=claim_id,
            target_id=target_id,
            session=session,
            through=_through(through),
        )
    )

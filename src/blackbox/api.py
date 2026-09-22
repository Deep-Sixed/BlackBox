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
    SchemaError,
    ValidationError,
)
from .integrity import evidence_errors, record_errors
from .models import Capture, Claim, Identifier, safe_strings
from .results import (
    CaptureResult,
    ClaimResult,
    ClaimView,
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
    "get_claims",
    "get_session",
    "get_timeline",
    "initialize",
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
    """Create a v2 store or atomically migrate a supported older store."""
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
def check_integrity(database: str | Path) -> IntegrityResult:
    """Report inconsistencies; failures to open/validate raise bounded errors."""
    connection = _db.connect(_path(database), readonly=True)
    try:
        connection.execute("BEGIN")
        errors = evidence_errors(connection) | record_errors(connection)
        return IntegrityResult(
            ok=not errors, schema_version=VERSION, errors=tuple(sorted(errors))
        )
    finally:
        connection.close()

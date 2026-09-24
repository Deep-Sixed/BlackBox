"""Private local database, durable transactions, and strictly read-only queries."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ._signals import DatabaseIssue, MigrationRequired, SchemaIssue
from .migrations import DEFINITIONS, install, schema_digest, validate
from .schema import APPLICATION_ID, VERSION

__all__ = ["connect", "now", "schema_digest", "transaction", "verify_schema"]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _verify_database_directory(path: Path) -> None:
    """Require the immediate database directory to be private to this user."""

    info = path.stat()
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and info.st_uid != geteuid():
        raise DatabaseIssue("database directory must be owned by the current user")
    if info.st_mode & 0o022:
        raise DatabaseIssue("database directory must not be group/other writable")


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise DatabaseIssue("database symlinks are not supported")
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _verify_database_directory(path.parent)
    if not readonly:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        if path.stat().st_mode & 0o077:
            raise DatabaseIssue("database must have private permissions (0600)")
    connection = sqlite3.connect(
        path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
        uri=True,
        timeout=5,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
            verify_schema(connection)
        else:
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if application not in (0, APPLICATION_ID) or version not in (
                0,
                *DEFINITIONS,
            ):
                raise SchemaIssue("unsupported database identity or schema version")
            if (
                application == 0
                and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
            ):
                raise SchemaIssue("refusing to adopt a non-BlackBox database")
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise DatabaseIssue("WAL mode unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            with transaction(connection):
                install(connection, now())
            verify_schema(connection)
        return connection
    except BaseException:
        connection.close()
        raise


def verify_schema(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise SchemaIssue("unsupported database identity or schema version")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version in DEFINITIONS and version < VERSION:
        raise MigrationRequired(
            "schema migration required; use a writer initialization"
        )
    if version != VERSION:
        raise SchemaIssue("unsupported database identity or schema version")
    validate(connection, VERSION)


@contextmanager
def transaction(connection: sqlite3.Connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

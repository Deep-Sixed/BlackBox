"""Private local database, durable transactions, and strictly read-only queries."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ._signals import DatabaseIssue, MigrationRequired, SchemaIssue
from .migrations import DEFINITIONS, install, schema_digest, validate
from .schema import APPLICATION_ID, VERSION

__all__ = ["connect", "now", "schema_digest", "transaction", "verify_schema"]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def private_directory(directory: Path) -> None:
    """Refuse a database directory that another local user could replace.

    Whoever can write the directory can replace or unlink the database despite its
    0600 mode, or plant a WAL sidecar that SQLite replays on open. The path is
    walked as written, one component at a time, following each symlink hop
    itself: checking only the resolved path would miss a writable directory that
    holds a symlink on the way. Every directory passed through must be owned by
    this user (or root) and closed to group and other writes; one on the way may
    be shared only with the sticky bit, which stops other users renaming entries
    they do not own, so each symlink must be owned by this user (or root) too.
    Replacement between this check and SQLite's open is not prevented.
    """
    uid = os.geteuid()

    def refuse():
        raise DatabaseIssue("database directory must not be writable by other users")

    def check(info: os.stat_result, final: bool) -> None:
        if info.st_uid not in (uid, 0):
            refuse()
        if info.st_mode & 0o022 and (final or not info.st_mode & stat.S_ISVTX):
            refuse()

    current = Path("/")
    pending = list(Path(directory).absolute().parts[1:])
    check(os.lstat(current), not pending)
    hops = 0
    while pending:
        name = pending.pop(0)
        if name in ("", "."):
            continue
        if name == "..":
            current = current.parent
            check(os.lstat(current), not pending)
            continue
        candidate = current / name
        info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in (uid, 0):
                refuse()
            hops += 1
            if hops > 40:
                raise DatabaseIssue("too many symlinks in the database path")
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                current = Path("/")
                check(os.lstat(current), False)
            pending = [*target.parts[1 if target.is_absolute() else 0 :], *pending]
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise DatabaseIssue("database directory is not a directory")
        check(info, not pending)
        current = candidate


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise DatabaseIssue("database symlinks are not supported")
    if readonly:
        # Readers must not trust a database another user could have swapped in.
        private_directory(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_directory(path.parent)
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

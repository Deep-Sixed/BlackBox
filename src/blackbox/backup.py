"""SQLite-consistent backups: online backup API, verified before they count.

Never copies the live file: a file copy of a WAL database can miss committed
pages or capture a torn state. The copy is written privately, converted to a
self-contained rollback-journal file, checked with SQLite's integrity_check and
BlackBox's own receipt-chain check, then atomically renamed into place.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import api
from . import db as _db
from .errors import IntegrityError

PREFIX = "blackbox-"
SUFFIX = ".sqlite3"


def backup(database: str | Path, directory: str | Path, *, keep: int = 14) -> dict:
    if not 1 <= keep <= 3650:
        raise ValueError("keep out of range")
    directory = Path(directory).absolute()
    _db.private_directory(directory)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    final = directory / f"{PREFIX}{stamp}{SUFFIX}"
    partial = directory / f".{final.name}.partial"
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with (
            closing(_db.connect(database, readonly=True)) as source,
            closing(sqlite3.connect(partial)) as target,
        ):
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise IntegrityError()
        checked = api.check_integrity(partial)
        if not checked.ok:
            raise IntegrityError()
        head = api.get_chain_head(partial)
        with open(partial, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, final)
        _fsync_directory(directory)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    removed = prune(directory, keep)
    return {
        "path": str(final),
        "bytes": final.stat().st_size,
        "sequence": head.sequence,
        "digest": head.digest,
        "integrity": "ok",
        "pruned": removed,
    }


def prune(directory: Path, keep: int) -> int:
    copies = sorted(
        p
        for p in directory.iterdir()
        if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX) and p.is_file()
    )
    removed = 0
    for old in copies[:-keep]:
        old.unlink()
        removed += 1
    if removed:
        _fsync_directory(directory)
    return removed


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

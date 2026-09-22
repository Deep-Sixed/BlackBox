import concurrent.futures
import json
import os
import sqlite3

import pytest

from blackbox.db import connect, transaction
from blackbox.ingest import ingest
from blackbox.query import integrity, timeline
from blackbox.schema import APPLICATION_ID, VERSION


@pytest.fixture
def capture_request():
    return {
        "request_id": "sqlite-contract",
        "producer": "test-agent",
        "observations": [
            {"source": "caller", "kind": "test", "name": "sqlite-contract"}
        ],
        "claims": [
            {
                "source": "caller",
                "topic": "sqlite",
                "statement": "SQLite contract held",
            }
        ],
    }


def test_valid_existing_database_keeps_required_pragmas(tmp_path, capture_request):
    database = tmp_path / "blackbox.sqlite3"
    ingest(database, capture_request)

    connection = connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert (
            connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == VERSION
    finally:
        connection.close()

    readonly = connect(database, readonly=True)
    try:
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        readonly.close()
    assert integrity(database) == {"ok": True, "errors": []}


def test_wrong_application_id_is_rejected(tmp_path):
    database = tmp_path / "wrong-app.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA application_id=12345")
    connection.execute(f"PRAGMA user_version={VERSION}")
    connection.close()
    os.chmod(database, 0o600)

    with pytest.raises(ValueError, match="unsupported database identity"):
        connect(database)


def test_corrupt_database_is_rejected(tmp_path):
    database = tmp_path / "corrupt.sqlite3"
    database.write_bytes(b"not a sqlite database")
    os.chmod(database, 0o600)

    with pytest.raises(sqlite3.DatabaseError):
        connect(database)


def test_readers_do_not_observe_uncommitted_writer_state(tmp_path, capture_request):
    database = tmp_path / "blackbox.sqlite3"
    ingest(database, capture_request)
    before = timeline(database)

    writer = connect(database)
    try:
        with transaction(writer):
            writer.execute(
                "INSERT INTO events(id, session_id, kind, entity_id, recorded_at) "
                "VALUES ('evt_pending', ?, 'TEST_PENDING', 'entity', '2026-01-01T00:00:00+00:00')",
                (before[0]["session_id"],),
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(timeline, database).result(timeout=5) == before
        assert len(timeline(database)) == len(before) + 1
    finally:
        writer.close()


def test_retry_after_failed_capture_keeps_failure_generic(tmp_path, capture_request):
    database = tmp_path / "blackbox.sqlite3"
    broken = json.loads(json.dumps(capture_request))
    broken["claims"][0]["statement"] = "password=synthetic-only"

    with pytest.raises(ValueError):
        ingest(database, broken)
    assert not database.exists()

    result = ingest(database, capture_request)
    assert result["status"] == "COMMITTED"
    assert integrity(database)["ok"]

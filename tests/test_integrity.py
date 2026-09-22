import json
import sqlite3

import pytest

import blackbox
from blackbox.db import connect
from blackbox.ingest import ingest
from blackbox.query import integrity


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "integrity.sqlite3"
    ingest(
        path,
        {
            "request_id": "integrity",
            "producer": "test",
            "observations": [{"source": "caller", "kind": "test", "name": "checks"}],
            "claims": [{"source": "caller", "topic": "tests", "statement": "Original"}],
            "artifacts": [
                {"source": "caller", "path": "report.txt", "digest": "a" * 64}
            ],
        },
    )
    return path


def corrupt(path, table, sql):
    # Restore the exact barriers so the test exercises semantic checking,
    # rather than merely the schema validator detecting a missing trigger.
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _ in triggers:
            db.execute(f"DROP TRIGGER {name}")
        db.execute(sql)
        for _, ddl in triggers:
            db.execute(ddl)


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("sessions", "producer", "changed"),
        ("sources", "identity", "changed"),
        ("observations", "kind", "changed"),
        ("evidence", "verification", "locally_observed"),
        ("claims", "statement", "changed"),
        ("artifacts", "path", "changed.txt"),
        ("events", "kind", "changed"),
    ],
)
def test_canonical_tampering(database, table, column, value):
    corrupt(database, table, f"UPDATE {table} SET {column}='{value}'")
    result = integrity(database)
    assert not result["ok"]
    assert "record_integrity" in result["errors"]
    assert value not in json.dumps(result)


def test_failure_integrity(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("blackbox.ingest")
    path = tmp_path / "failure.sqlite3"

    def fail(*args, **kwargs):
        raise OSError("synthetic")

    monkeypatch.setattr(module, "collect_git", fail)
    with pytest.raises(blackbox.ObservationError):
        blackbox.capture(
            path, {"request_id": "failure", "producer": "test"}, repo=tmp_path
        )
    assert integrity(path)["ok"]
    corrupt(path, "failures", "UPDATE failures SET code='CHANGED'")
    assert "record_integrity" in integrity(path)["errors"]


@pytest.mark.parametrize(
    "sql,category",
    [
        ("DELETE FROM record_receipts WHERE sequence=2", "record_coverage"),
        ("DELETE FROM record_receipts WHERE sequence=2", "sequence_continuity"),
        (
            "UPDATE record_receipts SET record_id='absent' WHERE sequence=2",
            "orphan_receipt",
        ),
        (
            "UPDATE record_receipts SET digest=printf('%064d',1) WHERE sequence=2",
            "record_integrity",
        ),
        (
            "UPDATE record_receipts SET previous_digest=printf('%064d',1) WHERE sequence=2",
            "chain_integrity",
        ),
        (
            "DELETE FROM record_receipts WHERE sequence=(SELECT max(sequence) FROM record_receipts)",
            "record_coverage",
        ),
    ],
)
def test_receipt_corruption(database, sql, category):
    corrupt(database, "record_receipts", sql)
    assert category in integrity(database)["errors"]


def test_missing_record_is_orphan_receipt(database):
    corrupt(database, "claims", "DELETE FROM claims")
    assert "orphan_receipt" in integrity(database)["errors"]


def test_foreign_key_violation(database):
    corrupt(database, "artifacts", "UPDATE artifacts SET source_id='absent'")
    assert "foreign_keys" in integrity(database)["errors"]


def test_old_observation_receipt_still_checked(database):
    corrupt(database, "evidence", "UPDATE evidence SET digest=printf('%064d',1)")
    assert "receipt_integrity" in integrity(database)["errors"]


def test_receipt_missing(database):
    corrupt(database, "evidence", "DELETE FROM evidence")
    assert "receipt_coverage" in integrity(database)["errors"]


def test_duplicate_receipt_identity_blocked(database):
    db = connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO record_receipts SELECT 999,record_type,record_id,digest,previous_digest FROM record_receipts LIMIT 1"
            )
    finally:
        db.close()
    assert integrity(database)["ok"]


def test_new_writes_do_not_heal_missing_receipts(database):
    corrupt(database, "record_receipts", "DELETE FROM record_receipts WHERE sequence=2")
    ingest(database, {"request_id": "later", "producer": "test"})
    assert "record_coverage" in integrity(database)["errors"]


def test_json_material_is_canonical(database):
    with sqlite3.connect(database) as db:
        data = json.loads(db.execute("SELECT data FROM observations").fetchone()[0])
    # Whitespace/object ordering are not evidence changes.
    encoded = json.dumps(data, indent=2, sort_keys=False).replace("'", "''")
    corrupt(database, "observations", f"UPDATE observations SET data='{encoded}'")
    assert integrity(database)["ok"]


def test_schema_tampering_reports_bounded_category(database):
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER claims_no_update")
    assert integrity(database) == {
        "ok": False,
        "schema_version": None,
        "errors": ["schema_integrity"],
    }


def test_corrupt_database_check_is_bounded(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a database")
    assert integrity(path) == {
        "ok": False,
        "schema_version": None,
        "errors": ["database_unavailable"],
    }

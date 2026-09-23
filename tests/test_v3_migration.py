"""Migrate genuine released schema-v2 stores without rewriting their evidence."""

import concurrent.futures
import json
import sqlite3

import pytest
import support
from support import READ_RECORDS, UPGRADE_LEDGER, run_release

import blackbox as bb
from blackbox import migrations
from blackbox.integrity import V2_RECORD_FIELDS
from blackbox.migrations import v002


def snapshot(path):
    return support.snapshot(path, (*V2_RECORD_FIELDS, *UPGRADE_LEDGER))


def test_frozen_v2_definition_matches_actual_release(released_v2):
    assert v002.DDL == released_v2[1]["ddl"]
    assert migrations.schema_digest(2) == released_v2[1]["digest"]


def test_v2_upgrade_preserves_rows_receipt_prefix_and_event_times(
    v2_database, released_v2
):
    before = snapshot(v2_database)
    bb.initialize(v2_database)
    after = snapshot(v2_database)
    for table in V2_RECORD_FIELDS:
        assert after[table] == before[table]
    for table in UPGRADE_LEDGER:
        assert after[table][: len(before[table])] == before[table]
    for original in released_v2[1]["records"]:
        assert (
            bb.get_session(v2_database, original["session"]["id"]).model_dump(
                mode="json"
            )
            == original
        )
    relations = bb.get_claim_relations(v2_database)
    assert len(relations) == 2
    events = {e.entity_id: e for e in bb.get_timeline(v2_database) if e.kind == "CLAIM"}
    for rel in relations:
        assert rel.sequence == events[rel.claim_id].sequence
        assert rel.recorded_at == events[rel.claim_id].recorded_at
        assert rel.origin_session_id == rel.target_session_id
    assert bb.check_integrity(v2_database).ok
    bb.initialize(v2_database)
    assert snapshot(v2_database) == after


@pytest.mark.parametrize("failure", ["exception", "write_error", "final_validation"])
def test_v3_failure_rolls_back_and_actual_v2_release_still_reads(
    v2_database, released_v2, monkeypatch, failure
):
    before = snapshot(v2_database)
    original_upgrade = migrations.UPGRADES[2]
    original_validate = migrations.validate

    def fail(db):
        original_upgrade(db)
        assert db.execute("SELECT count(*) FROM claim_relations").fetchone()[0] == 2
        if failure == "write_error":
            db.execute("PRAGMA query_only=ON")
            db.execute("INSERT INTO schema_metadata VALUES (3,'test','test')")
        raise RuntimeError("injected")

    def reject_final(db, version):
        original_validate(db, version)
        if version == 4:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4
            raise RuntimeError("final validation failure")

    if failure == "final_validation":
        monkeypatch.setattr(migrations, "validate", reject_final)
    else:
        monkeypatch.setitem(migrations.UPGRADES, 2, fail)
    with pytest.raises((RuntimeError, bb.DatabaseError)):
        bb.initialize(v2_database)
    assert snapshot(v2_database) == before
    with sqlite3.connect(v2_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('claim_relations','evidence_links')"
        ).fetchall()
    old = released_v2[1]
    result = run_release(
        released_v2,
        READ_RECORDS,
        v2_database,
        json.dumps([r["session"]["id"] for r in old["records"]]),
    )
    assert json.loads(result.stdout) == old["records"]
    monkeypatch.undo()
    bb.initialize(v2_database)
    assert bb.check_integrity(v2_database).ok


def test_v2_damaged_receipt_is_rejected_without_backfill(v2_database):
    with sqlite3.connect(v2_database) as db:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='record_receipts_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER record_receipts_no_update")
        db.execute("UPDATE record_receipts SET digest=? WHERE sequence=1", ("0" * 64,))
        db.execute(ddl)
    before = snapshot(v2_database)
    with pytest.raises(bb.IntegrityError):
        bb.initialize(v2_database)
    assert snapshot(v2_database) == before


def test_v2_missing_claim_event_is_rejected_even_with_rebuilt_receipts(v2_database):
    # A self-consistent unauthenticated chain cannot legitimize invented chronology.
    from blackbox.integrity import append_receipt

    with sqlite3.connect(v2_database) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        triggers = list(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE tbl_name IN ('events','record_receipts') AND type='trigger'"
            )
        )
        for row in triggers:
            db.execute(f"DROP TRIGGER {row['name']}")
        db.execute(
            "DELETE FROM events WHERE kind='CLAIM' AND entity_id IN (SELECT id FROM claims WHERE relation='supersedes')"
        )
        db.execute("DELETE FROM record_receipts")
        for table in V2_RECORD_FIELDS:
            for row in db.execute(f"SELECT id FROM {table} ORDER BY id"):
                append_receipt(db, table, row[0])
        for row in triggers:
            db.execute(row["sql"])
    before = snapshot(v2_database)
    with pytest.raises(bb.IntegrityError):
        bb.initialize(v2_database)
    assert snapshot(v2_database) == before


def test_concurrent_v2_upgrade_happens_once(v2_database):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: bb.initialize(v2_database), range(4)))
    with sqlite3.connect(v2_database) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=3"
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT count(*) FROM claim_relations").fetchone()[0] == 2
    assert bb.check_integrity(v2_database).ok

"""Migrate genuine released schema-v3 stores: widen one CHECK, touch no record."""

import concurrent.futures
import json
import sqlite3
import subprocess

import pytest

import blackbox as bb
from blackbox import migrations
from blackbox.api import _capture_host_report
from blackbox.integrity import RECORD_FIELDS
from blackbox.migrations import v003, v004

HOST_REPORT = {
    "request_id": "host-event",
    "producer": "claude-code",
    "observations": [{"source": "claude-code", "kind": "activity", "name": "Stop"}],
}


def snapshot(path):
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                *RECORD_FIELDS,
                "record_receipts",
                "schema_metadata",
                "schema_migrations",
            )
        }


def schema(path):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()


def released(released_v3, script, *args, check=True):
    _, _, python, source, env = released_v3
    return subprocess.run(
        [str(python), "-c", script, *map(str, args)],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def test_frozen_v3_definition_matches_actual_release(released_v3):
    assert v003.DDL == released_v3[1]["ddl"]
    assert migrations.schema_digest(3) == released_v3[1]["digest"]


def test_v4_changes_only_the_source_authority_check():
    changed = [(old, new) for old, new in zip(v003.DDL, v004.DDL) if old != new]
    assert len(v004.DDL) == len(v003.DDL)
    assert changed == [(v004.V3_SOURCES, v004.SOURCES)]
    assert "'caller_asserted','local_git','host_reported'" in v004.SOURCES


def test_v3_upgrade_preserves_every_record_and_receipt(v3_database, released_v3):
    before = snapshot(v3_database)
    assert bb.initialize(v3_database).schema_version == 4
    after = snapshot(v3_database)
    # Widening a CHECK rewrites nothing: no record, receipt or event is added.
    for table in (*RECORD_FIELDS, "record_receipts"):
        assert after[table] == before[table]
    for table in ("schema_metadata", "schema_migrations"):
        assert after[table][: len(before[table])] == before[table]
        assert [row[0] for row in after[table][len(before[table]) :]] == [4]
    for original in released_v3[1]["records"]:
        view = bb.get_session(v3_database, original["session"]["id"])
        assert view.model_dump(mode="json") == original
    assert bb.check_integrity(v3_database).ok
    bb.initialize(v3_database)
    assert snapshot(v3_database) == after


def test_migrated_schema_is_identical_to_a_fresh_install(v3_database, tmp_path):
    bb.initialize(v3_database)
    fresh = tmp_path / "fresh.sqlite3"
    bb.initialize(fresh)
    assert schema(v3_database) == schema(fresh)
    host = _capture_host_report(v3_database, HOST_REPORT)
    view = bb.get_session(v3_database, host.session_id)
    assert view.sources[0].authority == "host_reported"
    assert bb.check_integrity(v3_database).ok


def test_open_connections_see_the_widened_check(v3_database):
    stale = sqlite3.connect(v3_database, isolation_level=None)
    (session,) = stale.execute("SELECT session_id FROM sources LIMIT 1").fetchone()
    bb.initialize(v3_database)
    # Without the schema cookie bump, this connection would keep the old CHECK.
    stale.execute("BEGIN")
    stale.execute("INSERT INTO sources VALUES ('x',?,'y','host_reported')", (session,))
    stale.execute("ROLLBACK")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        stale.execute("INSERT INTO sources VALUES ('x',?,'y','bogus')", (session,))
    stale.close()


def test_released_v3_code_refuses_a_v4_store(v3_database, released_v3):
    bb.initialize(v3_database)
    result = released(
        released_v3,
        "import blackbox as bb, sys; bb.check_integrity(sys.argv[1])",
        v3_database,
        check=False,
    )
    assert result.returncode != 0 and "unsupported_schema" in result.stderr


@pytest.mark.parametrize("failure", ["exception", "write_error", "final_validation"])
def test_v4_failure_rolls_back_and_actual_v3_release_still_reads(
    v3_database, released_v3, monkeypatch, failure
):
    before = snapshot(v3_database)
    before_schema = schema(v3_database)
    original_upgrade = migrations.UPGRADES[3]
    original_validate = migrations.validate

    def fail(db):
        original_upgrade(db)
        assert (
            "host_reported"
            in db.execute(
                "SELECT sql FROM sqlite_master WHERE name='sources'"
            ).fetchone()[0]
        )
        if failure == "write_error":
            db.execute("PRAGMA query_only=ON")
            db.execute("INSERT INTO schema_metadata VALUES (4,'test','test')")
        raise RuntimeError("injected")

    def reject_final(db, version):
        original_validate(db, version)
        if version == 4:
            raise RuntimeError("final validation failure")

    if failure == "final_validation":
        monkeypatch.setattr(migrations, "validate", reject_final)
    else:
        monkeypatch.setitem(migrations.UPGRADES, 3, fail)
    with pytest.raises((RuntimeError, bb.DatabaseError)):
        bb.initialize(v3_database)
    assert snapshot(v3_database) == before
    assert schema(v3_database) == before_schema
    with sqlite3.connect(v3_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    old = released_v3[1]
    result = released(
        released_v3,
        """
import blackbox as bb, json, sys
assert bb.check_integrity(sys.argv[1]).ok
print(json.dumps([bb.get_session(sys.argv[1], s).model_dump(mode="json")
                  for s in json.loads(sys.argv[2])]))
""",
        v3_database,
        json.dumps([r["session"]["id"] for r in old["records"]]),
    )
    assert json.loads(result.stdout) == old["records"]
    monkeypatch.setitem(migrations.UPGRADES, 3, original_upgrade)
    monkeypatch.setattr(migrations, "validate", original_validate)
    bb.initialize(v3_database)
    assert bb.check_integrity(v3_database).ok


def test_concurrent_v3_upgrade_happens_once(v3_database):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: bb.initialize(v3_database), range(4)))
    with sqlite3.connect(v3_database) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=4"
            ).fetchone()[0]
            == 1
        )
    assert bb.check_integrity(v3_database).ok

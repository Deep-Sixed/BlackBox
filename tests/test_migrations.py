import concurrent.futures
import json
import sqlite3
import subprocess

import pytest

from blackbox import migrations
from blackbox.db import connect
from blackbox.migrations import v001
from blackbox.query import integrity, reconstruct


def test_real_release_upgrade_preserves_every_canonical_field(v1_database, released_v1):
    with sqlite3.connect(v1_database) as db:
        old_metadata = db.execute("SELECT * FROM schema_metadata").fetchall()
    connect(v1_database).close()
    for original in released_v1[1]["records"]:
        assert reconstruct(v1_database, original["session"]["id"]) == original
    assert integrity(v1_database) == {"ok": True, "schema_version": 4, "errors": []}
    with sqlite3.connect(v1_database) as db:
        assert (
            db.execute("SELECT * FROM schema_metadata WHERE version=1").fetchall()
            == old_metadata
        )
        assert db.execute(
            "SELECT version,previous_version FROM schema_migrations"
        ).fetchall() == [(2, 1), (3, 2), (4, 3)]
        before = db.execute("SELECT * FROM record_receipts").fetchall()
    connect(v1_database).close()
    with sqlite3.connect(v1_database) as db:
        assert db.execute("SELECT * FROM record_receipts").fetchall() == before


@pytest.mark.parametrize("failure", ["exception", "write_error"])
def test_rollback_readable_by_released_code(
    v1_database, released_v1, monkeypatch, failure
):
    original = migrations.UPGRADES[1]

    def fail(db):
        original(db)  # Fail after DDL and backfill, before version/metadata commit.
        if failure == "write_error":
            db.execute("PRAGMA query_only=ON")
            db.execute(
                "INSERT INTO schema_migrations VALUES (2,1,?, 'test')", ("a" * 64,)
            )
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(migrations.UPGRADES, 1, fail)
    with pytest.raises((RuntimeError, sqlite3.OperationalError)):
        connect(v1_database)
    with sqlite3.connect(v1_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('record_receipts','schema_migrations')"
        ).fetchall()
        assert db.execute("SELECT version FROM schema_metadata").fetchall() == [(1,)]
    _, snapshot, python, source, env = released_v1
    result = subprocess.run(
        [
            str(python),
            "-c",
            """
import json,sys
from blackbox.query import integrity,reconstruct
assert integrity(sys.argv[1])["ok"]
print(json.dumps([reconstruct(sys.argv[1], s) for s in json.loads(sys.argv[2])]))
""",
            str(v1_database),
            json.dumps([r["session"]["id"] for r in snapshot["records"]]),
        ],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == snapshot["records"]
    monkeypatch.setitem(migrations.UPGRADES, 1, original)
    connect(v1_database).close()
    assert integrity(v1_database)["ok"]


def test_readonly_old_schema_never_migrates(v1_database):
    before = v1_database.read_bytes()
    with pytest.raises(ValueError, match="migration required"):
        connect(v1_database, readonly=True)
    assert v1_database.read_bytes() == before
    with sqlite3.connect(v1_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.parametrize("version", [0, -1, 4, 999])
def test_unknown_version_rejected(v1_database, version):
    with sqlite3.connect(v1_database) as db:
        db.execute(f"PRAGMA user_version={version}")
    with pytest.raises(ValueError):
        connect(v1_database)
    with sqlite3.connect(v1_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version


@pytest.mark.parametrize("damage", ["trigger", "column", "digest", "evidence"])
def test_invalid_v1_not_repaired(v1_database, damage):
    with sqlite3.connect(v1_database) as db:
        if damage == "trigger":
            db.execute("DROP TRIGGER claims_no_update")
        elif damage == "column":
            db.execute("ALTER TABLE claims ADD COLUMN extra TEXT")
        elif damage == "digest":
            db.execute("DROP TRIGGER schema_metadata_no_update")
            db.execute("UPDATE schema_metadata SET schema_digest='bad'")
        else:
            db.execute("DROP TRIGGER evidence_no_update")
            db.execute("UPDATE evidence SET digest='bad'")
            db.execute(
                next(
                    s
                    for s in v001.DDL
                    if s.startswith("CREATE TRIGGER evidence_no_update")
                )
            )
    with pytest.raises(ValueError):
        connect(v1_database)
    with sqlite3.connect(v1_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_concurrent_migration_is_once(v1_database):
    def open_writer(_):
        connect(v1_database).close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(open_writer, range(4)))
    assert integrity(v1_database)["ok"]
    with sqlite3.connect(v1_database) as db:
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 3


def test_fresh_v4_skips_upgrade(tmp_path, monkeypatch):
    def forbidden(_):
        raise AssertionError("fresh creation must not upgrade")

    for version in (1, 2, 3):
        monkeypatch.setitem(migrations.UPGRADES, version, forbidden)
    path = tmp_path / "fresh.sqlite3"
    connect(path).close()
    assert integrity(path)["ok"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_metadata").fetchall() == [(4,)]
        assert db.execute(
            "SELECT version,previous_version FROM schema_migrations"
        ).fetchall() == [(4, 0)]


def test_extra_schema_object_not_hidden_by_internal_name_filter(v1_database):
    with sqlite3.connect(v1_database) as db:
        db.execute("CREATE TABLE sqliteXunexpected(value TEXT)")
    with pytest.raises(ValueError, match="invalid source schema"):
        connect(v1_database)


def test_metadata_and_version_rollback_together(v1_database, monkeypatch):
    original = migrations.validate

    def reject_final(db, version):
        if version == 4:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4
            assert db.execute("SELECT count(*) FROM schema_metadata").fetchone()[0] == 4
            raise RuntimeError("final validation failure")
        original(db, version)

    monkeypatch.setattr(migrations, "validate", reject_final)
    with pytest.raises(RuntimeError):
        connect(v1_database)
    with sqlite3.connect(v1_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT version FROM schema_metadata").fetchall() == [(1,)]
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='record_receipts'"
        ).fetchall()

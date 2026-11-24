"""Schema v5: query indexes over genuine released v4 stores, plus event fields."""

import concurrent.futures
import json
import sqlite3

import pytest
import support
from support import READ_RECORDS, UPGRADE_LEDGER, run_release

import blackbox as bb
from blackbox import migrations
from blackbox.integrity import RECORD_FIELDS
from blackbox.migrations import v004, v005
from blackbox.models import Capture, Observation, canonical, identity

EVENT = {
    "source": "contextforge",
    "kind": "activity",
    "name": "tool.invoke",
    "outcome": "success",
    "severity": "info",
    "occurred_at": "2026-11-24T07:37:46.734100Z",
    "correlation_id": "req-42",
}


def snapshot(path):
    return support.snapshot(path, (*RECORD_FIELDS, *UPGRADE_LEDGER))


def schema(path):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()


def test_frozen_v4_definition_matches_actual_release(released_v4):
    assert v004.DDL == released_v4[1]["ddl"]
    assert migrations.schema_digest(4) == released_v4[1]["digest"]


def test_v5_only_adds_indexes():
    assert v005.DDL[: len(v004.DDL)] == v004.DDL
    assert v005.ADDITIONS == v005.DDL[len(v004.DDL) :]
    assert all(s.startswith("CREATE INDEX ") for s in v005.ADDITIONS)
    assert v005.TABLES == v004.TABLES


def test_v4_upgrade_preserves_every_record_and_receipt(v4_database, released_v4):
    before = snapshot(v4_database)
    assert bb.initialize(v4_database).schema_version == 5
    after = snapshot(v4_database)
    for table in (*RECORD_FIELDS, "record_receipts"):
        assert after[table] == before[table]
    for table in ("schema_metadata", "schema_migrations"):
        assert after[table][: len(before[table])] == before[table]
        assert [row[0] for row in after[table][len(before[table]) :]] == [5]
    for original in released_v4[1]["records"]:
        view = bb.get_session(v4_database, original["session"]["id"])
        assert view.model_dump(mode="json") == original
    assert bb.check_integrity(v4_database).ok
    bb.initialize(v4_database)
    assert snapshot(v4_database) == after


def test_migrated_schema_is_identical_to_a_fresh_install(v4_database, tmp_path):
    bb.initialize(v4_database)
    fresh = tmp_path / "fresh.sqlite3"
    bb.initialize(fresh)
    assert schema(v4_database) == schema(fresh)


def test_released_v4_code_refuses_a_v5_store(v4_database, released_v4):
    bb.initialize(v4_database)
    result = run_release(
        released_v4,
        "import blackbox as bb, sys; bb.check_integrity(sys.argv[1])",
        v4_database,
        check=False,
    )
    assert result.returncode != 0 and "unsupported_schema" in result.stderr


def test_replaying_a_released_request_is_a_duplicate(v4_database):
    """Old fingerprints and observation identities are unchanged by 0.8.0."""
    request = {
        "request_id": "released",
        "producer": "release-test",
        "observations": [{"source": "caller", "kind": "test", "name": "released"}],
        "claims": [
            {"source": "caller", "topic": "release", "statement": "Initial claim"}
        ],
        "artifacts": [{"source": "caller", "path": "report.txt", "digest": "a" * 64}],
    }
    result = bb.capture(v4_database, request)
    assert result.duplicate is True
    view = bb.get_session(v4_database, result.session_id)
    (stored,) = [o for o in view.observations if o.kind == "test"]
    data = Observation.model_validate(request["observations"][0]).model_dump(
        mode="json"
    )
    # 0.8.0 serializes the same input to exactly the bytes 0.6.8 stored.
    with sqlite3.connect(v4_database) as db:
        (stored_data,) = db.execute(
            "SELECT data FROM observations WHERE id=?", (stored.id,)
        ).fetchone()
    assert canonical(data) == stored_data
    # v0.6.8 predates occurrence-qualified identities (#32); its key is the
    # material alone, and that material is unchanged.
    material = {
        "session": result.session_id,
        "source": stored.source_id,
        "kind": "test",
        "data": data,
    }
    assert identity("obs", material) == stored.id


@pytest.mark.parametrize("failure", ["exception", "final_validation"])
def test_v5_failure_rolls_back_and_actual_v4_release_still_reads(
    v4_database, released_v4, monkeypatch, failure
):
    before = snapshot(v4_database)
    before_schema = schema(v4_database)
    original_upgrade = migrations.UPGRADES[4]
    original_validate = migrations.validate

    def fail(db):
        original_upgrade(db)
        raise RuntimeError("injected")

    def reject_final(db, version):
        original_validate(db, version)
        if version == 5:
            raise RuntimeError("final validation failure")

    if failure == "final_validation":
        monkeypatch.setattr(migrations, "validate", reject_final)
    else:
        monkeypatch.setitem(migrations.UPGRADES, 4, fail)
    with pytest.raises((RuntimeError, bb.DatabaseError)):
        bb.initialize(v4_database)
    assert snapshot(v4_database) == before
    assert schema(v4_database) == before_schema
    with sqlite3.connect(v4_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    old = released_v4[1]
    result = run_release(
        released_v4,
        READ_RECORDS,
        v4_database,
        json.dumps([r["session"]["id"] for r in old["records"]]),
    )
    assert json.loads(result.stdout) == old["records"]
    monkeypatch.undo()
    bb.initialize(v4_database)
    assert bb.check_integrity(v4_database).ok


def test_concurrent_v4_upgrade_happens_once(v4_database):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: bb.initialize(v4_database), range(4)))
    with sqlite3.connect(v4_database) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=5"
            ).fetchone()[0]
            == 1
        )
    assert bb.check_integrity(v4_database).ok


# --- event fields -------------------------------------------------------------


def test_event_fields_round_trip(tmp_path):
    database = tmp_path / "bb.sqlite3"
    result = bb.capture(
        database, {"request_id": "e1", "producer": "p", "observations": [EVENT]}
    )
    (observation,) = bb.get_session(database, result.session_id).observations
    assert observation.data.model_dump(mode="json") == {
        **EVENT,
        "exit_code": None,
        "duration_ms": None,
        "content_digest": None,
    }
    assert bb.check_integrity(database).ok


def test_unset_event_fields_are_not_stored(tmp_path):
    database = tmp_path / "bb.sqlite3"
    plain = {"source": "s", "kind": "activity", "name": "n"}
    bb.capture(database, {"request_id": "e1", "producer": "p", "observations": [plain]})
    with sqlite3.connect(database) as db:
        (data,) = db.execute("SELECT data FROM observations").fetchone()
    assert json.loads(data) == {
        **plain,
        "exit_code": None,
        "duration_ms": None,
        "content_digest": None,
    }
    assert Capture.model_validate(
        {"request_id": "r", "producer": "p", "observations": [plain]}
    ).model_dump(mode="json")["observations"][0] == json.loads(data)
    assert canonical(json.loads(data)) == data


@pytest.mark.parametrize(
    "field,value",
    [
        ("outcome", "ok"),
        ("severity", "fatal"),
        ("occurred_at", "2026-11-24T07:37:46+00:00"),
        ("occurred_at", "2026-11-24 07:37:46Z"),
        ("occurred_at", "2026-02-30T00:00:00Z"),
        ("occurred_at", "2026-11-24T07:37:46.1234567Z"),
        ("correlation_id", "has space"),
        ("correlation_id", "token=abc"),
        ("correlation_id", ""),
    ],
)
def test_invalid_event_fields_are_rejected(tmp_path, field, value):
    with pytest.raises(bb.ValidationError):
        bb.capture(
            tmp_path / "bb.sqlite3",
            {
                "request_id": "bad",
                "producer": "p",
                "observations": [{**EVENT, field: value}],
            },
        )


def plan(connection, sql, parameters=()):
    return " ".join(
        row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)
    )


def test_event_queries_use_the_v5_indexes(tmp_path):
    database = tmp_path / "bb.sqlite3"
    events = [
        {
            **EVENT,
            "source": f"svc-{i % 40}",
            "correlation_id": f"req-{i}",
            "severity": ("info", "error")[i % 2],
        }
        for i in range(400)
    ]
    for start in range(0, len(events), 100):
        bb.capture(
            database,
            {
                "request_id": f"batch-{start}",
                "producer": "p",
                "observations": events[start : start + 100],
            },
        )
    with sqlite3.connect(database) as db:
        db.execute("ANALYZE")
        assert "observations_correlation" in plan(
            db,
            "SELECT id FROM observations "
            "WHERE json_extract(data, '$.correlation_id') = ?",
            ("req-7",),
        )
        assert "observations_severity" in plan(
            db,
            "SELECT id FROM observations "
            "WHERE json_extract(data, '$.severity') = ? ORDER BY recorded_at",
            ("error",),
        )
        assert "observations_occurred" in plan(
            db,
            "SELECT id FROM observations "
            "WHERE json_extract(data, '$.occurred_at') >= ?",
            ("2026-11-24T00:00:00Z",),
        )
        assert "observations_recorded" in plan(
            db,
            "SELECT id FROM observations WHERE recorded_at >= ? "
            "ORDER BY recorded_at, id LIMIT 100",
            ("2026-01-01",),
        )
        assert "sources_identity" in plan(
            db, "SELECT id FROM sources WHERE identity = ?", ("svc-3",)
        )

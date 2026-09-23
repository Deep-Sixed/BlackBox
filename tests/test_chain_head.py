import json
import sqlite3
import subprocess
import sys

import pytest
from support import rewrite_consistently

import blackbox as bb
from blackbox.integrity import GENESIS


@pytest.fixture
def database(tmp_path):
    return tmp_path / "anchor.sqlite3"


def request(request_id):
    return {
        "request_id": request_id,
        "producer": "anchor-test",
        "observations": [{"source": "caller", "kind": "test", "name": "unit"}],
        "claims": [{"source": "caller", "topic": "tests", "statement": "Passed"}],
        "artifacts": [{"source": "caller", "path": "report.txt", "digest": "a" * 64}],
    }


def receipts(path):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT sequence,record_type,digest FROM record_receipts ORDER BY sequence"
        ).fetchall()


def test_empty_store_head_is_genesis(database):
    bb.initialize(database)
    head = bb.get_chain_head(database)
    assert isinstance(head, bb.ChainHead)
    assert (head.sequence, head.digest) == (0, GENESIS)
    assert bb.check_integrity(database, anchor=head.model_dump()).ok


def test_head_is_last_receipt_and_stays_valid_as_chain_grows(database):
    bb.capture(database, request("first"))
    head = bb.get_chain_head(database)
    assert (head.sequence, head.digest) == receipts(database)[-1][::2]
    bb.capture(database, request("second"))
    later = bb.get_chain_head(database)
    assert later.sequence > head.sequence
    for anchor in (head, later):
        result = bb.check_integrity(database, anchor=anchor.model_dump(mode="json"))
        assert result.ok and result.first_broken_sequence is None


def test_anchor_detects_rewrite_that_recomputes_receipts(database):
    bb.capture(database, request("rewrite"))
    anchor = bb.get_chain_head(database).model_dump()
    rewrite_consistently(database, "UPDATE artifacts SET path='forged.txt'")
    # Without an anchor a consistent rewrite is indistinguishable from history.
    assert bb.check_integrity(database).ok
    result = bb.check_integrity(database, anchor=anchor)
    assert not result.ok
    assert result.errors == ("anchor_mismatch",)


def test_anchor_detects_rollback_to_an_older_consistent_state(database, tmp_path):
    bb.capture(database, request("kept"))
    older = tmp_path / "older.sqlite3"
    with sqlite3.connect(database) as source, sqlite3.connect(older) as target:
        source.backup(target)
    older.chmod(0o600)
    bb.capture(database, request("dropped"))
    anchor = bb.get_chain_head(database).model_dump()
    assert bb.check_integrity(older).ok
    result = bb.check_integrity(older, anchor=anchor)
    assert result.errors == ("anchor_missing",)


def test_first_broken_sequence_points_at_the_altered_record(database):
    bb.capture(database, request("locate"))
    (artifact,) = [row[0] for row in receipts(database) if row[1] == "artifacts"]
    with sqlite3.connect(database) as db:
        (ddl,) = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='artifacts_no_update'"
        ).fetchone()
        db.execute("DROP TRIGGER artifacts_no_update")
        db.execute("UPDATE artifacts SET path='changed.txt'")
        db.execute(ddl)
    result = bb.check_integrity(database)
    assert "record_integrity" in result.errors
    assert result.first_broken_sequence == artifact


@pytest.mark.parametrize(
    "anchor",
    [
        [],
        {"sequence": -1, "digest": GENESIS},
        {"sequence": True, "digest": GENESIS},
        {"sequence": "1", "digest": GENESIS},
        {"sequence": 2**63, "digest": GENESIS},
        {"sequence": 1, "digest": "not-a-digest"},
        {"sequence": 1, "digest": GENESIS, "extra": 1},
        {"sequence": 1},
    ],
)
def test_invalid_anchor_is_rejected_before_opening(database, anchor):
    with pytest.raises(bb.ValidationError):
        bb.check_integrity(database, anchor=anchor)
    assert not database.exists()


def test_head_is_a_reader(database, v1_database):
    with pytest.raises(bb.DatabaseError):
        bb.get_chain_head(database)
    assert not database.exists()
    before = v1_database.read_bytes()
    with pytest.raises(bb.MigrationRequiredError):
        bb.get_chain_head(v1_database)
    assert v1_database.read_bytes() == before


def test_cli_head_round_trips_into_check(database, tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(request("cli")))
    base = [sys.executable, "-m", "blackbox.cli", "--database", str(database)]

    def cli(*args):
        return subprocess.run(
            [*base, *args], capture_output=True, text=True, check=False
        )

    assert cli("capture", "--input", str(source)).returncode == 0
    head = cli("head")
    assert head.returncode == 0
    anchor = tmp_path / "anchor.json"
    anchor.write_text(head.stdout)
    assert json.loads(head.stdout) == bb.get_chain_head(database).model_dump()
    checked = cli("check", "--anchor", str(anchor))
    assert checked.returncode == 0
    assert json.loads(checked.stdout) == {
        "ok": True,
        "schema_version": 4,
        "errors": [],
        "first_broken_sequence": None,
    }
    anchor.write_text(json.dumps({**json.loads(head.stdout), "digest": "b" * 64}))
    mismatch = cli("check", "--anchor", str(anchor))
    assert mismatch.returncode == 1
    assert json.loads(mismatch.stdout)["errors"] == ["anchor_mismatch"]
    anchor.write_text(json.dumps({"sequence": -1, "digest": GENESIS}))
    invalid = cli("check", "--anchor", str(anchor))
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout) == {"error": "invalid_input", "retryable": False}

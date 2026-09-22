import concurrent.futures
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from blackbox.db import connect
from blackbox.ingest import append_claim, ingest
from blackbox.models import Capture, identity
from blackbox.provenance import collect_git
from blackbox.query import claims, integrity, reconstruct, timeline
from blackbox.schema import TABLES, VERSION


@pytest.fixture
def database(tmp_path):
    return tmp_path / "runtime" / "blackbox.sqlite3"


@pytest.fixture
def capture_request():
    return {
        "request_id": "test-001",
        "producer": "test-agent",
        "observations": [
            {"source": "caller", "kind": "test", "name": "unit-tests", "exit_code": 0}
        ],
        "claims": [
            {
                "source": "caller",
                "topic": "tests",
                "statement": "Tests reported as passing",
            }
        ],
        "artifacts": [{"source": "caller", "path": "report.txt", "digest": "0" * 64}],
    }


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return (
            subprocess.check_output(
                ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )

    git("init", "-q")
    (root / "tracked.txt").write_text("baseline\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "baseline",
    )
    return root, git


def test_migration_empty_database_and_reopen(database):
    connection = connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == VERSION
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } == set(TABLES)
    connection.close()
    connect(database).close()
    assert database.stat().st_mode & 0o777 == 0o600
    assert integrity(database) == {"ok": True, "schema_version": VERSION, "errors": []}


def test_capture_reconstruction_and_dedup(database, capture_request):
    result = ingest(database, capture_request)
    before = timeline(database)
    again = ingest(database, capture_request)
    assert again["session_id"] == result["session_id"]
    assert again["duplicate"] is True
    assert timeline(database) == before
    record = reconstruct(database, result["session_id"])
    assert record["status"] == "COMMITTED"
    assert (
        len(record["observations"])
        == len(record["claims"])
        == len(record["artifacts"])
        == 1
    )
    assert record["evidence"][0]["verification"] == "unverified"
    assert record["sources"][0]["authority"] == "caller_asserted"
    assert record["artifacts"][0]["verification"] == "unverified"
    assert integrity(database)["ok"]


def test_conflicting_id_cannot_overwrite(database, capture_request):
    ingest(database, capture_request)
    before = timeline(database)
    capture_request["producer"] = "different"
    with pytest.raises(ValueError, match="different input"):
        ingest(database, capture_request)
    assert timeline(database) == before


def test_claim_corrections_are_append_only_and_temporal(database, capture_request):
    session = ingest(database, capture_request)["session_id"]
    old = claims(database)[0]
    cutoff = timeline(database)[-1]["sequence"]
    correction = {
        "source": "reviewer",
        "topic": "tests",
        "statement": "One test failed",
    }
    successor = append_claim(
        database, session, correction, target=old["id"], relation="supersedes"
    )
    assert successor != old["id"]
    assert claims(database, through=cutoff) == [old]
    assert claims(database)[0]["status"] == "superseded"
    assert claims(database)[0]["statement"] == old["statement"]
    assert (
        append_claim(
            database, session, correction, target=old["id"], relation="supersedes"
        )
        == successor
    )
    correction["statement"] = "Another correction"
    with pytest.raises(sqlite3.IntegrityError):
        append_claim(
            database, session, correction, target=old["id"], relation="supersedes"
        )
    assert len(claims(database)) == 2
    append_claim(database, session, correction, target=successor, relation="contests")
    assert claims(database)[1]["status"] == "contested"


@pytest.mark.parametrize("table", TABLES)
def test_history_rejects_update_delete(database, capture_request, table):
    ingest(database, capture_request)
    connection = connect(database)
    # Even empty tables must have both immutability triggers.
    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
    }
    assert triggers == {f"{table}_no_update", f"{table}_no_delete"}
    if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
        column = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
        with pytest.raises(sqlite3.IntegrityError, match="immutable history"):
            connection.execute(f"UPDATE {table} SET {column}={column}")
        with pytest.raises(sqlite3.IntegrityError, match="immutable history"):
            connection.execute(f"DELETE FROM {table}")
    connection.close()


def test_read_only_handle_and_foreign_keys(database, capture_request):
    session = ingest(database, capture_request)["session_id"]
    connection = connect(database, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO sessions VALUES ('a','b','c','d','e')")
    connection.close()
    with pytest.raises(ValueError, match="correction target"):
        append_claim(
            database,
            session,
            capture_request["claims"][0],
            target="missing",
            relation="contests",
        )
    assert len(claims(database)) == 1


def test_corrections_cannot_cross_sessions(database, capture_request):
    first = ingest(database, capture_request)["session_id"]
    target = claims(database)[0]["id"]
    second_request = {**capture_request, "request_id": "test-002"}
    second = ingest(database, second_request)["session_id"]
    with pytest.raises(ValueError, match="correction target"):
        append_claim(
            database,
            second,
            second_request["claims"][0],
            target=target,
            relation="contests",
        )
    assert len(claims(database)) == 2
    assert reconstruct(database, first)["status"] == "COMMITTED"


def test_failed_capture_retries_atomically(database, capture_request, monkeypatch):
    module = importlib.import_module("blackbox.ingest")
    original = module.claim_row

    def fail(*args, **kwargs):
        raise OSError("sensitive internal failure detail")

    monkeypatch.setattr(module, "claim_row", fail)
    with pytest.raises(OSError):
        ingest(database, capture_request)
    session = identity("ses", capture_request["request_id"])
    failed = reconstruct(database, session)
    assert failed["status"] == "FAILED_RETRYABLE"
    assert failed["observations"] == failed["evidence"] == failed["claims"] == []
    assert "sensitive internal" not in json.dumps(failed)
    monkeypatch.setattr(module, "claim_row", original)
    ingest(database, capture_request)
    assert reconstruct(database, session)["status"] == "COMMITTED"
    assert len(reconstruct(database, session)["failures"]) == 1


def test_process_crash_releases_transaction_and_allows_retry(database, capture_request):
    code = """
import importlib,json,os,sys
m=importlib.import_module('blackbox.ingest')
m.claim_row=lambda *a,**kw: os._exit(73)
m.ingest(sys.argv[1],json.loads(sys.argv[2]))
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(database), json.dumps(capture_request)],
        check=False,
    )
    assert child.returncode == 73
    session = identity("ses", capture_request["request_id"])
    record = reconstruct(database, session)
    assert record["status"] == "RESERVED"
    assert record["observations"] == []
    ingest(database, capture_request)
    assert reconstruct(database, session)["status"] == "COMMITTED"
    assert integrity(database)["ok"]


def test_concurrent_duplicate_delivery(database, capture_request):
    connect(database).close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ingest(database, capture_request), range(8)))
    assert sum(not item["duplicate"] for item in results) == 1
    assert len({item["session_id"] for item in results}) == 1
    assert len(claims(database)) == 1


def test_concurrent_supersede_one_winner(database, capture_request):
    session = ingest(database, capture_request)["session_id"]
    target = claims(database)[0]["id"]

    def write(number):
        try:
            append_claim(
                database,
                session,
                {
                    "source": "caller",
                    "topic": "tests",
                    "statement": f"Correction {number}",
                },
                target=target,
                relation="supersedes",
            )
            return True
        except sqlite3.IntegrityError:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(write, range(2))) == 1


def test_git_baseline_does_not_lose_dirty_or_untracked_files(repo):
    root, git = repo
    baseline = git("rev-parse", "HEAD")
    (root / "committed.txt").write_text("committed")
    git("add", "committed.txt")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "change",
    )
    (root / "tracked.txt").write_text("dirty")
    (root / "new\nfile.txt").write_text("untracked")
    result = collect_git(root, baseline)
    assert result["committed_delta"] == ["committed.txt"]
    assert result["working_tree_delta"] == ["tracked.txt"]
    assert result["untracked_files"] == ["new\nfile.txt"]


def test_staged_and_unstaged_cancellation_is_visible(repo):
    root, git = repo
    (root / "tracked.txt").write_text("staged")
    git("add", "tracked.txt")
    (root / "tracked.txt").write_text("baseline\n")
    result = collect_git(root, git("rev-parse", "HEAD"))
    assert result["working_tree_delta"] == ["tracked.txt"]
    assert result["staged_delta"] == result["unstaged_delta"] == ["tracked.txt"]


def test_local_observer_authority_cannot_be_claimed_by_input(
    database, capture_request, repo
):
    root, _ = repo
    capture_request["observations"][0]["source"] = "blackbox.git"
    session = ingest(database, capture_request, repo=root)["session_id"]
    record = reconstruct(database, session)
    assert {row["verification"] for row in record["evidence"]} == {
        "unverified",
        "locally_observed",
    }
    assert {row["authority"] for row in record["sources"]} == {
        "caller_asserted",
        "local_git",
    }


@pytest.mark.parametrize(
    "extra",
    ["password", "payload", "stdout", "verified", "observer_authority", "raw_response"],
)
def test_unknown_persisted_fields_rejected(database, capture_request, extra):
    capture_request["observations"][0][extra] = "not-persisted"
    with pytest.raises(ValueError):
        ingest(database, capture_request)
    assert not database.exists()


@pytest.mark.parametrize(
    "value",
    [
        "password=synthetic-only",
        "Bearer synthetic-only",
        "postgresql://synthetic.invalid/db",
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_sensitive_shaped_content_rejected_before_storage(
    database, capture_request, value
):
    capture_request["claims"][0]["statement"] = value
    with pytest.raises(ValueError):
        ingest(database, capture_request)
    assert not database.exists()


def test_unknown_schema_is_rejected(database):
    connection = connect(database)
    connection.execute("PRAGMA user_version=999")
    connection.close()
    with pytest.raises(ValueError, match="unsupported"):
        connect(database)


def test_cli_end_to_end_and_safe_errors(database, capture_request, tmp_path):
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(capture_request))

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "blackbox.cli", "--database", str(database), *args],
            capture_output=True,
            check=False,
            text=True,
        )

    result = cli("capture", "--input", str(input_file))
    assert result.returncode == 0, result.stderr
    session = json.loads(result.stdout)["session_id"]
    assert json.loads(cli("show", session).stdout)["status"] == "COMMITTED"
    assert json.loads(cli("check").stdout)["ok"]
    input_file.write_text(
        json.dumps({**capture_request, "password": "synthetic-private-value"})
    )
    result = cli("capture", "--input", str(input_file))
    assert result.returncode == 1
    assert "synthetic-private-value" not in result.stdout + result.stderr


def test_model_instance_cannot_bypass_validation(database):
    malicious = Capture.model_construct(request_id="x", producer="password=synthetic")
    with pytest.raises(ValueError):
        ingest(database, malicious)


def test_foreign_database_not_adopted(tmp_path):
    path = tmp_path / "foreign.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE important(value TEXT)")
    db.close()
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="non-BlackBox"):
        connect(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()


def test_machine_readable_schema_matches_model(capture_request):
    import jsonschema

    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/capture.schema.json").read_text()
    )
    assert schema == Capture.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(capture_request, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**capture_request, "payload": {}}, schema)

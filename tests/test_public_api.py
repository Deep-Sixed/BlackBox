import importlib
import json
import os
import sqlite3
import subprocess
import sys
import traceback

import pytest
from pydantic import ValidationError as ModelValidationError

import blackbox as bb


@pytest.fixture
def database(tmp_path):
    return tmp_path / "public.sqlite3"


@pytest.fixture
def request_data():
    return {
        "request_id": "public-contract",
        "producer": "consumer",
        "observations": [{"source": "caller", "kind": "test", "name": "contract"}],
        "claims": [{"source": "caller", "topic": "contract", "statement": "Original"}],
        "artifacts": [{"source": "caller", "path": "result.txt", "digest": "a" * 64}],
    }


def test_supported_exports_are_deliberate():
    assert len(bb.__all__) == len(set(bb.__all__))
    assert all(hasattr(bb, name) for name in bb.__all__)
    for name in (
        "connect",
        "transaction",
        "ingest",
        "reconstruct",
        "canonical",
        "identity",
        "collect_git",
        "BaseModel",
    ):
        assert name not in bb.__all__
    assert bb.__version__ == "0.6.7"


def test_typed_detached_results(database, request_data):
    assert bb.initialize(database).schema_version == 4
    captured = bb.capture(database, request_data)
    assert isinstance(captured, bb.CaptureResult)
    view = bb.get_session(database, captured.session_id)
    assert isinstance(view.session, bb.SessionRecord)
    assert isinstance(view.observations[0].data, bb.CallerObservation)
    assert isinstance(view.claims[0], bb.ClaimRecord)
    assert not hasattr(view.claims[0], "status")
    assert isinstance(bb.get_claims(database)[0], bb.ClaimView)
    assert isinstance(bb.get_timeline(database)[0], bb.TimelineEvent)
    assert isinstance(view.events, tuple)
    with pytest.raises(ModelValidationError):
        view.session.producer = "changed"
    with pytest.raises(ModelValidationError):
        view.observations[0].data.name = "changed"
    request_data["producer"] = "changed"
    assert bb.get_session(database, captured.session_id).session.producer == "consumer"
    assert bb.check_integrity(database).ok


@pytest.mark.parametrize(
    "invalid_request",
    [None, [], {}, {"request_id": "x", "producer": "password=synthetic-sensitive"}],
)
def test_invalid_capture_is_bounded_and_does_not_create_db(database, invalid_request):
    with pytest.raises(bb.ValidationError) as caught:
        bb.capture(database, invalid_request)
    assert str(caught.value) == "invalid_input"
    assert "synthetic-sensitive" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert not database.exists()


@pytest.mark.parametrize("through", [-1, True, "1", 1.5, 2**63])
def test_invalid_cutoff_is_rejected(database, through):
    with pytest.raises(bb.ValidationError):
        bb.get_timeline(database, through=through)
    with pytest.raises(bb.ValidationError):
        bb.get_claims(database, through=through)
    assert not database.exists()


def test_conflict_and_not_found(database, request_data):
    result = bb.capture(database, request_data)
    assert bb.capture(database, request_data).duplicate
    with pytest.raises(bb.ConflictError) as caught:
        bb.capture(database, {**request_data, "producer": "different"})
    assert not caught.value.retryable
    with pytest.raises(bb.NotFoundError):
        bb.get_session(database, "missing")
    with pytest.raises(bb.NotFoundError):
        bb.append_claim(database, "missing", request_data["claims"][0])
    original = bb.get_claims(database)[0]
    bb.append_claim(
        database,
        result.session_id,
        {**request_data["claims"][0], "statement": "First correction"},
        target=original.id,
        relation="supersedes",
    )
    with pytest.raises(bb.ConflictError):
        bb.append_claim(
            database,
            result.session_id,
            {**request_data["claims"][0], "statement": "Other correction"},
            target=original.id,
            relation="supersedes",
        )
    assert bb.check_integrity(database).ok


def test_real_writer_lock_is_retryable(database, request_data):
    bb.capture(database, request_data)
    connection = sqlite3.connect(database)
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(bb.BusyError) as caught:
            bb.initialize(database)
        assert isinstance(caught.value, bb.ConflictError) and caught.value.retryable
        assert str(caught.value) == "database_busy"
    finally:
        connection.rollback()
        connection.close()
    assert bb.initialize(database).schema_version == 4


@pytest.mark.parametrize(
    "operation", [bb.initialize, bb.check_integrity, bb.get_timeline, bb.get_claims]
)
def test_corruption_has_bounded_nonretryable_error(database, operation):
    database.write_bytes(b"synthetic-private-corrupt-data")
    database.chmod(0o600)
    with pytest.raises(bb.IntegrityError) as caught:
        operation(database)
    assert not caught.value.retryable
    assert str(caught.value) == "integrity_failure"


@pytest.mark.parametrize(
    "operation", [bb.check_integrity, bb.get_timeline, bb.get_claims]
)
def test_reader_never_creates_database(database, operation):
    with pytest.raises(bb.DatabaseError):
        operation(database)
    assert not database.exists()


def test_readonly_old_database_requires_explicit_writer(v1_database, released_v1):
    session = released_v1[1]["records"][0]["session"]["id"]
    before = v1_database.read_bytes()
    for call in (
        lambda: bb.get_session(v1_database, session),
        lambda: bb.get_timeline(v1_database),
        lambda: bb.get_claims(v1_database),
        lambda: bb.check_integrity(v1_database),
    ):
        with pytest.raises(bb.MigrationRequiredError):
            call()
        assert v1_database.read_bytes() == before
    assert bb.initialize(v1_database).schema_version == 4
    assert (
        bb.get_session(v1_database, session).model_dump(mode="json")
        == released_v1[1]["records"][0]
    )
    assert bb.check_integrity(v1_database).ok


def test_capture_writer_can_migrate(v1_database, request_data):
    assert bb.capture(v1_database, request_data).status == "COMMITTED"
    assert bb.check_integrity(v1_database).schema_version == 4


def test_schema_error_is_distinct(database):
    bb.initialize(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(bb.SchemaError) as caught:
        bb.get_timeline(database)
    assert not isinstance(caught.value, bb.MigrationRequiredError)


def test_observer_failure_is_bounded_and_retryable(database, request_data, monkeypatch):
    module = importlib.import_module("blackbox.ingest")

    def fail(*args):
        raise OSError("synthetic-private-location")

    monkeypatch.setattr(module, "collect_git", fail)
    with pytest.raises(bb.ObservationError) as caught:
        bb.capture(database, request_data, repo=database.parent)
    assert caught.value.retryable
    assert "synthetic-private-location" not in "".join(
        traceback.format_exception(caught.value)
    )
    session = bb.get_timeline(database)[0].session_id
    failed = bb.get_session(database, session)
    assert failed.status == "FAILED_RETRYABLE"
    assert failed.failures[-1].retryable == 1
    assert bb.check_integrity(database).ok


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test"]
            + ["-c", "user.email=test@example.invalid", *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (repo / "tracked.txt").write_text("baseline\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    return repo, git


def test_credential_shaped_names_are_redacted_not_fatal(database, request_data, tmp_path):
    # One such file used to reject the whole snapshot, hiding every other change.
    repo, _ = _git_repo(tmp_path)
    (repo / "api_key=synthetic-private-value.txt").write_text("untracked")
    (repo / "token=synthetic-other").write_text("")
    (repo / "tracked.txt").write_text("edited\n")
    (repo / "new-feature.py").write_text("x = 1\n")
    result = bb.capture(database, request_data, repo=repo)
    assert result.status == "COMMITTED"
    [git_data] = [
        o.data for o in bb.get_session(database, result.session_id).observations
        if o.kind == "git"
    ]
    marker = "[redacted: credential-shaped name]"
    assert git_data.untracked_files == (marker, marker, "new-feature.py")
    assert git_data.unstaged_delta == ("tracked.txt",)
    assert bb.check_integrity(database).ok
    stored = b"".join(
        path.read_bytes() for path in database.parent.glob(database.name + "*")
    )
    assert b"synthetic-private-value" not in stored
    assert b"synthetic-other" not in stored


def test_credential_shaped_branch_name_is_redacted(database, request_data, tmp_path):
    repo, git = _git_repo(tmp_path)
    git("checkout", "-qb", "fix/password=synthetic-branch")
    result = bb.capture(database, request_data, repo=repo)
    [git_data] = [
        o.data for o in bb.get_session(database, result.session_id).observations
        if o.kind == "git"
    ]
    assert git_data.branch == "[redacted: credential-shaped name]"
    assert git_data.commit_after  # the rest of the snapshot is intact


def test_integrity_findings_are_typed_results(database, request_data):
    bb.capture(database, request_data)
    with sqlite3.connect(database) as connection:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='claims_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER claims_no_update")
        connection.execute("UPDATE claims SET statement='Changed'")
        connection.execute(ddl)
    result = bb.check_integrity(database)
    assert isinstance(result, bb.IntegrityResult)
    assert not result.ok and "record_integrity" in result.errors


@pytest.mark.parametrize(
    "command", [("capture", "--input"), ("claim", "session", "--input")]
)
def test_cli_bounds_deeply_nested_input(database, tmp_path, command):
    source = tmp_path / "deep.json"
    source.write_text("[" * 100_000 + "]" * 100_000)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "blackbox.cli",
            "--database",
            str(database),
            *command,
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "error": "input_unavailable_or_invalid",
        "retryable": False,
    }
    assert "Traceback" not in result.stderr


def test_cli_uses_public_error_contract(database, request_data, tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(request_data))
    command = [
        sys.executable,
        "-m",
        "blackbox.cli",
        "--database",
        str(database),
        "capture",
        "--input",
        str(source),
    ]
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    source.write_text(json.dumps({**request_data, "producer": "different"}))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"error": "conflict", "retryable": False}


def test_wrong_identity_is_not_a_migration_request(v1_database):
    with sqlite3.connect(v1_database) as connection:
        connection.execute("PRAGMA application_id=123")
    with pytest.raises(bb.SchemaError) as caught:
        bb.get_timeline(v1_database)
    assert not isinstance(caught.value, bb.MigrationRequiredError)


def test_append_claim_writer_can_migrate(v1_database, released_v1):
    session = released_v1[1]["records"][0]["session"]["id"]
    result = bb.append_claim(
        v1_database,
        session,
        {"source": "caller", "topic": "upgrade", "statement": "Added after migration"},
    )
    assert result.claim_id in {row.id for row in bb.get_claims(v1_database)}
    assert bb.check_integrity(v1_database).ok


def test_git_observation_is_typed(database, request_data, tmp_path):
    repo = tmp_path / "git"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "baseline",
    )
    (repo / "untracked.txt").write_text("test")
    result = bb.capture(database, request_data, repo=repo)
    view = bb.get_session(database, result.session_id)
    observations = [row for row in view.observations if row.kind == "git"]
    assert isinstance(observations[0].data, bb.GitObservation)
    assert observations[0].data.untracked_files == ("untracked.txt",)
    assert {row.authority for row in view.sources} == {"caller_asserted", "local_git"}
    assert {row.verification for row in view.evidence} == {
        "unverified",
        "locally_observed",
    }


def test_malformed_stored_result_is_integrity_error(database, request_data):
    result = bb.capture(database, request_data)
    with sqlite3.connect(database) as connection:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='observations_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER observations_no_update")
        connection.execute("UPDATE observations SET data='{}'")
        connection.execute(ddl)
    with pytest.raises(bb.IntegrityError):
        bb.get_session(database, result.session_id)


def test_missing_database_os_error_is_sanitized(database, monkeypatch):
    module = importlib.import_module("blackbox.db")

    def fail(*args, **kwargs):
        raise OSError("synthetic-private-database-path")

    monkeypatch.setattr(module, "connect", fail)
    with pytest.raises(bb.DatabaseError) as caught:
        bb.initialize(database)
    assert str(caught.value) == "database_unavailable"
    assert "synthetic-private-database-path" not in "".join(
        traceback.format_exception(caught.value)
    )


def test_unresolvable_home_path_is_bounded(monkeypatch):
    from pathlib import Path

    def fail(_):
        raise RuntimeError("synthetic-private-home")

    monkeypatch.setattr(Path, "expanduser", fail)
    with pytest.raises(bb.ValidationError) as caught:
        bb.initialize("~/store.sqlite3")
    assert str(caught.value) == "invalid_input"
    assert "synthetic-private-home" not in "".join(
        traceback.format_exception(caught.value)
    )


READERS = [
    bb.get_timeline,
    bb.get_claims,
    bb.get_evidence_links,
    bb.check_integrity,
    bb.get_chain_head,
]


@pytest.mark.parametrize("operation", READERS)
def test_readers_refuse_a_directory_other_users_can_write(
    database, request_data, operation
):
    bb.initialize(database)
    bb.capture(database, request_data)
    database.parent.chmod(0o777)
    with pytest.raises(bb.DatabaseError):
        operation(database)


def test_swapped_in_database_is_refused_before_it_can_pass_integrity(
    database, request_data, tmp_path
):
    bb.initialize(database)
    bb.capture(database, request_data)
    forged = tmp_path / "elsewhere" / "blackbox.sqlite3"
    bb.initialize(forged)
    bb.capture(forged, {**request_data, "request_id": "forged-001"})
    assert bb.check_integrity(forged).ok  # internally consistent
    database.parent.chmod(0o777)  # another user could now replace the file
    forged.replace(database)
    with pytest.raises(bb.DatabaseError):
        bb.check_integrity(database)
    with pytest.raises(bb.DatabaseError):
        bb.get_timeline(database)


@pytest.mark.parametrize("operation", [bb.initialize, bb.check_integrity])
def test_symlinked_parent_in_a_shared_directory_is_refused(
    tmp_path, request_data, operation
):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    bb.initialize(private / "blackbox.sqlite3")
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    (shared / "dbdir").symlink_to(private)
    with pytest.raises(bb.DatabaseError):
        operation(shared / "dbdir" / "blackbox.sqlite3")


def test_own_symlink_in_a_sticky_directory_is_accepted(tmp_path):
    # Like /tmp on macOS: a sticky shared directory holding this user's symlink.
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)
    (sticky / "dbdir").symlink_to(private)
    database = sticky / "dbdir" / "blackbox.sqlite3"
    bb.initialize(database)
    assert bb.check_integrity(database).ok


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to chown")
def test_another_users_symlink_in_a_sticky_directory_is_refused(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)
    link = sticky / "dbdir"
    link.symlink_to(private)
    os.lchown(link, 12345, 12345)  # its owner could repoint it
    with pytest.raises(bb.DatabaseError):
        bb.initialize(link / "blackbox.sqlite3")

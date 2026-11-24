"""CLI client mode (socket), hook delivery, backups and token generation."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest
from test_daemon import Running

import blackbox as bb
from blackbox import api, backup
from blackbox.service.auth import Registry, hash_token, new_token, parse_principals

PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "session_id": "sess-1",
    "tool_name": "Bash",
    "tool_use_id": "toolu_1",
    "tool_input": {"command": "echo password=hunter2"},
    "cwd": "/tmp",
}


def cli(*args, stdin=b"", home=None, check=False):
    env = {**os.environ}
    if home is not None:
        env["HOME"] = str(home)
        env.pop("BLACKBOX_HOOK_HMAC_KEY_FILE", None)
    return subprocess.run(
        [sys.executable, "-m", "blackbox.cli", *map(str, args)],
        input=stdin,
        capture_output=True,
        env=env,
        check=check,
    )


@pytest.fixture
def daemon(tmp_path):
    instance = Running(tmp_path)
    hooks_token, hooks_hash = new_token()
    text = instance.principals_path.read_text() + (
        "[principals.hooks]\n"
        f"uids = [{os.getuid()}]\n"
        'scopes = ["host:report", "events:read"]\n'
        f'token_sha256 = ["{hooks_hash}"]\n'
    )
    instance.principals_path.write_text(text)
    instance.service.reload(Registry(parse_principals(text)))
    for name, token in (("forge", instance.token), ("hooks", hooks_token)):
        path = tmp_path / f"{name}.token"
        path.write_text(token + "\n")
        path.chmod(0o600)
    yield instance
    instance.close()


def test_token_new_prints_a_token_and_its_hash():
    result = cli("token", "new", check=True)
    data = json.loads(result.stdout)
    assert (
        data["token"].startswith("bbx_") and hash_token(data["token"]) == data["sha256"]
    )


def test_hook_over_socket_records_host_event_without_payload(daemon, tmp_path):
    result = cli(
        "--socket",
        daemon.socket,
        "--token-file",
        tmp_path / "hooks.token",
        "hook",
        "--producer",
        "claude-code",
        stdin=json.dumps(PAYLOAD).encode(),
        home=tmp_path,
    )
    assert result.returncode == 0 and result.stdout == b""
    with sqlite3.connect(daemon.service.database) as db:
        rows = db.execute(
            "SELECT s.authority, o.data FROM observations o "
            "JOIN sources s ON s.id=o.source_id"
        ).fetchall()
    assert [r[0] for r in rows] == ["host_reported"]
    data = json.loads(rows[0][1])
    assert data["name"] == "PostToolUse:Bash" and len(data["content_digest"]) == 64
    assert "hunter2" not in rows[0][1]


def test_hook_failure_is_reported_but_never_blocks(tmp_path):
    token = tmp_path / "t"
    token.write_text("bbx_x")
    token.chmod(0o600)
    result = cli(
        "--socket",
        tmp_path / "missing.sock",
        "--token-file",
        token,
        "hook",
        stdin=json.dumps(PAYLOAD).encode(),
        home=tmp_path,
    )
    assert result.returncode == 1  # never 2, which would block the agent
    assert result.stdout == b""
    assert json.loads(result.stderr) == {"error": "daemon_unreachable"}


def test_token_file_must_be_private(daemon, tmp_path):
    loose = tmp_path / "forge.token"
    loose.chmod(0o644)
    result = cli("--socket", daemon.socket, "--token-file", loose, "head")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "token_file_not_private"


def test_head_and_anchored_check_over_socket(daemon, tmp_path):
    daemon.request(
        "POST", "/api/v1/events", {"idempotency_key": "a", "source": "s", "type": "t"}
    )
    token = tmp_path / "forge.token"
    head = cli("--socket", daemon.socket, "--token-file", token, "head", check=True)
    anchors = tmp_path / "anchors.jsonl"
    stale = {"sequence": 0, "digest": "0" * 64}
    anchors.write_text(json.dumps(stale) + "\n" + head.stdout.decode())
    ok = cli(
        "--socket", daemon.socket, "--token-file", token, "check", "--anchor", anchors
    )
    assert ok.returncode == 0 and json.loads(ok.stdout)["ok"] is True
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({**json.loads(head.stdout), "digest": "f" * 64}))
    bad = cli(
        "--socket", daemon.socket, "--token-file", token, "check", "--anchor", forged
    )
    assert bad.returncode == 1 and "anchor_mismatch" in json.loads(bad.stdout)["errors"]


def test_database_commands_are_refused_in_socket_mode(tmp_path):
    result = cli("--socket", tmp_path / "s", "show", "x")
    assert json.loads(result.stdout)["error"] == "command_requires_database"
    result = cli("init")
    assert json.loads(result.stdout)["error"] == "database_or_socket_required"


# --- backups ------------------------------------------------------------------


def seeded(tmp_path):
    database = tmp_path / "db" / "bb.sqlite3"
    bb.capture(
        database,
        {
            "request_id": "r1",
            "producer": "p",
            "observations": [
                {"source": "s", "kind": "activity", "name": "n", "severity": "info"}
            ],
        },
    )
    return database


def test_backup_is_a_verified_private_consistent_copy(tmp_path):
    database = seeded(tmp_path)
    target = tmp_path / "backups"
    target.mkdir(mode=0o750)
    result = cli("--database", database, "backup", "--to", target, check=True)
    info = json.loads(result.stdout)
    copy = target / os.path.basename(info["path"])
    assert copy.stat().st_mode & 0o777 == 0o600
    assert info["integrity"] == "ok"
    assert bb.check_integrity(copy).ok
    assert (
        bb.get_chain_head(copy).model_dump() == bb.get_chain_head(database).model_dump()
    )
    with sqlite3.connect(copy) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not list(target.glob(".*.partial"))


def test_backup_prunes_to_keep(tmp_path):
    database = seeded(tmp_path)
    target = tmp_path / "backups"
    target.mkdir(mode=0o700)
    for _ in range(4):
        backup.backup(database, target, keep=2)
    assert len(list(target.glob("blackbox-*.sqlite3"))) == 2


def test_backup_refuses_a_shared_directory(tmp_path):
    database = seeded(tmp_path)
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o777)
    with pytest.raises(ValueError):
        backup.backup(database, target)


def test_failed_verification_leaves_no_copy(tmp_path, monkeypatch):
    database = seeded(tmp_path)
    target = tmp_path / "backups"
    target.mkdir(mode=0o700)
    original = api.check_integrity

    def broken(path, **kwargs):
        result = original(path, **kwargs)
        return result.model_copy(update={"ok": False})

    monkeypatch.setattr(api, "check_integrity", broken)
    with pytest.raises(bb.IntegrityError):
        backup.backup(database, target)
    assert list(target.iterdir()) == []


def test_group_readable_backups_for_a_separate_reader(tmp_path):
    database = seeded(tmp_path)
    target = tmp_path / "backups"
    target.mkdir()
    target.chmod(0o2750)
    info = backup.backup(database, target, group_readable=True)
    assert os.stat(info["path"]).st_mode & 0o777 == 0o640
    assert database.stat().st_mode & 0o777 == 0o600


def test_head_append_writes_only_successful_heads(daemon, tmp_path):
    token = tmp_path / "forge.token"
    log = tmp_path / "anchors.jsonl"
    for _ in range(2):
        cli(
            "--socket",
            daemon.socket,
            "--token-file",
            token,
            "head",
            "--append",
            log,
            check=True,
        )
    failed = cli(
        "--socket",
        tmp_path / "gone.sock",
        "--token-file",
        token,
        "head",
        "--append",
        log,
    )
    assert failed.returncode == 1
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert all(set(json.loads(x)) == {"sequence", "digest"} for x in lines)
    assert log.stat().st_mode & 0o777 == 0o600

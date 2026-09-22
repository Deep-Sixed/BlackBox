"""Finalizer produces Recon audit artifacts (original Snitch format) from a real git repo."""

import json
import subprocess
import uuid
from datetime import date

import pytest

from recon.finalize import derive_request_id, finalize_session

TODAY = f"{date.today():%Y/%m/%d}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "commit", "-q", "--allow-empty", "-m", "seed"],
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )
    (workdir / "touched.txt").write_text("evidence\n")

    storage = tmp_path / "storage"
    monkeypatch.setenv("RECON_AUDIT_DIR", str(storage / "audits"))
    monkeypatch.setenv("RECON_RECORDS_DIR", str(storage / "records"))
    monkeypatch.setenv("RECON_RESERVATIONS_DIR", str(storage / "reservations"))
    return workdir


def test_finalize_writes_recon_audit(repo, tmp_path):
    session_id = str(uuid.uuid4())
    outcome = finalize_session(
        surface="claude_code", session_id=session_id, cwd=str(repo)
    )
    assert outcome["status"] == "finalized"

    audit = tmp_path / "storage" / "audits" / TODAY / f"recon_{session_id}.md"
    assert audit.exists()
    text = audit.read_text()

    assert text.startswith(f"# Recon Session {session_id}")
    for heading in (
        "## Session Context",
        "## Files Changed",
        "## Commands Claimed",
        "## Commands Verified",
        "## Tests Claimed",
        "## Tests Verified",
        "## Artifacts Written",
        "## Database Writes",
        "## Failures",
        "## Blockers",
        "## Deferred Work",
        "## Risk Flags",
    ):
        assert heading in text
    assert "- Flight Recorder Role: `evidence-only`" in text
    assert '"touched.txt"' in text
    assert "rev-parse HEAD" in text
    assert "status --porcelain=v1 --untracked-files=all" in text
    assert "evidence_sha256" in text

    record = json.loads(
        (tmp_path / "storage" / "records" / f"{session_id}.json").read_text()
    )
    assert record["agent"] == "claude_code"
    assert record["files_changed"] == ["touched.txt"]
    assert len(record["commands_verified"]) == 5
    assert record["redaction_applied"] is True
    assert record["content_capture"] is False

    digest_file = tmp_path / "storage" / "records" / f"{session_id}.sha256"
    assert digest_file.exists()


def test_repeat_session_complete_dedups(repo):
    session_id = str(uuid.uuid4())
    first = finalize_session(
        surface="claude_code", session_id=session_id, cwd=str(repo)
    )
    second = finalize_session(
        surface="claude_code", session_id=session_id, cwd=str(repo)
    )
    assert first["status"] == "finalized"
    assert second["status"] == "duplicate"
    assert second["request_id"] == derive_request_id("claude_code", session_id)


def test_non_repo_cwd_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("RECON_AUDIT_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("RECON_RECORDS_DIR", str(tmp_path / "r"))
    monkeypatch.setenv("RECON_RESERVATIONS_DIR", str(tmp_path / "v"))
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    outcome = finalize_session(
        surface="claude_code", session_id=str(uuid.uuid4()), cwd=str(bare)
    )
    assert outcome["status"] == "skipped"

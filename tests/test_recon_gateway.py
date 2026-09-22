"""Gateway honors the stop-hook POST contract and journals observations."""

import json
import subprocess
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from recon.gateway import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECON_GATEWAY_LOG", str(tmp_path / "observations.jsonl"))
    monkeypatch.setenv("RECON_AUDIT_DIR", str(tmp_path / "audits"))
    monkeypatch.setenv("RECON_RECORDS_DIR", str(tmp_path / "records"))
    monkeypatch.setenv("RECON_RESERVATIONS_DIR", str(tmp_path / "reservations"))
    return TestClient(app)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_non_session_event_is_journaled(client, tmp_path):
    response = client.post(
        "/v1/record",
        json={
            "surface": "recon",
            "request_id": uuid.uuid4().hex,
            "tool": "ledger-mcp-ledger-add-claim",
            "result": {},
        },
    )
    assert response.json()["status"] == "recorded"
    lines = (tmp_path / "observations.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["tool"] == "ledger-mcp-ledger-add-claim"


def test_session_complete_finalizes(client, tmp_path):
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

    session_id = str(uuid.uuid4())
    payload = {
        "surface": "claude_code",
        "request_id": session_id,
        "tool": "session_complete",
        "result": {"cwd": str(workdir), "hook_event_name": "Stop"},
    }

    first = client.post("/v1/record", json=payload).json()
    assert first["status"] == "finalized"
    dated = f"{date.today():%Y/%m/%d}"
    audit = tmp_path / "audits" / dated / f"recon_{session_id}.md"
    assert audit.exists()
    assert audit.read_text().startswith(f"# Recon Session {session_id}")

    second = client.post("/v1/record", json=payload).json()
    assert second["status"] == "duplicate"

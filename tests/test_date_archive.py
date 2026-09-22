"""Canonical date-first archive: one daily folder = complete view of a date."""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ledger.parse import claim_from_mapping
from ledger.repository import add_claim, contest_linked, load_claims
from ledger.models import SourceRef
from recall.compose import build_digest, write_digest

TODAY = date.today()  # operator-local day — shared bucket across all three products
DATED = f"{TODAY:%Y}/{TODAY:%m}/{TODAY:%d}"


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setenv("FLIGHT_RECORDER_STORE", str(root))
    monkeypatch.setenv("RECALL_LEDGER_STORE", str(root))
    monkeypatch.setenv("RECALL_RECON_STORE", str(root))
    monkeypatch.setenv("RECALL_OUTPUT_STORE", str(root))
    return root


def _claim(claim_id: str, evidence: Path):
    return claim_from_mapping(
        {
            "id": claim_id,
            "statement": "A recorded fact",
            "topic": "session-summary",
            "type": "event",
            "sources": [{"ref": evidence.name, "quote": "evidence"}],
            "confidence": "high",
            "status": "active",
            "created": TODAY.isoformat(),
            "updated": TODAY.isoformat(),
        }
    )


def test_new_claims_land_in_todays_ledger_folder(archive):
    evidence = archive / "note.md"
    evidence.write_text("evidence", encoding="utf-8")
    add_claim(archive, _claim("clm-2026-1001", evidence))

    stored = archive / DATED / "ledger" / "clm-2026-1001.md"
    assert stored.is_file()
    assert [c.id for c in load_claims(archive)] == ["clm-2026-1001"]


def test_contest_rewrites_target_in_place_and_disputes_land_today(archive):
    evidence = archive / "note.md"
    evidence.write_text("evidence", encoding="utf-8")
    add_claim(archive, _claim("clm-2026-1001", evidence))
    target_path = archive / DATED / "ledger" / "clm-2026-1001.md"

    contest_claim, target = contest_linked(
        archive,
        "clm-2026-1001",
        rationale="Disputed by newer evidence",
        sources=[SourceRef(ref="note.md", quote="evidence", source_type="repo-file")],
    )

    by_id = {c.id: c for c in load_claims(archive)}
    assert by_id["clm-2026-1001"].status == "contested"
    assert target_path.is_file()  # rewritten where it lives, not relocated
    contest_path = archive / DATED / "ledger" / f"{contest_claim.id}.md"
    assert contest_path.is_file()


def test_recon_finalize_writes_into_day_recon_folder(archive, tmp_path, monkeypatch):
    monkeypatch.delenv("RECON_AUDIT_DIR", raising=False)
    from recon.finalize import finalize_session

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
    outcome = finalize_session(
        surface="claude_code", session_id=session_id, cwd=str(workdir)
    )
    assert outcome["status"] == "finalized"

    day_recon = archive / DATED / "recon"
    assert (day_recon / f"recon_{session_id}.md").is_file()
    assert (day_recon / f"{session_id}.json").is_file()
    assert (archive / "_state" / "recon-reservations").is_dir()


def test_daily_folder_gives_complete_view(archive, tmp_path, monkeypatch):
    monkeypatch.delenv("RECON_AUDIT_DIR", raising=False)
    from recon.finalize import finalize_session

    # ledger activity
    evidence = archive / "note.md"
    evidence.write_text("evidence", encoding="utf-8")
    add_claim(archive, _claim("clm-2026-1001", evidence))

    # recon activity
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
    finalize_session(surface="claude_code", session_id=session_id, cwd=str(workdir))

    # daily observation journal (what the recon gateway appends)
    journal = archive / DATED / "recon" / "observations.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "tool": "session_complete"}) + "\n",
        encoding="utf-8",
    )

    # recall digest composed from the same day folder
    path = write_digest(archive, TODAY)
    assert path == archive / DATED / "recall" / "digest.md"
    digest = build_digest(archive, TODAY)
    assert "A recorded fact" in digest
    assert session_id.split("-")[0] in digest
    assert "1 recon observations" in digest

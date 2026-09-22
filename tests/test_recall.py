from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ledger.parse import claim_from_mapping
from ledger.repository import add_claim
from recall.compose import build_digest, digest_path, read_recall, recall_day_for, write_digest
from recall.sources import activity_on, ledger_claims_on, recon_sessions_on


def _claim(claim_id: str, *, topic: str = "ledger", statement: str = "A recorded fact", created: str = "2026-07-02"):
    return claim_from_mapping(
        {
            "id": claim_id,
            "statement": statement,
            "topic": topic,
            "type": "event" if topic == "session-summary" else "fact",
            "sources": [{"ref": "docs/source.md", "quote": "evidence"}],
            "confidence": "high",
            "status": "active",
            "created": created,
            "updated": created,
        }
    )


RECON_RECORD = """# Recon Session verify-abc123__deadbeef

## Session Context

- Agent: `cursor`
- Model/Tool: `auto`
- Repository: `EVECOR`
- Branch: `main`
- Commit after: `b9cb6b270be857708d5462fe1ea400394c49275c`

## Files Changed

- `"a.py"`
- `"b.py"`

## Notes

- `"this backtick line must not be counted as a file"`
"""


def _write_recon(repo: Path, day: date, name: str, body: str) -> None:
    directory = repo / "storage" / "recon" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(body, encoding="utf-8")


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ledger_claims_on_filters_by_created_day(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-0001", created="2026-07-02"))
    add_claim(tmp_path, _claim("clm-2026-0002", created="2026-07-01"))

    ids = [c.id for c in ledger_claims_on(tmp_path, date(2026, 7, 2))]
    assert ids == ["clm-2026-0001"]


def test_recon_sessions_parse_fields_and_scope_file_count(tmp_path: Path) -> None:
    _write_recon(tmp_path, date(2026, 7, 2), "verify-abc123__deadbeef", RECON_RECORD)

    sessions = recon_sessions_on(tmp_path, date(2026, 7, 2))
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session"] == "verify-abc123"
    assert s["agent"] == "cursor"
    assert s["branch"] == "main"
    assert s["commit_after"].startswith("b9cb6b2")
    assert s["files_changed"] == 2  # the Notes backtick line is excluded


def test_activity_counts_both_ts_and_timestamp_fields(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "storage" / "ledger" / "receipts" / "claims.jsonl",
        ['{"timestamp": "2026-07-02T10:00:00+00:00"}', '{"timestamp": "2026-07-01T10:00:00+00:00"}'],
    )
    _write_jsonl(
        tmp_path / "storage" / "recon" / "observations.jsonl",
        ['{"ts": "2026-07-02T11:00:00+00:00"}'],
    )

    activity = activity_on(tmp_path, date(2026, 7, 2))
    assert activity["ledger_receipts"] == 1  # timestamp field honored
    assert activity["recon_observations"] == 1  # ts field honored


def test_build_digest_sections_and_summary_split(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-0001", topic="session-summary", statement="Did the work", created="2026-07-02"))
    add_claim(tmp_path, _claim("clm-2026-0002", topic="deploy", statement="Shipped it", created="2026-07-02"))
    _write_recon(tmp_path, date(2026, 7, 2), "verify-abc123__deadbeef", RECON_RECORD)

    digest = build_digest(tmp_path, date(2026, 7, 2))
    assert "# Recall — 2026-07-02" in digest
    assert "## Session summaries" in digest
    assert "Did the work" in digest
    assert "## Sessions recorded" in digest
    assert "commit `b9cb6b270be8`" in digest
    assert "## Claims recorded" in digest
    assert "Shipped it" in digest
    # session summary must not be duplicated in the plain-claims section
    assert digest.count("Did the work") == 1


def test_build_digest_empty_day(tmp_path: Path) -> None:
    digest = build_digest(tmp_path, date(2020, 1, 1))
    assert "No recorded activity" in digest


def test_read_recall_spans_days_newest_first(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-0001", topic="session-summary", statement="Newer", created="2026-07-02"))
    add_claim(tmp_path, _claim("clm-2026-0002", topic="session-summary", statement="Older", created="2026-07-01"))

    out = read_recall(tmp_path, days=2, end=date(2026, 7, 2))
    assert out.index("Newer") < out.index("Older")
    assert out.count("---") >= 1

    with pytest.raises(ValueError, match="days must be"):
        read_recall(tmp_path, days=0, end=date(2026, 7, 2))


def test_recall_day_uses_operator_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECALL_TIMEZONE", "America/New_York")
    evening_edT_next_utc_day = datetime(2026, 7, 3, 0, 0, 47, tzinfo=timezone.utc)

    assert recall_day_for(evening_edT_next_utc_day) == date(2026, 7, 2)


def test_write_digest_persists_readable_file(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-0001", topic="session-summary", statement="Persist me", created="2026-07-02"))

    path = write_digest(tmp_path, date(2026, 7, 2))
    assert path == digest_path(tmp_path, date(2026, 7, 2))
    assert path.is_file()
    assert (path.stat().st_mode & 0o004)  # world-readable
    assert "Persist me" in path.read_text(encoding="utf-8")


def test_recall_read_tool_clamps_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from recall.mcp_server import do_read

    monkeypatch.setenv("RECALL_REPO_ROOT", str(tmp_path))
    assert do_read(999)["days"] == 60
    assert do_read(0)["days"] == 1
    assert do_read(4)["ok"] is True


def test_recall_mcp_registers_one_read_only_tool() -> None:
    pytest.importorskip("mcp")
    import asyncio

    from recall.mcp_server import _build_server

    tools = asyncio.run(_build_server().list_tools())
    assert {t.name for t in tools} == {"recall_read"}

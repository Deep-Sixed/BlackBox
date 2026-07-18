"""Read-only adapters over the existing EVECOR memory stores.

Each function answers "what does store X hold for day D?" without mutating anything.
Recall composes these; it never writes to the source stores.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from ledger.models import ClaimRecord
from ledger.repository import load_claims, receipts_jsonl

_RECON_FIELD = {
    "agent": re.compile(r"^- Agent:\s*`([^`]*)`", re.MULTILINE),
    "model": re.compile(r"^- Model/Tool:\s*`([^`]*)`", re.MULTILINE),
    "repository": re.compile(r"^- Repository:\s*`([^`]*)`", re.MULTILINE),
    "branch": re.compile(r"^- Branch:\s*`([^`]*)`", re.MULTILINE),
    "commit_after": re.compile(r"^- Commit after:\s*`([^`]*)`", re.MULTILINE),
}
# Recon session reports are titled "Recon Session"; reports written between
# the 2026-07-16 Snitch-engine restore and the same-day rename carry the
# legacy "Snitch Session" title. Recall reads both eras.
_RECON_TITLE = re.compile(r"^#\s*(?:Recon|Snitch) Session\s*(.+?)\s*$", re.MULTILINE)
_RECON_FILES_SECTION = re.compile(r"^##\s*Files Changed\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


def _ledger_store(repo: Path) -> Path:
    """Ledger claim store root — env override for the split governance layout."""
    override = os.environ.get("RECALL_LEDGER_STORE")
    return Path(override).expanduser() if override else repo


def _recon_store(repo: Path) -> Path:
    """Recon session-record store — env override for the split governance layout."""
    override = os.environ.get("RECALL_RECON_STORE")
    return Path(override).expanduser() if override else repo / "storage" / "recon"


def _files_changed_count(text: str) -> int:
    section = _RECON_FILES_SECTION.search(text)
    if not section:
        return 0
    return sum(1 for line in section.group(1).splitlines() if line.lstrip().startswith("- `"))


def _claim_date(claim: ClaimRecord) -> date | None:
    raw = claim.created or claim.updated
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def ledger_claims_on(repo: Path, day: date) -> list[ClaimRecord]:
    """Claims created (or last updated) on the given day."""
    return [claim for claim in load_claims(_ledger_store(repo)) if _claim_date(claim) == day]


def recon_sessions_on(repo: Path, day: date) -> list[dict[str, Any]]:
    """Parsed recon session reports for one day.

    Date-first archive: <store>/YYYY/MM/DD/recon/*.md; legacy layouts kept
    the day directory itself: <store>/YYYY/MM/DD/*.md. Both are read.
    """
    day_dir = _recon_store(repo) / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    directory = day_dir / "recon" if (day_dir / "recon").is_dir() else day_dir
    if not directory.is_dir():
        return []

    sessions: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        title = _RECON_TITLE.search(text)
        record: dict[str, Any] = {
            "session": (title.group(1) if title else path.stem).split("__")[0],
            "files_changed": _files_changed_count(text),
        }
        for key, pattern in _RECON_FIELD.items():
            match = pattern.search(text)
            record[key] = match.group(1) if match else ""
        sessions.append(record)
    return sessions


def _count_jsonl_lines(path: Path) -> int:
    """Count records in a per-day journal — file placement defines the day."""
    if not path.is_file():
        return 0
    count = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += 1
    except OSError:
        return count
    return count


def _count_jsonl_on(path: Path, day: date) -> int:
    if not path.is_file():
        return 0
    prefix = day.isoformat()
    count = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = record.get("ts") or record.get("timestamp") or ""
                if isinstance(ts, str) and ts.startswith(prefix):
                    count += 1
    except OSError:
        return count
    return count


def activity_on(repo: Path, day: date) -> dict[str, int]:
    """Volume signal per store for the day (receipts / observations / wiki turns)."""
    receipts = receipts_jsonl(_ledger_store(repo))
    recon_store = _recon_store(repo)
    daily_journal = (
        recon_store
        / f"{day.year:04d}"
        / f"{day.month:02d}"
        / f"{day.day:02d}"
        / "recon"
        / "observations.jsonl"
    )
    if daily_journal.is_file():
        recon_count = _count_jsonl_lines(daily_journal)
    else:
        recon_count = _count_jsonl_on(recon_store / "observations.jsonl", day)
    return {
        "ledger_receipts": _count_jsonl_on(receipts, day),
        "recon_observations": recon_count,
        "wiki_turns": _count_jsonl_on(repo / "storage" / "wiki" / "raw" / "sessions" / "turns.jsonl", day),
    }

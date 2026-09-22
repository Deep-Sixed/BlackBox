"""Compose human-readable daily digests from the memory stores.

`build_digest` renders one day; `read_recall` composes the last N days live (never
stale); `write_digest` persists a day's digest under storage/recall/ for browsing and
for the compose timer. All three share one renderer, so a stored digest and a live
read are byte-identical for the same inputs.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ledger.repository import resolve_repo

from recall.sources import activity_on, ledger_claims_on, recon_sessions_on

SESSION_SUMMARY_TOPIC = "session-summary"
DEFAULT_TIMEZONE = "America/New_York"


def recall_timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("RECALL_TIMEZONE", DEFAULT_TIMEZONE))


def recall_day_for(moment: datetime) -> date:
    return moment.astimezone(recall_timezone()).date()


def recall_today() -> date:
    return recall_day_for(datetime.now(tz=recall_timezone()))


def _output_store(repo: Path) -> Path:
    """Digest output root — env override for split/archive layouts."""
    override = os.environ.get("RECALL_OUTPUT_STORE")
    return Path(override).expanduser() if override else repo / "storage" / "recall"


def _archive_layout() -> bool:
    """True when the canonical flight-recorder date-first archive is in use.

    There the digest lives inside the day folder: <store>/YYYY/MM/DD/recall/.
    """
    store = os.environ.get("FLIGHT_RECORDER_STORE")
    output = os.environ.get("RECALL_OUTPUT_STORE")
    return bool(store) and store == output


def digest_dir(repo: Path, day: date) -> Path:
    dated = _output_store(repo) / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    return dated / "recall" if _archive_layout() else dated


def digest_path(repo: Path, day: date) -> Path:
    return digest_dir(repo, day) / "digest.md"


def build_digest(repo: Path, day: date) -> str:
    repo = resolve_repo(repo)
    claims = ledger_claims_on(repo, day)
    summaries = [c for c in claims if c.topic == SESSION_SUMMARY_TOPIC]
    other_claims = [c for c in claims if c.topic != SESSION_SUMMARY_TOPIC]
    sessions = recon_sessions_on(repo, day)
    activity = activity_on(repo, day)

    lines: list[str] = [f"# Recall — {day.isoformat()}", ""]

    if summaries:
        lines.append("## Session summaries")
        for claim in summaries:
            lines.append(f"- {claim.statement} _({claim.id})_")
        lines.append("")

    if sessions:
        lines.append("## Sessions recorded")
        for s in sessions:
            who = "/".join(part for part in (s.get("agent"), s.get("model")) if part) or "unknown"
            where = "/".join(part for part in (s.get("repository"), s.get("branch")) if part)
            loc = f" on {where}" if where else ""
            lines.append(f"- `{s['session']}` — {who}{loc}")
            detail = []
            if s.get("files_changed"):
                detail.append(f"{s['files_changed']} files changed")
            if s.get("commit_after"):
                detail.append(f"commit `{s['commit_after'][:12]}`")
            if detail:
                lines.append(f"  - {', '.join(detail)}")
        lines.append("")

    if other_claims:
        lines.append("## Claims recorded")
        for claim in other_claims:
            lines.append(f"- **{claim.topic}** ({claim.type}): {claim.statement} _({claim.id})_")
        lines.append("")

    lines.append("## Activity")
    lines.append(
        f"- {activity['ledger_receipts']} ledger receipts, "
        f"{activity['recon_observations']} recon observations, "
        f"{activity['wiki_turns']} wiki turns"
    )

    if not (summaries or sessions or other_claims) and not any(activity.values()):
        lines = [f"# Recall — {day.isoformat()}", "", "_No recorded activity._"]

    return "\n".join(lines).rstrip() + "\n"


def read_recall(
    repo: Path,
    days: int = 4,
    *,
    end: date | None = None,
    persist: bool = False,
) -> str:
    """Live-composed digest for the last `days` days, newest first.

    With ``persist=True`` each composed day is also materialized to its
    ``digest.md`` (best-effort — a read must never fail on a write error).
    """
    repo = resolve_repo(repo)
    if days < 1:
        raise ValueError("days must be >= 1")
    end = end or recall_today()
    composed_days = [end - timedelta(days=offset) for offset in range(days)]
    parts = [build_digest(repo, day) for day in composed_days]
    if persist:
        for day in composed_days:
            try:
                write_digest(repo, day)
            except OSError:
                pass
    return "\n\n---\n\n".join(parts)


def write_digest(repo: Path, day: date | None = None) -> Path:
    """Persist a day's digest atomically; returns the path written."""
    repo = resolve_repo(repo)
    day = day or recall_today()
    path = digest_path(repo, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = build_digest(repo, day)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o644)  # operator must read; digests hold no secrets
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return path

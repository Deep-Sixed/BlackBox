"""Recon deployment paths — flight-recorder canonical date-first archive.

Default layout (one daily folder gives the complete view of a date):

    <flight-recorder>/storage/YYYY/MM/DD/recon/   audits + records + journal
    <flight-recorder>/storage/_state/recon-reservations/

`FLIGHT_RECORDER_STORE` overrides the archive root. The per-directory
`RECON_*` overrides (and their `SNITCH_*` legacy aliases) take precedence
over the archive layout — they exist for tests and ad-hoc redirection.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

FLIGHT_RECORDER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE_ROOT = FLIGHT_RECORDER_ROOT / "storage"


def archive_root() -> Path:
    override = os.environ.get("FLIGHT_RECORDER_STORE")
    return Path(override).expanduser() if override else DEFAULT_ARCHIVE_ROOT


def _day_dir(base: Path, day: date) -> Path:
    return base / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"


def _override(env_vars: tuple[str, ...]) -> Path | None:
    for env_var in env_vars:
        value = os.environ.get(env_var)
        if value:
            return Path(value).expanduser()
    return None


def audit_dir() -> Path:
    """Date-partitioned audit directory for today."""
    override = _override(("RECON_AUDIT_DIR", "SNITCH_AUDIT_DIR"))
    if override is not None:
        return _day_dir(override, date.today())
    return _day_dir(archive_root(), date.today()) / "recon"


def records_dir() -> Path:
    """Canonical JSON records live beside the day's audits."""
    override = _override(("RECON_RECORDS_DIR", "SNITCH_RECORDS_DIR"))
    if override is not None:
        return override
    return _day_dir(archive_root(), date.today()) / "recon"


def reservations_dir() -> Path:
    override = _override(("RECON_RESERVATIONS_DIR", "SNITCH_RESERVATIONS_DIR"))
    if override is not None:
        return override
    return archive_root() / "_state" / "recon-reservations"


def observations_log() -> Path:
    """Daily observation journal inside the day's recon folder."""
    override = os.environ.get("RECON_GATEWAY_LOG")
    if override:
        return Path(override).expanduser()
    return _day_dir(archive_root(), date.today()) / "recon" / "observations.jsonl"

"""Recall CLI — `recall read` / `recall compose`.

    recall read --days 4          # live digest for the last 4 days (stdout)
    recall compose [--day D]      # persist a day's digest under storage/recall/
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from recall.compose import read_recall, write_digest


def _repo(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recall", description="Human/handoff view over EVECOR memory stores.")
    parser.add_argument("--repo", default=None, help="repository root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="print a live digest for the last N days")
    read.add_argument("--days", type=int, default=4)

    compose = sub.add_parser("compose", help="persist a day's digest to storage/recall/")
    compose.add_argument("--day", default=None, help="ISO date YYYY-MM-DD (default: today, UTC)")

    args = parser.parse_args(argv)
    repo = _repo(args.repo)

    if args.command == "read":
        try:
            sys.stdout.write(read_recall(repo, days=args.days))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "compose":
        day = date.fromisoformat(args.day) if args.day else None
        path = write_digest(repo, day)
        print(f"wrote {path}")
        return 0

    return 1

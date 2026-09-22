from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from ledger.repository import load_claims, resolve_repo
from ledger.search import search_claims
from ledger.validate import validate_claims


def _format_valid_range(claim) -> str:
    start = claim.valid_from.isoformat() if claim.valid_from else "unknown"
    end = claim.valid_until.isoformat() if claim.valid_until else "now"
    return f"{start} -> {end}"


def cmd_check(repo: Path) -> int:
    claims = load_claims(repo)
    report = validate_claims(repo, claims)

    if report.errors:
        for err in report.errors:
            print(f"error: {err}", file=sys.stderr)
        print(
            f"ledger has {len(report.errors)} error(s), {len(report.warnings)} warning(s)",
            file=sys.stderr,
        )
        return 1

    warn_suffix = ""
    if report.warnings:
        warn_suffix = f", {len(report.warnings)} warning(s)"
        for warn in report.warnings:
            print(f"warning: {warn}", file=sys.stderr)

    print(f"ledger is consistent — {len(claims)} claims, 0 errors{warn_suffix}")
    return 0


def cmd_stats(repo: Path) -> int:
    claims = load_claims(repo)
    topics = sorted({c.topic for c in claims if c.topic})
    status_counts = Counter(c.status for c in claims)

    print(f"claims: {len(claims)}")
    topic_list = ", ".join(topics) if topics else "(none)"
    print(f"topics: {len(topics)}  -> {topic_list}")

    parts = [f"{name}={status_counts.get(name, 0)}" for name in sorted(status_counts)]
    print(f"status: {', '.join(parts)}")
    return 0


def cmd_search(repo: Path, query: str, as_of: date | None) -> int:
    claims = load_claims(repo)
    results = search_claims(claims, query, as_of=as_of)

    if not results:
        print("no matching claims")
        return 0

    for _score, claim in results:
        superseded = " [superseded]" if claim.status == "superseded" else ""
        print(f"{claim.id}{superseded}  ({claim.confidence})")
        print(f"  {claim.statement}")
        print(f"  valid: {_format_valid_range(claim)}")
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description="Verifiable knowledge ledger")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_repo_flag(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--repo",
            type=Path,
            default=None,
            help="Ledger repository root (default: cwd)",
        )

    check_parser = sub.add_parser("check", help="Validate ledger consistency")
    add_repo_flag(check_parser)

    stats_parser = sub.add_parser("stats", help="Show ledger statistics")
    add_repo_flag(stats_parser)

    search_parser = sub.add_parser("search", help="Search claims")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument(
        "--as-of",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Filter to claims valid on date (YYYY-MM-DD)",
    )
    add_repo_flag(search_parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = resolve_repo(args.repo)

    if args.command == "check":
        return cmd_check(repo)
    if args.command == "stats":
        return cmd_stats(repo)
    if args.command == "search":
        return cmd_search(repo, args.query, args.as_of)

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

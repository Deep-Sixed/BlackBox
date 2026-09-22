"""Explicit local CLI; input data and exception details never enter error output."""

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

from .db import connect
from .ingest import append_claim, ingest
from .query import claims, integrity, reconstruct, timeline


def main() -> int:
    parser = argparse.ArgumentParser(prog="blackbox")
    parser.add_argument("--database", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    capture = commands.add_parser("capture")
    capture.add_argument("--input", type=Path, required=True)
    capture.add_argument("--repo", type=Path)
    capture.add_argument("--baseline")
    show = commands.add_parser("show")
    show.add_argument("session")
    for name in ("timeline", "claims"):
        command = commands.add_parser(name)
        command.add_argument("--through", type=int)
        if name == "claims":
            command.add_argument("--topic")
    commands.add_parser("check")
    claim = commands.add_parser("claim")
    claim.add_argument("session")
    claim.add_argument("--input", type=Path, required=True)
    claim.add_argument("--target")
    claim.add_argument("--relation", choices=("supersedes", "contests"))
    args = parser.parse_args()
    try:
        match args.command:
            case "init":
                connect(args.database).close()
                result = {"status": "initialized"}
            case "capture":
                result = ingest(
                    args.database,
                    json.loads(args.input.read_text()),
                    repo=args.repo,
                    baseline=args.baseline,
                )
            case "show":
                result = reconstruct(args.database, args.session)
            case "timeline":
                result = timeline(args.database, through=args.through)
            case "claims":
                result = claims(args.database, through=args.through, topic=args.topic)
            case "claim":
                result = {
                    "claim_id": append_claim(
                        args.database,
                        args.session,
                        json.loads(args.input.read_text()),
                        target=args.target,
                        relation=args.relation,
                    )
                }
            case "check":
                result = integrity(args.database)
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ok"] else 1
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
        ValueError,
    ):
        # Validation exceptions often embed rejected inputs. Never print them.
        print(
            '{"error":"BlackBox operation failed; inspect input and local configuration"}'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

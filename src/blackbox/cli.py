"""Explicit local CLI; input data and exception details never enter error output."""

import argparse
import json
from pathlib import Path

from .api import (
    append_claim,
    check_integrity,
    get_claims,
    get_session,
    get_timeline,
    initialize,
)
from .api import (
    capture as capture_session,
)
from .errors import BlackBoxError, IntegrityError, SchemaError


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
                initialize(args.database)
                result = {"status": "initialized"}
            case "capture":
                result = capture_session(
                    args.database,
                    json.loads(args.input.read_text()),
                    repo=args.repo,
                    baseline=args.baseline,
                ).model_dump(mode="json")
            case "show":
                result = get_session(args.database, args.session).model_dump(
                    mode="json"
                )
            case "timeline":
                result = [
                    event.model_dump(mode="json")
                    for event in get_timeline(args.database, through=args.through)
                ]
            case "claims":
                result = [
                    claim.model_dump(mode="json")
                    for claim in get_claims(
                        args.database, through=args.through, topic=args.topic
                    )
                ]
            case "claim":
                result = {
                    "claim_id": append_claim(
                        args.database,
                        args.session,
                        json.loads(args.input.read_text()),
                        target=args.target,
                        relation=args.relation,
                    ).claim_id
                }
            case "check":
                result = check_integrity(args.database).model_dump(mode="json")
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ok"] else 1
        print(json.dumps(result, sort_keys=True))
        return 0
    except BlackBoxError as error:
        if args.command == "check":
            category = (
                "schema_integrity"
                if isinstance(error, SchemaError)
                else "sqlite_integrity"
                if isinstance(error, IntegrityError)
                else "database_unavailable"
            )
            result = {"ok": False, "schema_version": None, "errors": [category]}
        else:
            result = {"error": error.code, "retryable": error.retryable}
        print(json.dumps(result, sort_keys=True))
        return 1
    except json.JSONDecodeError, UnicodeError, OSError:
        print('{"error":"input_unavailable_or_invalid","retryable":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

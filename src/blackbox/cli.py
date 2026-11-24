"""Explicit local CLI; input data and exception details never enter error output."""

import argparse
import json
import os
import sys
import syslog
from pathlib import Path

from .api import (
    _capture_host_report,
    append_claim,
    check_integrity,
    get_chain_head,
    get_claims,
    get_session,
    get_timeline,
    initialize,
)
from .api import (
    capture as capture_session,
)
from .backup import backup as backup_database
from .client import Client, ClientError, read_token
from .errors import BlackBoxError, IntegrityError, SchemaError, ValidationError
from .hooks import digest_only_requests, hook_requests, load_hook_key, read_payload
from .models import CLAIM_RELATIONS
from .service.auth import new_token


def main() -> int:
    parser = argparse.ArgumentParser(prog="blackbox")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--database", type=Path)
    target.add_argument(
        "--socket", type=Path, help="talk to blackboxd instead of opening a database"
    )
    parser.add_argument(
        "--token-file", type=Path, help="private file holding the blackboxd token"
    )
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
    check = commands.add_parser("check")
    check.add_argument("--anchor", type=Path)
    head = commands.add_parser("head")
    head.add_argument(
        "--append", type=Path, help="append the head as one JSON line (anchor log)"
    )
    claim = commands.add_parser("claim")
    claim.add_argument("session")
    claim.add_argument("--input", type=Path, required=True)
    claim.add_argument("--target")
    claim.add_argument("--relation", choices=CLAIM_RELATIONS)
    hook = commands.add_parser("hook")
    hook.add_argument("--producer", default="claude-code")
    hook.add_argument("--git", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--to", type=Path, required=True)
    backup.add_argument("--keep", type=int, default=14)
    backup.add_argument("--group-readable", action="store_true")
    token = commands.add_parser("token")
    token.add_argument("action", choices=["new"])
    try:
        args = parser.parse_args()
    except SystemExit as error:
        # Exit 2 blocks the observed action in hook hosts such as Claude Code.
        if error.code == 2 and "hook" in sys.argv[1:]:
            raise SystemExit(1) from None
        raise
    if args.command == "token":
        token, token_hash = new_token()
        print(json.dumps({"token": token, "sha256": token_hash}, sort_keys=True))
        return 0
    if args.socket is not None:
        if args.command not in SOCKET_COMMANDS:
            print('{"error":"command_requires_database","retryable":false}')
            return 1
        return run_socket_hook(args) if args.command == "hook" else run_socket(args)
    if args.database is None:
        print('{"error":"database_or_socket_required","retryable":false}')
        return 1
    if args.command == "hook":
        return run_hook(args)
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
            case "head":
                result = get_chain_head(args.database).model_dump(mode="json")
                if args.append is not None:
                    append_anchor(args.append, result)
            case "backup":
                result = backup_database(
                    args.database,
                    args.to,
                    keep=args.keep,
                    group_readable=args.group_readable,
                )
            case "check":
                anchor = read_anchor(args.anchor) if args.anchor is not None else None
                result = check_integrity(args.database, anchor=anchor).model_dump(
                    mode="json"
                )
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ok"] else 1
        print(json.dumps(result, sort_keys=True))
        return 0
    except BlackBoxError as error:
        if args.command == "check" and not isinstance(error, ValidationError):
            category = (
                "schema_integrity"
                if isinstance(error, SchemaError)
                else "sqlite_integrity"
                if isinstance(error, IntegrityError)
                else "database_unavailable"
            )
            result = {
                "ok": False,
                "schema_version": None,
                "errors": [category],
                "first_broken_sequence": None,
            }
        else:
            result = {"error": error.code, "retryable": error.retryable}
        print(json.dumps(result, sort_keys=True))
        return 1
    except json.JSONDecodeError, UnicodeError, OSError, RecursionError:
        # RecursionError: input JSON nested deeper than the parser can follow.
        print('{"error":"input_unavailable_or_invalid","retryable":false}')
        return 1


SOCKET_COMMANDS = ("hook", "head", "check")


def append_anchor(path: Path, head: dict) -> None:
    """Append one anchor line durably; the log is private to its owner."""
    line = (json.dumps(head, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise OSError("anchor log must be private")
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_anchor(path: Path) -> dict:
    """A chain head as JSON, or the last line of an anchor log (JSON lines)."""
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise
        return json.loads(lines[-1])


def run_socket(args) -> int:
    try:
        client = Client(args.socket, read_token(args.token_file))
        if args.command == "head":
            head = client.request("GET", "/api/v1/chain-head")
            if args.append is not None:
                append_anchor(args.append, head)
            print(json.dumps(head, sort_keys=True))
            return 0
        query = None
        if args.anchor is not None:
            anchor = read_anchor(args.anchor)
            query = {"sequence": anchor["sequence"], "digest": anchor["digest"]}
        result = client.request("GET", "/api/v1/integrity", query=query)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") else 1
    except ClientError as error:
        print(json.dumps({"error": error.code, "retryable": True}, sort_keys=True))
        return 1
    except json.JSONDecodeError, KeyError, TypeError, UnicodeError, OSError:
        print('{"error":"input_unavailable_or_invalid","retryable":false}')
        return 1


def _warn(code: str) -> None:
    """Never silent: hosts may discard stderr, so failures also go to syslog."""
    print(json.dumps({"error": code}, sort_keys=True), file=sys.stderr)
    try:
        syslog.openlog("blackbox-hook", 0, syslog.LOG_USER)
        syslog.syslog(syslog.LOG_WARNING, f"host event not recorded: {code}")
    except OSError:
        pass


def run_socket_hook(args) -> int:
    """Send one host hook event to blackboxd. Never exits 2 (that would block).

    The payload is digested here with the local hook key; only names and the
    digest leave this process. The daemon decides whether a Git snapshot of the
    working directory is allowed.
    """
    try:
        key = load_hook_key()
        raw, content_digest = read_payload(sys.stdin.buffer, key)
        reason = "oversized" if raw is None else None
        if reason is None:
            try:
                event, git, repo = hook_requests(
                    raw,
                    producer=args.producer,
                    content_digest=content_digest,
                    git=args.git,
                )
            except ValueError, TypeError, RecursionError:
                reason = "unreadable"
        if reason is not None:
            event, git, repo = digest_only_requests(
                reason, content_digest, producer=args.producer, git=args.git
            )
        body = {"capture": event}
        if git is not None and repo is not None:
            body["git_cwd"] = str(repo)
        Client(args.socket, read_token(args.token_file), timeout=2.0).request(
            "POST", "/api/v1/host-reports", body
        )
    except ClientError as error:
        _warn(error.code)
        return 1
    except OSError:
        _warn("input_unavailable_or_invalid")
        return 1
    return 0


def run_hook(args) -> int:
    """Record one host hook event. Stdout stays empty: hosts may feed it to the agent.

    Never exits 2, which hook hosts treat as "block this action". Failures exit 1,
    a non-blocking error, with a bounded code on stderr.
    """
    try:
        key = load_hook_key()
        raw, content_digest = read_payload(sys.stdin.buffer, key)
        # A payload that cannot be recorded by name is still recorded by digest:
        # dropping it would let the agent hide a tool call by shaping its payload.
        reason = "oversized" if raw is None else None
        if reason is None:
            try:
                event, git, repo = hook_requests(
                    raw,
                    producer=args.producer,
                    content_digest=content_digest,
                    git=args.git,
                )
            except ValueError, TypeError, RecursionError:
                reason = "unreadable"
            else:
                try:
                    _capture_host_report(args.database, event)
                except ValidationError:
                    reason = "unreadable"
        if reason is not None:
            event, git, repo = digest_only_requests(
                reason, content_digest, producer=args.producer, git=args.git
            )
            _capture_host_report(args.database, event)
        if git is not None:
            capture_session(args.database, git, repo=repo)
        if reason == "unreadable":
            raise ValidationError()
    except BlackBoxError as error:
        result = {"error": error.code, "retryable": error.retryable}
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 1
    except OSError:
        print(
            '{"error":"input_unavailable_or_invalid","retryable":false}',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

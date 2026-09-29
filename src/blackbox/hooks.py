"""Map one host lifecycle-hook payload to capture requests; never store the payload.

The host runtime (for example Claude Code) runs `blackbox hook` on its own
lifecycle events and pipes the event as JSON on stdin. The acting agent neither
calls nor sees it. Only bounded metadata is kept: event and tool name, and a
HMAC-SHA-256 digest of the exact payload bytes. Tool inputs, outputs and prompts are
keyed-hashed, not stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import uuid
from pathlib import Path

MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
# Events after which the working tree is worth an independent Git snapshot.
TURN_END_EVENTS = ("Stop", "SubagentStop", "SessionEnd")
COMMAND_TOOLS = ("Bash",)
DIGEST_ONLY_NAMES = {
    "oversized": "OversizedHookPayload",
    "unreadable": "UnreadableHookPayload",
}

HOOK_KEY_ENV = "BLACKBOX_HOOK_HMAC_KEY_FILE"
DEFAULT_HOOK_KEY = "~/.config/blackbox/hook-hmac.key"


def load_hook_key(path: str | Path | None = None) -> bytes:
    """Load or create the local HMAC key used for hook payload digests.

    The key lives outside the BlackBox database so a database copy alone cannot
    be used to test guesses for low-entropy secrets embedded in hook payloads.
    """
    location = Path(
        path or os.environ.get(HOOK_KEY_ENV, DEFAULT_HOOK_KEY)
    ).expanduser().absolute()
    location.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_mode = location.parent.stat().st_mode
    if parent_mode & 0o022:
        raise OSError("hook key directory must not be writable by other users")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(location, flags)
    except FileNotFoundError:
        create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            created = os.open(location, create, 0o600)
        except FileExistsError:
            # Another hook process won first-use key creation. Use that key
            # rather than dropping this event.
            fd = os.open(location, flags)
        else:
            try:
                key = os.urandom(32)
                os.write(created, key)
                os.fsync(created)
            finally:
                os.close(created)
            directory = os.open(location.parent, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            fd = os.open(location, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise OSError("hook key must be a private regular file")
        key = os.read(fd, 4096)
    finally:
        os.close(fd)
    if len(key) < 32:
        raise OSError("hook key is too short")
    return key


def payload_digest(raw: bytes, key: bytes) -> str:
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def read_payload(stream, key: bytes) -> tuple[bytes | None, str]:
    """Read one payload and its HMAC-SHA-256 without buffering past the limit.

    Returns the bytes when they fit, else None: the rest is only hashed, so an
    oversized payload costs bounded memory and is still identified by digest.
    """
    digest = hmac.new(key, digestmod=hashlib.sha256)
    head = stream.read(MAX_PAYLOAD_BYTES + 1)
    digest.update(head)
    if len(head) <= MAX_PAYLOAD_BYTES:
        return head, digest.hexdigest()
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
    return None, digest.hexdigest()


def digest_only_requests(
    reason: str, content_digest: str, *, producer: str, git: bool = False
) -> tuple[dict, dict | None, str | None]:
    """Record that an event arrived even though it could not be recorded by name.

    `reason` is `oversized` for a payload too large to parse and `unreadable` for
    one that does not parse into a valid event. Dropping either would let an
    agent hide a tool call by making its payload huge or, for example, nested
    deeper than the parser follows. The event and tool names are unknown, so the
    event is recorded under its own name with the digest of every payload byte.
    Whether it ended a turn is unknown too, so with `git` it always gets a
    snapshot, of the directory the host ran the hook in: skipping it would let
    such an event hide the working tree.
    """
    capture = {
        "request_id": f"{reason}:{uuid.uuid4().hex}",
        "producer": producer,
        "observations": [
            {
                "source": producer,
                "kind": "activity",
                "name": DIGEST_ONLY_NAMES[reason],
                "content_digest": content_digest,
            }
        ],
    }
    if not git:
        return capture, None, None
    snapshot = {"request_id": capture["request_id"] + ":git", "producer": producer}
    return capture, snapshot, os.getcwd()


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("hook payload field missing")
    return value


def hook_requests(
    raw: bytes, *, producer: str, content_digest: str, git: bool = False
) -> tuple[dict, dict | None, str | None]:
    """Return the event capture, an optional Git capture and its repository.

    Tool events use the host's tool-use ID, so a redelivered event is a duplicate
    rather than a second record. Events without a host identifier get a random
    one: two identical Stop payloads are two separate stops.
    """
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("hook payload too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("hook payload must be a JSON object")
    event = _text(payload, "hook_event_name")
    session = _text(payload, "session_id")
    name = event
    kind = "activity"
    unique = None
    if event in TOOL_EVENTS:
        tool = _text(payload, "tool_name")
        name = f"{event}:{tool}"
        kind = "command" if tool in COMMAND_TOOLS else "activity"
        tool_use = payload.get("tool_use_id")
        if isinstance(tool_use, str) and tool_use:
            unique = tool_use
    request_id = f"{session}:{event}:{unique or uuid.uuid4().hex}"
    capture = {
        "request_id": request_id,
        "producer": producer,
        "observations": [
            {
                "source": producer,
                "kind": kind,
                "name": name,
                "content_digest": content_digest,
            }
        ],
    }
    cwd = payload.get("cwd")
    if not (git and event in TURN_END_EVENTS and isinstance(cwd, str) and cwd):
        return capture, None, None
    return capture, {"request_id": request_id + ":git", "producer": producer}, cwd

"""Map one host lifecycle-hook payload to capture requests; never store the payload.

The host runtime (for example Claude Code) runs `blackbox hook` on its own
lifecycle events and pipes the event as JSON on stdin. The acting agent neither
calls nor sees it. Only bounded metadata is kept: event and tool name, and a
SHA-256 digest of the exact payload bytes. Tool inputs, outputs and prompts are
hashed, not stored.
"""

from __future__ import annotations

import hashlib
import json
import uuid

MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
# Events after which the working tree is worth an independent Git snapshot.
TURN_END_EVENTS = ("Stop", "SubagentStop", "SessionEnd")
COMMAND_TOOLS = ("Bash",)


def read_payload(stream) -> tuple[bytes | None, str]:
    """Read one payload and its SHA-256 without buffering more than the limit.

    Returns the bytes when they fit, else None: the rest is only hashed, so an
    oversized payload costs bounded memory and is still identified by digest.
    """
    digest = hashlib.sha256()
    head = stream.read(MAX_PAYLOAD_BYTES + 1)
    digest.update(head)
    if len(head) <= MAX_PAYLOAD_BYTES:
        return head, digest.hexdigest()
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
    return None, digest.hexdigest()


def oversized_request(content_digest: str, *, producer: str) -> dict:
    """Record that an event arrived even though its payload was too large to parse.

    Dropping it would let an agent hide a tool result by making it huge. The
    event and tool names are unknown without parsing, so it is recorded under
    its own name with the digest of every payload byte.
    """
    return {
        "request_id": f"oversized:{uuid.uuid4().hex}",
        "producer": producer,
        "observations": [
            {
                "source": producer,
                "kind": "activity",
                "name": "OversizedHookPayload",
                "content_digest": content_digest,
            }
        ],
    }


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("hook payload field missing")
    return value


def hook_requests(
    raw: bytes, *, producer: str, git: bool = False
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
                "content_digest": hashlib.sha256(raw).hexdigest(),
            }
        ],
    }
    cwd = payload.get("cwd")
    if not (git and event in TURN_END_EVENTS and isinstance(cwd, str) and cwd):
        return capture, None, None
    return capture, {"request_id": request_id + ":git", "producer": producer}, cwd

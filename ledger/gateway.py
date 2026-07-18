"""Ledger gateway connector — TCP HTTP observer for ContextForge cpex tool_post_invoke.

Receives {surface, request_id, tool, result} from the ledger_writer cpex plugin and
appends a redacted passive receipt. Receipts are this process's ONLY store; canonical
claims are written exclusively by the Ledger MCP service (ledger.mcp_server) through
direct library calls. The receipt JSONL is not the Ledger system of record.

Environment:
    LEDGER_GATEWAY_HOST     TCP host (default: 127.0.0.1)
    LEDGER_GATEWAY_PORT     TCP port (default: 8774)
    LEDGER_REPO_ROOT        Repository root (default: cwd)

Start:
    LEDGER_REPO_ROOT=/mnt/jarvis-data/projects/EVECOR/AgentSync ledger-gateway
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, Response

from ledger.repository import append_receipt, resolve_repo

logger = logging.getLogger("ledger.gateway")

app = FastAPI(title="ledger-gateway", docs_url=None, redoc_url=None)


def _repo() -> Path:
    return resolve_repo(Path(os.environ["LEDGER_REPO_ROOT"]) if os.environ.get("LEDGER_REPO_ROOT") else None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _receipt_from_body(body: dict[str, Any]) -> dict[str, Any]:
    status = _str_or_empty(body.get("status")) or _str_or_empty(body.get("outcome")) or "observed"
    return {
        "schema_version": "ledger.receipt.v1",
        "receipt_id": f"rcpt-{uuid4()}",
        "timestamp": _now(),
        "surface": _str_or_empty(body.get("surface")),
        "tool": _str_or_empty(body.get("tool")),
        "agent_identity": _str_or_empty(body.get("agent_identity") or body.get("agent")),
        "user_identity": _str_or_empty(body.get("user_identity") or body.get("user")),
        "session_id": _str_or_empty(body.get("session_id")),
        "request_id": _str_or_empty(body.get("request_id")),
        "correlation_id": _str_or_empty(body.get("correlation_id") or body.get("request_id") or body.get("session_id")),
        "outcome": status,
        "status": status,
        "result_type": type(body.get("result")).__name__ if "result" in body else "",
    }


@app.post("/v1/record")
async def record(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return Response(content='{"ok":false,"error":"bad_json"}', status_code=400, media_type="application/json")
    if not isinstance(body, dict):
        return Response(content='{"ok":false,"error":"bad_receipt"}', status_code=400, media_type="application/json")

    receipt = _receipt_from_body(body)
    try:
        append_receipt(_repo(), receipt)
    except Exception as exc:
        logger.warning("ledger.gateway: failed to write receipt: %r", exc)
    logger.debug("ledger.gateway: receipted %s/%s", receipt["surface"], receipt["tool"])
    return Response(content='{"ok":true}', status_code=200, media_type="application/json")


@app.get("/healthz")
async def healthz() -> Response:
    return Response(content='{"ok":true}', status_code=200, media_type="application/json")


def main() -> None:
    host = os.environ.get("LEDGER_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("LEDGER_GATEWAY_PORT", "8774"))
    logging.basicConfig(level=logging.INFO)
    logger.info("ledger-gateway starting on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

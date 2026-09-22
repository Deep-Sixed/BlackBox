"""Recall MCP Service — human/handoff read view, direct store access.

Single FastMCP process (runs as agentsync-svc) exposing one read-only tool,
`recall_read`, so an incoming agent can catch up on the last N days in one call
instead of being pointed at a folder. Like Ledger and Recon it is not something an
agent is told to write to — it composes what those stores already captured.

Run locally (stdio):   python -m recall.mcp_server
Run as HTTP service:   RECALL_MCP_TRANSPORT=http python -m recall.mcp_server

Environment variables
---------------------
RECALL_REPO_ROOT       repository root (default: cwd)
RECALL_MCP_TRANSPORT   stdio (default) | http
RECALL_MCP_PORT        HTTP port when transport=http (default 8587)
RECALL_MCP_LOG_LEVEL   logging level (default INFO)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ledger.repository import resolve_repo

from recall.compose import read_recall

logger = logging.getLogger("recall.mcp")

READ_DAYS_DEFAULT = 4
READ_DAYS_MAX = 60


def _repo() -> Path:
    return resolve_repo(Path(os.environ["RECALL_REPO_ROOT"]) if os.environ.get("RECALL_REPO_ROOT") else None)


def do_read(days: int = READ_DAYS_DEFAULT) -> dict:
    days = max(1, min(READ_DAYS_MAX, int(days)))
    return {"ok": True, "days": days, "digest": read_recall(_repo(), days=days)}


def _build_server(port: int = 8587):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # FastMCP's default DNS-rebinding allowlist only covers 127.0.0.1/localhost.
    # Docker Desktop containers reach this host as host.docker.internal, so
    # that name needs to be allowed too or streamable-http POSTs get a 421.
    mcp = FastMCP(
        "recall",
        port=port,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*"]
        ),
    )

    @mcp.tool(
        name="recall_read",
        annotations={
            "title": "Read back the last N days of session activity (human/handoff digest)",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    def recall_read(days: int = READ_DAYS_DEFAULT) -> dict:
        """Return a human-readable digest of the last `days` days (default 4, max 60).

        Composes session summaries, recorded sessions, claims, and activity volume
        from the Ledger and Recon stores. Use this to pick up work a colleague agent
        left off — e.g. "read back the past 4 days".
        """
        return do_read(days)

    return mcp


def _configure_logging() -> None:
    level_name = os.environ.get("RECALL_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    _configure_logging()
    transport = os.environ.get("RECALL_MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("RECALL_MCP_PORT", "8587"))
        logger.info("recall-mcp starting (http, port %s, repo %s)", port, _repo())
        _build_server(port=port).run(transport="streamable-http")
    else:
        _build_server().run()


if __name__ == "__main__":
    main()

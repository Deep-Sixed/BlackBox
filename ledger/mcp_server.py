"""Ledger MCP Service — explicit claim operations, direct store access.

Single FastMCP process exposing the deliberate Ledger layer. No shims: this
process runs as ``agentsync-svc`` and operates on the claim store directly
through the ledger library. It is the ONLY writer of canonical claims. The
passive receipt path (CPEX ``tool_post_invoke`` → gateway ``/v1/record`` →
receipts JSONL) is a separate process with a separate store; the gateway never
writes claims and this service never writes receipts.

Deployment model
----------------
  Agent / A2A client
    ↓
  ContextForge (:4444)
    ↓
  Ledger MCP Service  (this file, :8586, runs as agentsync-svc)
    ↓  direct library calls
  storage/ledger/claims/*.md   (canonical, append-only)

Tool contract
-------------
  ledger_add_claim   record a new atomic claim
  ledger_supersede   replace a stale claim with a linked successor
  ledger_contest     dispute a claim via a new linked contest claim
  ledger_search      BM25 query with optional as-of temporal window
  ledger_timeline    chronological chain / as-of snapshot (read-only)
  ledger_audit       validate the whole store on demand (read-only)

Identity is implicit from the ContextForge/MCP request context; tools carry no
identity parameters and agents cannot self-attest attribution.

Run locally (stdio):   python -m ledger.mcp_server
Run as HTTP service:   LEDGER_MCP_TRANSPORT=http python -m ledger.mcp_server

Environment variables
---------------------
LEDGER_REPO_ROOT       repository root (default: cwd)
LEDGER_MCP_TRANSPORT   stdio (default) | http
LEDGER_MCP_PORT        HTTP port when transport=http (default 8586)
LEDGER_MCP_LOG_LEVEL   logging level (default INFO)
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ledger.models import CLAIM_TYPES, SourceRef
from ledger.parse import claim_from_mapping
from ledger.repository import add_claim, contest_linked, load_claims, resolve_repo, supersede
from ledger.search import search_claims
from ledger.timeline import build_timeline
from ledger.validate import validate_claims

logger = logging.getLogger("ledger.mcp")

WRITE_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
SEARCH_LIMIT_DEFAULT = 20
SEARCH_LIMIT_MAX = 100


class SourceInput(BaseModel):
    """One evidence pointer backing a claim."""

    ref: str = Field(description="Repo-relative file path or artifact anchor reference.")
    quote: str = Field(description="Verbatim anchor text extracted from the source material.")
    locator: str | None = Field(default=None, description="Optional line range or structural pointer.")
    source_type: str | None = Field(
        default="repo-file",
        description="Evidence classification; repo-file references are verified by ledger audit.",
    )
    source_hash: str | None = Field(default=None, description="Optional sha256:-prefixed source digest.")


def _repo() -> Path:
    return resolve_repo(Path(os.environ["LEDGER_REPO_ROOT"]) if os.environ.get("LEDGER_REPO_ROOT") else None)


def _parse_as_of(value: str | None) -> date | None:
    return date.fromisoformat(str(value)[:10]) if value else None


def _source_refs(sources: list[SourceInput]) -> list[SourceRef]:
    return [
        SourceRef(
            ref=source.ref,
            quote=source.quote,
            locator=source.locator,
            source_type=source.source_type,
            source_hash=source.source_hash,
        )
        for source in sources
    ]


def _build_claim(
    *,
    topic: str,
    type: str,
    statement: str,
    sources: list[SourceInput],
    confidence: str,
    valid_from: str | None,
):
    if type not in CLAIM_TYPES:
        raise ValueError(f"invalid claim type '{type}'; expected one of {sorted(CLAIM_TYPES)}")
    if confidence not in WRITE_CONFIDENCE_LEVELS:
        raise ValueError(f"invalid confidence '{confidence}'; expected one of {sorted(WRITE_CONFIDENCE_LEVELS)}")
    if not statement.strip():
        raise ValueError("statement is required")
    if not topic.strip():
        raise ValueError("topic is required")
    if not sources:
        raise ValueError("at least one source is required")
    mapping: dict[str, Any] = {
        "topic": topic,
        "type": type,
        "statement": statement,
        "sources": [source.model_dump(exclude_none=True) for source in sources],
        "confidence": confidence,
        "status": "active",
    }
    if valid_from:
        mapping["valid_from"] = valid_from
    return claim_from_mapping(mapping)


def _search_result(score: float, claim, *, include_relations: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "claim_id": claim.id,
        "score": round(score, 4),
        "statement": claim.statement,
        "topic": claim.topic,
        "type": claim.type,
        "status": claim.status,
        "confidence": claim.confidence,
        "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
        "valid_until": claim.valid_until.isoformat() if claim.valid_until else None,
        "sources": [{"ref": s.ref, "quote": s.quote, "locator": s.locator} for s in claim.sources],
    }
    if include_relations:
        result.update(
            {
                "supersedes": claim.supersedes,
                "superseded_by": claim.superseded_by,
                "contradicts": claim.contradicts,
                "relations": claim.relations,
                "note": claim.note,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Tool implementations — plain functions so they are testable without `mcp`.
# ---------------------------------------------------------------------------

def do_add_claim(
    topic: str,
    type: str,
    statement: str,
    sources: list[SourceInput],
    confidence: str = "high",
    valid_from: str | None = None,
) -> dict:
    claim = add_claim(
        _repo(),
        _build_claim(topic=topic, type=type, statement=statement, sources=sources, confidence=confidence, valid_from=valid_from),
    )
    return {"ok": True, "claim_id": claim.id}


def do_supersede(
    superseded_id: str,
    topic: str,
    type: str,
    statement: str,
    sources: list[SourceInput],
    confidence: str = "high",
    valid_from: str | None = None,
) -> dict:
    claim = supersede(
        _repo(),
        superseded_id,
        _build_claim(topic=topic, type=type, statement=statement, sources=sources, confidence=confidence, valid_from=valid_from),
    )
    return {"ok": True, "claim_id": claim.id, "superseded_id": superseded_id}


def do_contest(target_id: str, rationale: str, sources: list[SourceInput]) -> dict:
    if not sources:
        raise ValueError("contest requires at least one evidence source")
    contest_claim, target = contest_linked(
        _repo(),
        target_id,
        rationale=rationale,
        sources=_source_refs(sources),
    )
    return {"ok": True, "contest_claim_id": contest_claim.id, "contested_claim_id": target.id}


def do_search(
    query: str,
    topic: str | None = None,
    as_of: str | None = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
    include_relations: bool = False,
) -> dict:
    if not query.strip():
        raise ValueError("query is required")
    limit = max(1, min(SEARCH_LIMIT_MAX, int(limit)))
    claims = load_claims(_repo())
    if topic:
        claims = [claim for claim in claims if claim.topic == topic]
    scored = search_claims(claims, query, as_of=_parse_as_of(as_of), limit=limit)
    return {
        "ok": True,
        "limit": limit,
        "results": [_search_result(score, claim, include_relations=include_relations) for score, claim in scored],
    }


def do_timeline(topic: str | None = None, claim_id: str | None = None, as_of: str | None = None) -> dict:
    return build_timeline(load_claims(_repo()), topic=topic, claim_id=claim_id, as_of=_parse_as_of(as_of))


def do_audit() -> dict:
    repo = _repo()
    claims = load_claims(repo)
    report = validate_claims(repo, claims)
    return {
        "ok": report.ok,
        "claim_count": len(claims),
        "errors": report.errors,
        "warnings": report.warnings,
    }


# ---------------------------------------------------------------------------
# Server construction (lazy so the module stays importable without `mcp`)
# ---------------------------------------------------------------------------

def _build_server(port: int = 8586):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # FastMCP's default DNS-rebinding allowlist only covers 127.0.0.1/localhost.
    # Docker Desktop containers reach this host as host.docker.internal, so
    # that name needs to be allowed too or streamable-http POSTs get a 421.
    mcp = FastMCP(
        "ledger",
        port=port,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*"]
        ),
    )
    write = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
    read = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

    @mcp.tool(name="ledger_add_claim", annotations={"title": "Record a new atomic claim in the verification ledger", **write})
    def ledger_add_claim(
        topic: str,
        type: str,
        statement: str,
        sources: list[SourceInput],
        confidence: str = "high",
        valid_from: str | None = None,
    ) -> dict:
        """Record a new intentional atomic claim with explicit sources.

        type must be one of: fact, event, definition, claim, metric, relation.
        Session summaries are type=event with topic=session-summary.
        valid_from is an optional ISO date (YYYY-MM-DD).
        Returns {"ok": true, "claim_id": "clm-..."}.
        """
        return do_add_claim(topic, type, statement, sources, confidence, valid_from)

    @mcp.tool(name="ledger_supersede", annotations={"title": "Supersede a stale claim with a linked successor", **write})
    def ledger_supersede(
        superseded_id: str,
        topic: str,
        type: str,
        statement: str,
        sources: list[SourceInput],
        confidence: str = "high",
        valid_from: str | None = None,
    ) -> dict:
        """Mark an out-of-date claim superseded and write its replacement.

        The old claim keeps its history; reciprocal supersedes/superseded_by
        links bind the two. Returns {"ok": true, "claim_id": "<new id>"}.
        """
        return do_supersede(superseded_id, topic, type, statement, sources, confidence, valid_from)

    @mcp.tool(name="ledger_contest", annotations={"title": "Dispute a claim via a new linked contest claim", **write})
    def ledger_contest(target_id: str, rationale: str, sources: list[SourceInput]) -> dict:
        """Flag a claim as contested without erasing it.

        Writes a NEW atomic contest claim carrying the evidence, linked via
        relations ["disputes:<target_id>"]; the target's body is never
        rewritten, only its status/confidence become contested.
        Returns {"ok": true, "contest_claim_id": ..., "contested_claim_id": ...}.
        """
        return do_contest(target_id, rationale, sources)

    @mcp.tool(name="ledger_search", annotations={"title": "Search ledger claims (BM25, optional as-of window)", **read})
    def ledger_search(
        query: str,
        topic: str | None = None,
        as_of: str | None = None,
        limit: int = 20,
        include_relations: bool = False,
    ) -> dict:
        """Query the append-only claim store.

        as_of is an ISO date (YYYY-MM-DD) reconstructing what was valid then.
        limit defaults to 20, hard max 100. Results are compact provenance by
        default; include_relations=true adds supersession/contradiction links.
        """
        return do_search(query, topic, as_of, limit, include_relations)

    @mcp.tool(name="ledger_timeline", annotations={"title": "Trace how claims changed over time / as-of snapshot", **read})
    def ledger_timeline(
        topic: str | None = None,
        claim_id: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Chronological reconstruction of the claim record.

        Pass topic for all claims on a topic, or claim_id to walk that claim's
        full supersession chain. as_of (ISO date) additionally returns the
        snapshot of claims valid on that date.
        """
        return do_timeline(topic, claim_id, as_of)

    @mcp.tool(name="ledger_audit", annotations={"title": "Validate the whole claim store on demand", **read})
    def ledger_audit() -> dict:
        """Run the full ledger consistency check (same rules as `ledger check`).

        Returns {"ok": bool, "claim_count": int, "errors": [...], "warnings": [...]}.
        """
        return do_audit()

    # Recall — derived-memory capability hosted in the Ledger runtime.
    # Its code, storage, and output stay logically separate from Ledger
    # claims: Recall READS Ledger + Recon and materializes daily digests
    # under the Recall store. Agents keep writing only to Ledger and Recon.
    from recall.compose import read_recall as _recall_read_compose

    RECALL_READ_DAYS_DEFAULT = 4
    RECALL_READ_DAYS_MAX = 60

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
    def recall_read(days: int = RECALL_READ_DAYS_DEFAULT) -> dict:
        """Return a human-readable digest of the last `days` days (default 4, max 60).

        Composes session summaries, recorded sessions, claims, and activity volume
        from the Ledger and Recon stores. Use this to pick up work a colleague agent
        left off — e.g. "read back the past 4 days". Each composed day is also
        materialized to its digest.md under the Recall store (on-demand refresh).
        """
        days = max(1, min(RECALL_READ_DAYS_MAX, int(days)))
        return {"ok": True, "days": days, "digest": _recall_read_compose(_repo(), days=days, persist=True)}

    return mcp


def _configure_logging() -> None:
    level_name = os.environ.get("LEDGER_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    _configure_logging()
    transport = os.environ.get("LEDGER_MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("LEDGER_MCP_PORT", "8586"))
        logger.info("ledger-mcp starting (http, port %s, repo %s)", port, _repo())
        _build_server(port=port).run(transport="streamable-http")
    else:
        _build_server().run()


if __name__ == "__main__":
    main()

# ledger (AgentSync integration)

Vendored from the clean-room standalone baseline at `/mnt/jarvis-data/projects/ledger-llm`.

- **Baseline tag:** `clean-room-v0.1-standalone` (standalone repo — not this copy)
- **Package name here:** `ledger` (imports: `from ledger...`)
- **CLI:** `uv run ledger check|stats|search`
- **Gateway:** `ledger-gateway` listens on loopback TCP `127.0.0.1:8774` by default.
- **Canonical claims:** Markdown claim files under `storage/ledger/claims/`.
- **Passive receipts:** `storage/ledger/receipts/claims.jsonl`; this path keeps the
  historical filename but is not the system of record.

Phase 0/1 Ledger writes use explicit ClaimRecord operations:

- `add_claim(repo, claim)`
- `supersede(repo, old_claim_id, new_claim)`
- `contest(repo, claim_id, reason=..., contested_by=...)`

The gateway (`gateway.py`) exposes the loopback HTTP observer used by the EVECOR gateway stack's
`LedgerWriter` plugin — receipts only, no claim endpoints:

- `GET /healthz`
- `POST /v1/record` for passive receipts

## MCP exposure (Phase 2/3, no shims)

`mcp_server.py` is the explicit claim-operations service: a FastMCP process
(`evecor-ledger-mcp.service`, streamable-http on `127.0.0.1:8586`, runs as
`agentsync-svc`) registered into ContextForge `:4444` as gateway `ledger-mcp`.
It operates on the claim store **directly through the ledger library** — no HTTP
hop, no middle tier. One writer per store:

- gateway (:8774) writes `storage/ledger/receipts/claims.jsonl` only
- ledger-mcp (:8586) writes `storage/ledger/claims/*.md` only

Tools: `ledger_add_claim`, `ledger_supersede`, `ledger_contest`, `ledger_search`,
`ledger_timeline` (chronological chain / as-of snapshot), `ledger_audit`
(on-demand `ledger check`). `ledger_contest` writes a NEW linked contest claim
(`relations: ["disputes:<target_id>"]`, `contradicts: [<target_id>]`); the target's
body is never rewritten, only its status/confidence become contested plus a pointer
note. Identity is implicit from ContextForge/MCP request context; tools carry no
identity parameters (no self-attestation). The passive CPEX receipt path also
observes these tool calls, so explicit claim writes are themselves receipted.

## Source-ref base convention

New claim `sources[].ref` paths default to `source_type: repo-file` and are verified
relative to the **AgentSync repo root** or its enclosing EVECOR checkout. `ledger check`
must be run with `--repo /mnt/jarvis-data/projects/EVECOR/AgentSync` (the systemd check
unit does this). Do not run it with `--repo storage/ledger`: that makes repo-relative
refs like `src/ledger/repository.py` and `src/recall/compose.py` look missing, which is
an operator error rather than ledger corruption. Legacy claims created before source
types were required may cite moved files, terminal output, or external artifacts. The
former ambiguous `source_type: file` is also legacy. An unresolved legacy reference is
reported as a warning, not a failed consistency check.
The audit also recognizes the narrowly defined EVECOR migration aliases for the shared
projects `.stignore`, the relocated LiteLLM local compose file, and the archived
`AI_SYNC_LEDGER.md`; claim files remain immutable.
The top-level `evidence -> storage/ledger/evidence` symlink is a compat shim for the one
pre-Phase-2 claim written with a `storage/ledger`-relative ref; removable if that claim
is ever retracted.

Receipts are enriched with schema version, timestamp, receipt id, surface/tool,
cpex `GlobalContext` attribution from the plugin envelope, session/request correlation,
and outcome/status. Receipts must not include prompts, payload bodies, result bodies,
bearer tokens, environment dumps, or sensitive tool arguments.

Claim writes create Markdown files under `storage/ledger/claims/`; `claims.jsonl` is not
the canonical store.

`ops/systemd/agentsync-ledger-check.service` and `.timer` run `ledger check` periodically
so claim-store drift is visible instead of silent.

This directory is the AgentSync integration layer. Audit trail and IP boundary
documentation live in the standalone repo (`CLEAN_ROOM.md`).

Do not treat edits here as retroactive changes to the clean-room baseline unless
explicitly re-synced and re-tagged upstream.

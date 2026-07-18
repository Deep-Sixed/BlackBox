# Flight Recorder

EVECOR's governance flight-recorder software group — one group, three
distinct products:

- **Recon** (`recon/`) captures agent-session evidence. Stop hooks POST to
  the gateway on :8773; each `session_complete` finalizes a
  `recon_<session>.md` audit with self-verifying git evidence receipts.
- **Ledger** (`ledger/`) stores authoritative claims. Append-only;
  corrections via `ledger_supersede`, disputes via `ledger_contest`. MCP
  service on :8586, federated through ContextForge as `ledger-mcp-*`.
- **Recall** (`recall/`) builds derived memory from both: daily audit
  digests for agent handoff (`recall_read(days=N)`), hosted in the Ledger
  runtime. Agents never write to Recall.

Tool names, schemas, permissions, and service identities stay separate.
Ledger + Recall share one runtime; Recon runs as its own capture service.

## Canonical date-first archive

```
storage/YYYY/MM/DD/
├── ledger/    claims recorded that day (clm-*.md)
├── recon/     session audits, canonical JSON records, observations.jsonl
└── recall/    digest.md — the day's composed view
```

One daily folder is the complete view of that date's activity. Shared
non-daily state: `storage/receipts/` (ledger observer journal),
`storage/evidence/` (checked-in claim evidence), `storage/_state/`
(recon reservations), `storage/archive/` (frozen pre-ledger audit
archive, including Snitch-era reports).

Deployment sets one env var, `FLIGHT_RECORDER_STORE`, pointing at
`storage/`; every writer and reader derives its paths from it.

## Operate

```bash
systemctl status evecor-ledger-mcp          # :8586 MCP (ledger_* + recall_read)
systemctl --user status evecor-recon-gateway    # :8773 capture
systemctl --user status evecor-ledger-gateway   # :8774 observer journal
systemctl list-timers evecor-recall-compose.timer  # daily digest 23:50
.venv/bin/pytest tests/ -q
```

Unit files are versioned in `deploy/`.

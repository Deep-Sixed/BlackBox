# recall (AgentSync)

**Recall is the human/handoff view over the EVECOR memory stores.** It does not capture
anything — Recon (session flight-recorder) and the Ledger (verifiable claims + passive
receipts) already capture. Recall *composes* what they hold into human-readable daily
digests, so an operator — or an incoming agent picking up a colleague's work — can read
back the last N days in one place.

This is the successor to the old `/mnt/jarvis-data/projects/Audits/` session-log tree.
Nothing is told to "report to Recall"; like Ledger and Recon it just happens.

## Layers

- **Recon** → memory: per-session flight-recorder records (files changed, commits, branch).
- **Ledger** → memory: verifiable claims (incl. `session-summary`) + passive receipts.
- **Recall** → *reading* memory: daily digests composed from the two above.

## Interfaces

- **MCP** (`evecor-recall-mcp.service`, streamable-http `127.0.0.1:8587`, `agentsync-svc`),
  registered in ContextForge `:4444` as `recall-mcp`. One tool:
  - `recall_read(days=4)` — digest of the last N days (default 4, max 60). "Read back the
    past 4 days" to pick up where another agent left off.
- **CLI**: `recall read --days N` (live to stdout) · `recall compose [--day D]` (persist).
- **Timer** (`evecor-recall-compose.timer`): persists the current digest under
  `storage/recall/YYYY/MM/DD/digest.md` every 30 min, so the folder is always browseable.

Recall day boundaries use `America/New_York` by default (`RECALL_TIMEZONE` overrides).
This matches operator handoff expectations; UTC evening rollover should not make
`recall_read(days=1)` appear empty while the local workday is still active.

## Composition

Per day, `build_digest` assembles:

- **Session summaries** — ledger claims with `topic: session-summary` (the narrative).
- **Sessions recorded** — parsed recon records (agent/model, repo/branch, commit, file count).
- **Claims recorded** — other ledger claims created that day (the atomic facts).
- **Activity** — receipt / observation / wiki-turn counts (volume signal).

Digests are world-readable (`0644`); they contain no secrets — only what the source
stores already hold under redaction policy.

## Archive

The frozen pre-Recall audit tree lives at `storage/recall/archive/` (the old
`/mnt/jarvis-data/projects/Audits/` path is now a symlink to it). Read-only history.

# Recon

EVECOR's flight recorder. The session engine
(`src/recon/session_record.py`) is restored from Project Snitch
(`/mnt/jarvis-data/projects/third-party/GitHubs/Recon`): normalized,
redacted, append-only session records with self-verifying evidence
receipts, rendered as `recon_<session>.md` audit reports titled
`# Recon Session <id>`.

**Legacy alias:** artifacts written before the 2026-07-16 rename use
`snitch_<session>.md` filenames and `# Snitch Session` headings. Readers
(including Recall's digest composer) accept both; nothing writes the
`snitch` name anymore. `SNITCH_*` env vars remain as documented fallbacks
for their `RECON_*` equivalents.

## Pipeline

1. Surface stop hooks (`~/.config/Claude/hooks/evecor-contextforge-stop.sh`)
   POST session-end events to `http://127.0.0.1:8773/v1/record`.
2. The gateway (`recon.gateway`, systemd user unit `evecor-recon-gateway`)
   appends every event to `storage/recon/observations.jsonl`.
3. `session_complete` events finalize a full Recon record: git evidence
   receipts (rev-parse, branch, status, diff --stat), canonical JSON +
   sha256 digest, and the audit markdown under `storage/recon/audits/`.

Repeat stop events for one session dedup through request-ID reservations
(`storage/recon/reservations/`).

## Operate

```bash
systemctl --user status evecor-recon-gateway
curl -s http://127.0.0.1:8773/healthz
.venv/bin/pytest tests/ -q
```

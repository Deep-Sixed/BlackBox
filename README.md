# BlackBox

Evidence-first execution recorder for capturing provenance, chronology, and reconstructable system history.

BlackBox is a refraction of Flight Recorder. The original implementation and outstanding Ledger work are preserved in Git before the standalone SQLite architecture is introduced.

## Current contract

BlackBox stores bounded, metadata-only evidence in a private local SQLite
database. Producers submit observations, claims and artifact references. BlackBox
validates their shape, records deterministic receipts, and appends lifecycle
events in one durable local store.

The first supported surface is the Python package and `blackbox` CLI. There is
no HTTP service, MCP gateway, EVECOR deployment cutover, legacy Ledger/Recon/Recall
compatibility layer, or production data migration in this initial bring-up.

## Local use

This repository pins Python exactly to `3.14.5`.

```bash
uv sync --frozen --python 3.14.5
uv run blackbox --database ./blackbox.sqlite3 init
uv run blackbox --database ./blackbox.sqlite3 capture --input capture.json --repo .
uv run blackbox --database ./blackbox.sqlite3 check
```

The database file is created with `0600` permissions, uses SQLite WAL mode, and
rejects unknown schema identities. `storage/`, `spool/`, local databases,
verification records, caches, logs, build output and environment files are
ignored.

## Migration boundary

`v0.0.0-original` is the original publishable Flight Recorder software baseline:
the Flight Recorder subtree extracted from EVECOR with operational
`storage/` evidence removed, before any BlackBox rebranding, SQLite redesign, or
behavioral changes.

The exact committed Flight Recorder extraction is preserved privately under
`/mnt/jarvis-data/migration-backups/blackbox/20260922T070643Z/`. Published
BlackBox history inherits software provenance only. It does not publish Flight
Recorder operational evidence or historical runtime artifacts.

See [docs/provenance.md](docs/provenance.md) for the measured tree hashes,
excluded paths, SHA-256 digests, and donor-to-published history mapping.

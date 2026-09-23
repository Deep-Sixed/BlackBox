# BlackBox

BlackBox is an independent execution-trace recorder for AI agents, models, tools and evaluations. It observes actions outside the acting agent, preserves provenance and chronology in a durable trace, and enables later audit and reconstruction without making policy decisions itself.

## Current contract

BlackBox stores bounded, metadata-only evidence in a private local SQLite
database. Producers submit observations, claims and artifact references. BlackBox
validates their shape, records deterministic receipts, and appends lifecycle
events in one durable local store.

The built-in Git observer independently collects local repository metadata.
Caller-submitted observations remain assertions. Claims can be linked to evidence
and corrected across sessions while preserving their sources and chronology.

BlackBox currently provides a local Python API and CLI backed by SQLite.
Network services, remote observer transports, and deployment integrations
are outside the current core runtime. Tool/process execution observation is
planned; the current runtime does not independently witness process execution.

## Local use

The package supports Python 3.14 (`>=3.14,<3.15`). CI and the release
compatibility tests run on exactly `3.14.5`.

```bash
uv sync --frozen --python 3.14.5
uv run blackbox --database ./blackbox.sqlite3 init
uv run blackbox --database ./blackbox.sqlite3 capture --input capture.json --repo .
uv run blackbox --database ./blackbox.sqlite3 check
uv run blackbox --database ./blackbox.sqlite3 head > anchor.json
uv run blackbox --database ./blackbox.sqlite3 check --anchor anchor.json
```

The receipt chain alone cannot detect a rewrite by someone who controls the
database file. Store `head` output somewhere that writer cannot change, then
`check --anchor` detects rewrites or truncation up to that point; see
[chain anchoring](docs/public-api.md#chain-anchoring).

The database file is created with `0600` permissions, uses SQLite WAL mode, and
rejects unknown schema identities. `storage/`, `spool/`, local databases,
verification records, caches, logs, build output and environment files are
ignored.

## Evidence and storage

See [docs/evidence-model.md](docs/evidence-model.md) for the claim,
observation, receipt, integrity, authority, verification and provenance
vocabulary used by the BlackBox contract.

See [docs/sqlite-contract.md](docs/sqlite-contract.md) for schema v3, atomic
upgrades from supported older schemas, and integrity-check semantics. Run `blackbox --database
./blackbox.sqlite3 init` with writer access to upgrade before using read-only queries.

## Python integration

BlackBox 0.5.0 exposes supported operations and typed results through `import
blackbox`. Use `initialize`, `capture`, `append_claim`, `get_session`,
`get_timeline`, `get_claims`, `link_evidence`, `get_evidence_links`,
`get_claim_relations`, `check_integrity`, and `get_chain_head`. See the
[public API contract](docs/public-api.md) for input mappings, result types,
bounded errors, retry behavior and migration ownership. Schema v3 adds [attributed evidence links and cross-session claim relations](docs/trace-relationships.md).

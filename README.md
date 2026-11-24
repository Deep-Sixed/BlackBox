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

BlackBox provides a local Python API and CLI backed by SQLite, and since 0.8.0
a local service, `blackboxd`. The service is the only process that opens the
database. Authorized components submit and read operational events through a
versioned HTTP API on a Unix socket. Every request needs a scoped token and a
matching caller uid, and there is no network listener. See
[docs/service.md](docs/service.md), [docs/openapi.yaml](docs/openapi.yaml) and
the [service design](docs/design/service.md). Remote observer transports
remain out of scope. Tool/process execution observation is planned; the
current runtime does not independently witness process execution.

## Host lifecycle hooks

`blackbox hook` lets the agent's host runtime, such as Claude Code, record
what the agent does without the agent reporting it. The host runs the command on
its own lifecycle events (prompt submitted, before and after each tool call,
turn end) and pipes the event as JSON on stdin. BlackBox keeps the event and
tool names and a digest of the payload, never the payload itself, under a
separate `host_reported` authority (schema v4). With `--git`
it also takes its own Git snapshot at the end of each turn. The command never
blocks the agent. See [docs/hooks.md](docs/hooks.md) for the settings snippet
and what the records do and do not prove.

## Local use

The package supports Python 3.14 (`>=3.14,<3.15`) on POSIX systems. CI and the
release compatibility tests run on exactly `3.14.5` on Linux. The database and
Git observer checks rely on POSIX ownership, permission and `O_NOFOLLOW`
semantics, so Windows is not supported: `import blackbox` fails there.

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

See [docs/threat-model.md](docs/threat-model.md) for what BlackBox defends
against, what it deliberately does not, and what a production deployment adds.
[docs/canonical-json.md](docs/canonical-json.md) specifies the exact bytes every
digest covers, for independent verifiers.

See [docs/sqlite-contract.md](docs/sqlite-contract.md) for schema v4, atomic
upgrades from supported older schemas, and integrity-check semantics. Run
`blackbox --database ./blackbox.sqlite3 init` with writer access to upgrade
before using read-only queries. [docs/trace-relationships.md](docs/trace-relationships.md)
covers the attributed evidence links and cross-session claim relations added in
schema v3.

## Python integration

BlackBox 0.8.0 exposes supported operations and typed results through `import
blackbox`. Use `initialize`, `capture`, `append_claim`, `get_session`,
`get_timeline`, `get_claims`, `link_evidence`, `get_evidence_links`,
`get_claim_relations`, `check_integrity`, and `get_chain_head`. See the
[public API contract](docs/public-api.md) for input mappings, result types,
bounded errors, retry behavior and migration ownership.

### Hardened Git observation

Linux deployments can set `BLACKBOX_GIT_SANDBOX=required` to require bubblewrap isolation for the built-in Git observer. Required mode is fail-closed and removes network access while mounting the host filesystem read-only. System/global Git configuration is ignored in all modes.

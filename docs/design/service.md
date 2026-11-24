# BlackBox service design (v0.8)

> **Status: approved 2026-11-24 with the decisions and requirements in sections 12–13.**
> Implementation proceeds in the order of section 11.
> Scope: the first production deployment of BlackBox on Friday, *alongside* Flight
> Recorder. Replacing Flight Recorder, changing ADR-005, a Ledger-MCP or `/v1/record`
> compatibility layer, and migrating historical data are explicitly out of scope.

## 1. Why this change

BlackBox today is a library and CLI. Every writer opens the SQLite file itself. That
works for one local user who owns the file. It does not work as a shared service on
Friday, where producers run under different accounts:

| Producer | Runs as |
| --- | --- |
| Claude Code host hooks | `jarvis` (uid 1000) |
| ContextForge plugins | uid 10001, in a rootful Podman container sharing the host user namespace (verified) |
| Future EVECOR components | Various |

BlackBox's database lives in `/var/lib/blackbox/db` and is private to `blackbox-svc`
(uid 993, 0700). Opening it to other accounts would undo the protections the
codebase already enforces (private directory checks, 0600 file, anchors). So BlackBox
needs one process that owns the database and that every other component talks to. That
process is `blackboxd`.

Everything else in this document follows from that one change. Design rules:

1. **Keep what works.** Schema v4, the receipt chain, the append-only triggers, the
   request-ID fingerprint semantics, the strict input models and the hardened Git
   observer stay as they are.
2. **One persistence path.** The REST API, the CLI's client mode and any future MCP
   surface all call the same service layer, which calls the existing public Python API.
3. **Authentication says who submitted, not whether it is true.** An authenticated
   producer's events remain *caller-asserted* evidence, exactly as today.

## 2. Boundary with Flight Recorder

| | BlackBox | Flight Recorder |
| --- | --- | --- |
| Purpose | Operational event recording: what happened, when, which component, outcome | EVECOR governance record: authoritative claims (Ledger), session audits (Recon), digests (Recall) |
| Account | `blackbox-svc` (993/970) | `flightrec-svc` (997/972); Ledger and Recon gateways still run as `jarvis` |
| Storage | `/var/lib/blackbox/db/blackbox.sqlite3` | `EVECOR/governance/flight-recorder/storage/` |
| Interface | `/run/blackbox/blackbox.sock` (HTTP, `/api/v1`) | MCP `:8586`, UDS + `:8774` (`/v1/record`), `:8773` |
| Consumers on Friday | None until approved | ContextForge (LedgerWriter, federation), Claude stop hook, Hindsight, Recall, backup |

The two services share no account, socket, port, path or database. Verified on Friday:
- `flightrec-svc` cannot read or write `/var/lib/blackbox`.
- `blackbox-svc` cannot write Flight Recorder storage.
- Read access by `blackbox-svc` to Flight Recorder storage is governed by Flight
  Recorder's own permissions, which are outside BlackBox's scope.

### Overlap (documented, not resolved here)

| Capability | BlackBox | Flight Recorder | Future consolidation would need |
| --- | --- | --- | --- |
| Tool-call receipts | Events via API | LedgerWriter → `receipts/claims.jsonl` | A ContextForge producer change; a decision on which store is authoritative |
| Session/Git capture | Hardened Git observer, host hooks | Recon `session_complete` (unhardened) | Recon retirement or hardening; Recall reading BlackBox |
| Claims and corrections | Schema v3 relations | Ledger MCP `ledger_*` | ADR-005 change; Ledger-MCP compatibility; BM25/as-of semantics (retired in BlackBox, G3/G4) |
| Daily digest | — | Recall | A consumer over the BlackBox API |
| Historical data | Starts empty | 763 claims, 10.6k receipts, Recon audits | A migration/archival decision (the disposition currently says *do not migrate*) |

## 3. Event model: mapping onto schema v4

An operational event carries: who submitted it, which component it is about, what
happened, when it happened, the outcome, its severity and a correlation id.

| API field | Stored as (v4) | Change? |
| --- | --- | --- |
| `event_id` (response) | `observations.id` (`obs_<sha256>`, deterministic) | none |
| `idempotency_key` | `sessions.request_id` + fingerprint | none (existing semantics) |
| submitting principal | `sessions.producer`, **set by the daemon** from the authenticated principal; the caller cannot choose it | none |
| `source` (component) | `sources.identity`, authority `caller_asserted` | none |
| `type` | `observations.data.name` (`Identifier`) | none |
| `kind` | `observations.kind` (`command`/`test`/`activity`) | none |
| `exit_code`, `duration_ms`, `content_digest` | `observations.data.*` | none |
| `recorded_at` | `observations.recorded_at`, BlackBox's clock | none |
| `outcome` | `observations.data.outcome` | **new optional field** |
| `severity` | `observations.data.severity` | **new optional field** |
| `occurred_at` | `observations.data.occurred_at`, the producer's clock, kept distinct from `recorded_at` | **new optional field** |
| `correlation_id` | `observations.data.correlation_id` | **new optional field** |

### Why the new fields are needed, and why they need almost no schema change

- `outcome`, `severity`, `occurred_at` and `correlation_id` are core requirements of
  your spec, and v4 cannot express them.
- `observations.data` is already canonical JSON (`CHECK(json_valid)`). The fields are
  added to the strict `Observation` model, not to a table.
- Rows without the new fields hash exactly as before. Old receipts, digests and
  fixtures are untouched. This must be proven by a test.

**Allowed values:**
- `outcome ∈ {success, failure, error, cancelled, unknown}`
- `severity ∈ {debug, info, notice, warning, error, critical}`
- `occurred_at` is RFC 3339 UTC with a `Z` suffix and microsecond precision. Values more
  than 5 minutes in the future (by BlackBox's clock) are rejected. Old values are
  accepted, because a producer may report late.
- `correlation_id` is an `Identifier`.

Every value passes the existing `SUSPICIOUS` secret scan.

### Schema v5: indexes only

Filtering by time, source, severity and correlation id at scale needs indexes, and the
schema validator compares exact objects, so any index is a schema version. v5 is
therefore additive:

- Expression indexes on `json_extract(data,'$.correlation_id')`,
  `json_extract(data,'$.occurred_at')` and `json_extract(data,'$.severity')`.
- Indexes on `observations(recorded_at)` and `sources(identity)`.
- No table rewrite, no CHECK change and no row change. The migration appends
  `schema_migrations`/`schema_metadata` rows like v2–v4, with upgrade tests from v1–v4
  stores.

If you prefer, v5 can be postponed: queries still work without the indexes, just
slower. Friday's expected volume (Flight Recorder took about 4 months to reach 10.6k
receipts) is small.

**Deferred:** a free-form `labels`/`attributes` map. The codebase deliberately has "no
arbitrary payload dictionaries". A bounded map (≤16 `Identifier` keys, short scanned
values) can be added later if a producer needs it.

### Batches

`POST /api/v1/events/batch` is one capture: one idempotency key, up to 1,000 events,
atomic. This is exactly today's `capture` with N observations. A single
`POST /api/v1/events` is a capture with one observation.

## 4. `blackboxd`

### Process model

- **Single writer.** One thread holds the only write connection and drains a bounded
  queue. A full queue returns `503` with `Retry-After`; it never drops silently.
- **Reads.** A small pool of `readonly=True` connections, which already refuse to
  migrate and re-check the directory.
- **Startup order:**
  1. Open the writer, which initializes or migrates the database.
  2. Run `check_integrity`.
  3. Only then bind the socket and report ready.

  A failed integrity check leaves the daemon *running but not ready*. Writes are
  refused and the reason is logged.

### Transport

- `/run/blackbox/blackbox.sock`, HTTP/1.1, owner `blackbox-svc`, group
  `blackbox-clients`, mode 0660. The directory is 0750.
- No TCP listener in this deployment. The daemon unit sets `IPAddressDeny=any` and
  allows only `AF_UNIX`.
- Containers reach BlackBox by bind-mounting `/run/blackbox`. This is the same pattern
  ContextForge already uses for the Ledger socket. It is not configured for Friday.

### HTTP stack: recommend `h11` plus asyncio

| Option | Peer credentials | Dependencies | Notes |
| --- | --- | --- | --- |
| **asyncio `start_unix_server` + `h11`** (recommended) | Direct (`SO_PEERCRED` on the connection) | +1 (`h11`, pure Python, used by uvicorn/httpx) | Small, auditable; we own routing and limits |
| stdlib `http.server` over `UnixStreamServer` | Direct | +0 | Python docs warn it is not for production use |
| Starlette/uvicorn | Not exposed to the app over UDS | +several | Heavier; peer credentials would need a custom server anyway |

### Authentication and authorization

- **Socket permission** is the first gate: members of `blackbox-clients` only.
- **`SO_PEERCRED`** gives the caller's uid, pid and gid on every connection.
- **Principals** are defined in `/etc/blackbox/principals.toml` (root-owned, 0640, group
  `blackbox-svc`). Each one has:
  - a name (becomes `sessions.producer`)
  - allowed peer uids
  - scopes: `events:write`, `events:read`, `host:report`, `metrics:read`
  - a bearer-token SHA-256 hash, if required. Raw tokens live only with the client.
- **Decision needed:**
  - **Option A (recommended):** require a token for every principal **and** match the
    peer uid. A token stolen by another local account doesn't work; a process under an
    allowed uid without the token doesn't work either.
  - **Option B:** peer uid alone.
- **Endpoints without authentication:** `health`, `ready` and `version` (no data).
  Everything else needs a scope.
- **`host:report`** is the only route to `host_reported` authority, which today is
  reachable only through `blackbox hook`. Grant it only to the hook principal.

### API v1

| Method and path | Scope | Notes |
| --- | --- | --- |
| `POST /api/v1/events` | `events:write` | One event. `201` with `event_id`; an identical repeat returns `200` with the same id; a conflicting repeat returns `409`. |
| `POST /api/v1/events/batch` | `events:write` | ≤1,000 events, atomic. |
| `GET /api/v1/events` | `events:read` | Filters: `since`/`until` (recorded, or occurred via `time=occurred`), `source`, `producer`, `type`, `kind`, `severity`, `outcome`, `correlation_id`, `authority`. Keyset cursor; `limit` ≤ 500 (default 100). Only committed sessions are returned. |
| `GET /api/v1/events/{event_id}` | `events:read` | Event plus session, source, authority, verification and receipt sequence. |
| `GET /api/v1/integrity` | `events:read` | `check_integrity`; with `?anchor=` the external-anchor check. |
| `GET /api/v1/chain-head` | `events:read` | For anchor export (section 6). |
| `GET /api/v1/health` | — | Process up. |
| `GET /api/v1/ready` | — | Writer open, integrity verified, queue not saturated. |
| `GET /api/v1/version` | — | Package version and schema version. |
| `GET /metrics` | `metrics:read` | Prometheus text. |

- No update or delete endpoints, by design.
- Claims and evidence-link endpoints are **not** part of v1. They exist in the library
  and can be exposed later without schema work.

### Limits

| Limit | Value |
| --- | --- |
| Request body | 1 MiB |
| Headers | 16 KiB |
| Batch size | 1,000 events |
| Page size | 500 |
| Per-principal token bucket | Writes 50/s, burst 200; reads 20/s |
| Request timeout | 10 s |
| Max concurrent connections | 64 |

All values are configurable. Every limit breach returns a specific `4xx` or `429`.

### Errors and logging

- Error bodies use BlackBox's bounded error codes (`ValidationError`, `ConflictError`,
  `BusyError`, ...) and never echo input. The existing `_boundary` decorator already
  guarantees this for the library.
- Logs are structured JSON to journald: principal, route, status, event count and
  latency. **No payloads, no tokens, no free text from callers.**

### No silent failures

1. **Daemon.** Every request ends in an explicit status. Rejections and failures are
   counted by reason (`blackbox_rejected_total{reason}`,
   `blackbox_ingest_failures_total`) and logged. Failed captures keep the existing
   durable `FAILED_RETRYABLE` lifecycle.
2. **Hook client.** It must never block the agent (existing rule), but it must not fail
   silently either. On an unreachable daemon or a rejection it exits 0 and writes one
   line to syslog (`blackbox-hook`, warning).
   - **No local spool:** EVECOR governance prohibits fallback audit files (disposition
     doc).
   - Gaps show up as syslog warnings plus missing events. A per-session
     `Stop`-without-prior-events check can be added later.

### Metrics

- `blackbox_requests_total{route,status}`
- `blackbox_request_seconds` (histogram)
- `blackbox_events_ingested_total{principal}`
- `blackbox_rejected_total{reason}`
- `blackbox_write_queue_depth`
- `blackbox_db_bytes`
- `blackbox_chain_head_sequence`
- `blackbox_last_backup_timestamp`
- `blackbox_integrity_ok`

## 5. Git observation in daemon mode

Today `blackbox hook --git` snapshots the repository the agent worked in, running Git
as the hook's user. In daemon mode, two problems arise:

1. **Independence.** If the client runs Git and sends results, the snapshot becomes
   caller-asserted, which defeats `local_git` authority.
2. **Recon's flaw.** If the daemon runs Git in any directory the caller names, that is
   the classic "run Git wherever the caller says" flaw, even with BlackBox's hardened observer.

**Proposal:**
- The daemon runs Git itself, as `blackbox-svc`, only for repository roots in an
  operator allowlist (`/etc/blackbox/repositories.toml`). Requests for other paths are
  rejected and counted.
- `BLACKBOX_GIT_SANDBOX=required` (bwrap). I verified bwrap runs as `blackbox-svc` on
  Friday; the full observer under the hardened unit still needs testing.
- `blackbox-svc` gets **read-only** ACLs on each allowlisted repository. Without them it
  cannot read the operator's private home directory.
- Git refuses repositories owned by another user ("dubious ownership"). The observer
  ignores system and global config, so it must pass `-c safe.directory=<root>` for
  allowlisted roots only. **To verify in implementation.**
- **Default for Friday: the allowlist is empty.** Git observation is off until a
  repository is approved.

## 6. Durability, backup, anchors, restore

- **SQLite settings (unchanged):** WAL, `synchronous=FULL`, `foreign_keys=ON`,
  `busy_timeout=5000`, `BEGIN IMMEDIATE`. The single writer makes `SQLITE_BUSY` rare;
  backups use the online backup API from a read connection. A checkpoint
  (`wal_checkpoint(TRUNCATE)`) runs after each backup and on clean shutdown.
- **Backup.** `blackbox backup --to DIR` (new CLI command over the backup API, followed
  by `PRAGMA integrity_check` and `check_integrity` on the copy). It runs from
  `blackbox-backup.timer` as `blackbox-svc`, nightly.
  - Target: `/var/backups/blackbox` (root disk, which has ample free space; the data disk is fuller,
    full). Directory 0750 `blackbox-svc:blackbox-svc`. Keep 14 daily copies.
  - **Decision needed:** should `evecor-backup` (restic, runs as `jarvis`) also pick up
    these copies? That needs `jarvis` read access to them (a group or ACL). `jarvis`
    has sudo, so this does not lower confidentiality in practice.
- **External anchors.** The writer must not be able to rewrite its own anchors.
  `blackbox-anchor.timer` runs as **`jarvis`**, reads `GET /api/v1/chain-head` with a
  read-only principal, and appends the head to
  `~/.local/state/blackbox-anchors/anchors.jsonl`. `check --anchor` and
  `GET /api/v1/integrity?anchor=` verify against it. `evecor-backup` currently targets
  only the Flight Recorder and Dreaming stores (verified). Adding the anchor log and the
  hook key to it is a small EVECOR change that needs approval.
- **Restore.** Stop the daemon, verify the backup copy (`integrity_check` plus
  `check --anchor` against the newest anchor at or below the copy's head), atomically
  replace the database file, start the daemon. Events recorded after the backup are
  lost; the anchor log shows how many.
- **Drill:** once in staging (Phase 4) and once after the Friday deployment.

## 7. Retention and archival

The append-only triggers block `DELETE` by design. A retention policy must not break
chain verification.

- **Friday policy: keep everything.** Projected growth is small. Disk usage is exported
  as a metric with an alert threshold.
- **Later (design only): sealed segments.**
  - At a size or age threshold, the daemon seals the current database: it exports a
    final anchor and marks the segment read-only.
  - It then starts a new database whose first receipt commits to the previous segment's
    head.
  - Retention deletes whole sealed segment files after N months (default: none).
    Deleting a file needs no row deletion.
  - Queries span segments by time.
  - This needs schema v6 (a genesis-link record) and its own review. Not needed for
    Friday.

## 8. Secrets

- The existing protections apply unchanged to every API path:
  - strict models (`extra="forbid"`)
  - the `SUSPICIOUS` scan (bearer tokens, private keys, URLs, `password=`/`token=`,
    `sk-`, `ghp_`, `AKIA`)
  - `Identifier` patterns
  - no free-form payloads; hook payloads stored only as HMAC digests
- New:
  - Tokens are compared only as SHA-256 hashes with constant-time comparison.
  - Tokens never appear in logs, metrics labels or error bodies.
  - Rejected values are never echoed.
- Tests feed each pattern through each endpoint.
- **Hook HMAC key.** In daemon mode the hook still digests payloads client-side with its
  own key (`~/.config/blackbox/hook-hmac.key`, `jarvis`). The daemon never sees raw
  payloads. Key loss makes old digests unverifiable, so the key should be backed up
  (see section 6).

## 9. Deployment shape (Phase 4/5 detail; nothing created yet)

- **Install.** A wheel from the tagged release, installed into
  `/opt/blackbox/<version>/` (venv on system `python3.14`, 3.14.4 verified), with a
  `/opt/blackbox/current` symlink. BlackBox runs as a **host systemd service, not a
  container**:
  - `SO_PEERCRED` and host Git observation need the host view.
  - Containers connect through the bind-mounted socket.
- **Accounts and groups:**
  - `blackbox-svc` (993/970) already exists.
  - New group `blackbox-clients`, members `jarvis` and later approved container uids by
    ACL.
- **`blackboxd.service`:**
  - `User=blackbox-svc`, `Group=blackbox-svc`, `SupplementaryGroups=blackbox-clients`
  - `StateDirectory=blackbox` (0700), `RuntimeDirectory=blackbox` (0750)
  - `ReadWritePaths=/var/backups/blackbox`
  - `ProtectSystem=strict`, `ProtectHome=read-only` (`tmpfs` if Git observation stays
    off)
  - `PrivateTmp`, `PrivateDevices`, `NoNewPrivileges`, `CapabilityBoundingSet=` (empty)
  - `RestrictAddressFamilies=AF_UNIX`, `IPAddressDeny=any`
  - `SystemCallFilter=@system-service`, `LockPersonality`, `ProtectKernel*`,
    `ProtectControlGroups`
  - `RestrictNamespaces` must allow user/mount/net/pid **only** if the bwrap Git
    sandbox is enabled.
  - `Restart=on-failure`, `RestartSec=2`, start-limit burst, `TimeoutStopSec=15`
    (drain the queue, checkpoint).
- **Timers:** `blackbox-backup.timer` (system, `blackbox-svc`) and
  `blackbox-anchor.timer` (`jarvis` user unit).
- **CLI client mode:** `blackbox --socket /run/blackbox/blackbox.sock ...` for
  `hook`, `head` and `check`. `--database` stays for local and development use.
- **Collision checks done:** `/run/blackbox`, `/var/backups/blackbox`, groups
  `blackbox-*` and units `blackbox*` do not exist on Friday. No TCP port is used.

## 10. Testing plan

| Area | Tests |
| --- | --- |
| Schema | v5 upgrade from v1–v4 stores; old rows' digests unchanged; new optional fields round-trip; secret scan on new fields |
| Service layer | Mapping event ↔ capture; idempotency (same input = same id, conflict = 409); batch atomicity |
| Auth | Missing, invalid or wrong-uid token; scope denial; host-report denial; constant-time comparison |
| Limits | Oversized body/headers/batch/page; rate limit 429; slow client timeout |
| Failure | DB locked/unavailable → 503 plus counter; integrity failure → not ready; daemon kill -9 mid-batch → no partial session, retry succeeds |
| Concurrency | N parallel writers through the socket; readers during writes |
| Git | Allowlisted vs rejected roots; sandbox required; `safe.directory` |
| Ops | Backup, verify, restore round-trip; anchor append/verify; restart persistence |
| Hook | Daemon down → exit 0 plus syslog line; payload never sent |

Existing suites stay green; CI adds the service tests and an installed-wheel daemon
smoke test.

## 11. Implementation order (after approval)

1. Tag **v0.7.0** from current `main`. PRs #32–#36 are unreleased and 0.6.9 was never
   tagged.
2. Event fields in models, plus v5 indexes (if approved) and migration tests.
3. Service layer (pure Python, no I/O): auth, mapping, queries, limits.
4. `blackboxd` transport (`h11`), metrics, logging.
5. CLI client mode, hook client mode, `backup` command.
6. systemd unit templates, principals/repository config examples, OpenAPI document,
   runbook.
7. Full test suite and CI; draft PR.

## 12. Decisions (approved 2026-11-24)

| # | Decision | Outcome |
| --- | --- | --- |
| D1 | HTTP stack | **Approved:** asyncio + `h11`. Pinned in `uv.lock`; included in dependency advisory scanning. |
| D2 | Authentication | **Approved:** a valid scoped token **and** a matching `SO_PEERCRED` uid are both required. Neither alone authorizes anything. |
| D3 | Event fields `outcome`, `severity`, `occurred_at`, `correlation_id` | **Approved:** optional. Rows without them keep identical hashes (tested). |
| D4 | Schema v5 (indexes only) | **Approved now**, with migration, integrity, compatibility and query-performance tests. |
| D5 | Free-form labels map | **Deferred.** |
| D6 | Git observation | **Approved:** sandbox required (fails closed when unavailable), operator allowlist, empty in production. |
| D7 | Backups in `evecor-backup` | **Approved** as a separate, narrowly scoped EVECOR change, using SQLite-consistent copies (never the live file). Verify permissions, capacity, retention and restore. |
| D8 | Retention | **Approved:** keep everything initially; monitor growth; disk-space warning thresholds. |
| D9 | MCP surface | **Deferred.** |
| D10 | Claims/evidence-link endpoints | **Deferred.** |

## 13. Approved requirements and how they are met

| # | Requirement | Design response | Status |
| --- | --- | --- | --- |
| R1 | A real ContextForge operation persists a new LedgerWriter receipt after the account separation | See note R1 below | Partly verified |
| R2 | Review and secret-scan EVECOR `8f189a0a`; no unrelated changes before publishing | The only commit ahead of origin; 7 intended files; no secret patterns in added lines. Pushed to `feat/evecor-rag-package`. | Done |
| R3 | Track Flight Recorder storage permissions with that service's own security work | Tracked outside BlackBox | Recorded |
| R4 | Anchors cannot be modified, replaced or deleted by `blackbox-svc` | See section 13.1 | Design |
| R5 | `blackboxd` never runs as root and has no sudo | The unit sets `User=blackbox-svc` and an empty `CapabilityBoundingSet=`, `NoNewPrivileges=yes`. `blackbox-svc` has no sudo rights (verified: `sudo -l -U blackbox-svc` reports none; no sudoers references). The daemon refuses to start if `geteuid()==0`. | Design |
| R6 | Malformed requests, oversized headers, incomplete requests, timeouts, concurrency | `h11` enforces HTTP/1.1 framing. The transport adds a header-size cap (16 KiB), body cap (1 MiB, also checked against `Content-Length` before reading), a per-connection read deadline (10 s total, 5 s idle), a connection cap (64; excess connections are refused immediately), one request per connection (no pipelining), no chunked request bodies, and bounded write-queue backpressure. Each case has a test. | Design |
| R7 | Credential revocation and rotation without redeployment | Principals and token hashes live in `/etc/blackbox/principals.toml` and are reloaded on `SIGHUP` (`systemctl reload blackboxd`). Each principal may hold two token hashes (current and next) for overlap during rotation. Removing a hash revokes it at the next reload. `blackbox token new` prints a token once and its hash for the file. Reload is atomic: a file that fails validation is rejected and the previous set stays active, with an error logged. | Design |
| R8 | Confirm root-filesystem capacity for backups | Confirmed on the host: the root filesystem has ample space; expected BlackBox size is megabytes; restic adds small deduplicated snapshots. `blackbox_db_bytes` and free-space metrics carry warning thresholds. | Confirmed |
| R9 | Preserve the receipt chain, append-only triggers, request-ID fingerprints and schema compatibility | v5 adds indexes only; v1–v4 upgrade tests; existing suites unchanged | Design |
| R10 | Keep ContextBridge available | No BlackBox component touches the ContextBridge agent, its config or its ports. Its health is checked before and after every Friday step. | Ongoing |

**R1 note.** The LedgerWriter plugin path (plugin → socket → gateway → journal) was
verified under the new accounts by running the production plugin code inside its
container. A tool call through the gateway's full request pipeline is a separate
production checkpoint, closed only by genuine authorized traffic.

### 13.1 Anchor trust boundary (R4)

**Writer.** `blackbox-anchor.timer` runs as `jarvis`. It reads `GET /api/v1/chain-head`
with a read-only principal and appends to
`~/.local/state/blackbox-anchors/anchors.jsonl` in the anchor owner's home (0600).

**What it protects against: a compromised `blackboxd` or `blackbox-svc`.** That account
cannot reach the anchor log:
- The anchor owner's home directory is private, with no ACL entry for `blackbox-svc` (verified: cannot list or
  traverse).
- `blackbox-svc` is not in any `jarvis` group and has no sudo.

A history rewrite or truncation of the database up to the newest anchor is therefore
detected by `check --anchor`.

**What it does not protect against:**
- **`jarvis` itself.** Claude Code and other agents run as `jarvis`, and `jarvis` has
  sudo, so an agent can edit both the anchor log and, through sudo, the database. Local
  anchors give **no** protection against the identity BlackBox most often observes.
- **root.** Same reason.
- **Events after the newest anchor.** Anchors run every 15 minutes, so the last window
  is unprotected.

**Hardening options** (not part of Friday; listed in order of strength):
1. Off-host anchors, e.g. append to a repository or host `jarvis` cannot rewrite.
2. `chattr +a` on the anchor log, set by root. It blocks truncation and rewrites by
   `jarvis` without sudo, but not by sudo.
3. Include the anchor log in restic. Old snapshots then hold earlier anchors, which
   raises the cost of an undetected rewrite.

**Plan for Friday:** option 3 (part of the D7 change). Options 1–2 go into the
follow-up list.

## 14. Release, implementation and deployment checkpoints

These are three separate checkpoints. Passing one does not imply the next.

1. **Baseline release: done.** `v0.7.0` is the reviewed pre-modernization
   baseline. It is `main` at `571cfec` (PRs #32–#36 plus a version bump only)
   and contains no `blackboxd` or API code.
2. **Implementation: done.** Developed on a feature branch (PR #37) as
   `0.8.0.dev0`, validated on the production host in Phase 4
   ([report](../validation/phase4-2026-11-24.md)), then released as `0.8.0`.
3. **Production deployment: Phase 5.** It requires the release from
   checkpoint 2, the reviewed deployment checklist, and your go-ahead.

**Passing CI does not make BlackBox production-ready.** Readiness also requires
these results from isolated integration on Friday (Phase 4):

| Criterion | Evidence required |
| --- | --- |
| Isolated integration | A staging `blackboxd` running as `blackbox-svc` on a scratch database and test unit, with Flight Recorder and ContextBridge healthy before and after |
| Authentication and authorization | Over the real socket on Friday: token + uid accepted; wrong token, wrong uid, missing scope and unreadable producer each refused; rotation and revocation by reload; a rootful Podman container granted access only as configured |
| Backup and restore | `blackbox-backup` run as `blackbox-svc`; the copy verified; a full restore drill that passes `check --anchor`; `evecor-backup` (D7) picks up the copy, and a restic restore of it is byte-identical |
| Service-account isolation | `flightrec-svc` and `jarvis` cannot open the database; `blackbox-svc` cannot write Flight Recorder storage; the daemon refuses to run as root; `systemd-analyze security` reviewed for the unit |
| Unit hardening works | The hardened unit starts, serves, reloads and stops cleanly. Each directive that had to be relaxed is recorded with its reason. |

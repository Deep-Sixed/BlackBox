# Flight Recorder capability disposition — PR #4 working inventory

**Status: parity closure blocked on the Ledger semantic decisions below.**
This inventory is not a retirement authorization, a claim of production parity,
or a statement that the migration is complete. No donor or EVECOR runtime changes
are made here. The supported BlackBox baseline is `v0.3.0`, schema v2.

## Scope and evidence

The donor is `EVECOR/governance/flight-recorder` at
`d62c5cd7fe4f5fc3f561f382a2908bcd2c7e6b65`, plus the separately preserved four-file
Ledger working patch. The inspected donor HEAD is still that commit. The current
four-file diff exactly matches the saved patch, SHA-256
`f9bf68e44481088ddbb2b994a430379e8ba37df25c46cabb0514f736af04a91a`.
[Software inventory](flight-recorder-source-inventory.json) records all 58 tracked
non-storage files and their inspected working-tree hashes. Hashes for the four
modified files describe the preserved working state, not the committed baseline.
The private extraction and bundle remain as recorded in [provenance](provenance.md).

The review covers module responsibilities, callables, input models, persistence,
read APIs, tests, declared entry points and deployment definitions. It does not
read or publish operational claim/spool contents, execute donor writes, or assert
which deployment units are currently active. Whole-EVECOR caller discovery and
runtime verification remain WP #5; a file inventory cannot substitute for them.

## Decisions blocking closure

These are identified differences, not permission to silently discard behavior.
They affect the newly frozen public contract and potentially stored schema.

| Gap | Donor evidence | BlackBox v0.3.0 limitation | Decision needed |
| --- | --- | --- | --- |
| G1: claim-specific evidence linkage | `ledger.models.SourceRef`, `mcp_server.SourceInput`, `_build_claim`, `validate_claims` | A claim has a source identity but no list of evidence refs/locators/digests. Session artifacts do not establish which artifact supports which claim. Quotes are not metadata-only. | Choose a native bounded evidence-link contract, or explicitly retire/externally own the richer semantics. Do not present session artifacts as equivalent. |
| G2: correction across sessions | `ledger.repository.supersede`, `contest_linked` operate on global claim IDs without session scope | `ingest.claim_row` rejects a target belonging to another session. Recording a later correction inside the old session loses the real originating-session association. | Approve cross-session relationships or explicitly accept the restriction; no adapter workaround pretending the old session originated the correction. |
| G3: effective-time validity | `ClaimRecord.valid_on`, `search_claims(as_of)`, `build_timeline(as_of)` | `through` is a recording-event cutoff, not the donor's effective-date `[valid_from, valid_until)` window. | Decide whether effective validity is required native evidence metadata or belongs to a separate knowledge consumer. |
| G4: retrieval semantics | `ledger.search` implements BM25 over statements, sources and notes; MCP exposes query/topic/as_of/limit | Public `get_claims` supports topic and recording cutoff, not text ranking, result limits or effective-date search. | Assign search to the consumer/adapter or specify the native retrieval requirement; topic filtering alone is not parity. |
| G5: claim taxonomy/context | `ClaimRecord` and MCP input expose type, confidence, notes, general relations and retracted state | Native claims contain topic/statement/source plus supersedes/contests; no confidence/type/effective metadata/general relation graph. | Explicitly classify retained metadata versus knowledge/governance semantics. Never derive observer authority from confidence. |

Recommended direction for review: retain bounded provenance links and real
cross-session correction provenance natively; keep transport identity, environment
path resolution, presentation and relevance ranking outside core. Effective-time
validity and richer claim metadata require an explicit contract choice. Until
these decisions are made, G1–G5 are **REQUIRED / MISSING — disposition pending**,
not obsolete. No schema or release bump is made merely to publish this inventory.

## Capability matrix

Categories distinguish responsibility from mechanism. An adapter assignment is
an outstanding WP #5 deliverable, not a claim that it already exists.

| Responsibility / donor location | Disposition | Native equivalent, intentional difference, or remaining owner |
| --- | --- | --- |
| Session/request correlation and duplicate reservation — `recon.finalize.derive_request_id`, `session_record.reserve_request_id` | NATIVE BLACKBOX | `capture` binds request IDs to deterministic sessions and input fingerprints. Same input deduplicates; conflicting input fails. Exact old UUID/req format and caller-chosen session IDs are not compatibility requirements. Adapter maps external correlation. |
| Canonical record reconstruction — `session_record.build_record`, `validate_record`, `write_session_artifacts` | NATIVE BLACKBOX | Typed capture, canonical SQLite rows and `get_session`; no parallel Markdown/JSON/sha sidecar store. |
| Git branch, commit, changed paths — `collect_git_evidence`, `finalize.collect_git_receipts` | NATIVE BLACKBOX | Built-in Git observer records committed, staged, unstaged and untracked deltas separately, including dirty/untracked state when a baseline is supplied. Raw command stdout/stderr is intentionally not retained. |
| Commands/tests/activity metadata — Recon claim and verified arrays | NATIVE BLACKBOX | Bounded observations carry name, kind, exit code, duration and optional digest. Caller data stays unverified. Self-hashed caller receipts do not become observer authority. |
| Claim/observation separation and receipt verification — `make_evidence_receipt`, `verify_evidence_receipt` | NATIVE BLACKBOX | Caller/local-Git authority and verification classifications; v2 checks all canonical record receipts and chain continuity. Hash integrity is not observer identity. |
| Artifact references — Recon `artifacts_written` | NATIVE BLACKBOX | Session artifact path/digest references with unverified classification. This does not close G1. |
| Failed capture/retry/restart — Recon multi-file reservation cleanup | NATIVE BLACKBOX | Durable RESERVED, atomic capture, FAILED_RETRYABLE, idempotent retry; no multi-file repair procedure. |
| Blockers/deferred work/risks/session narrative — Recon arrays | NATIVE BLACKBOX | Bounded topic/statement claims can record these caller assertions; adapter defines event-to-topic mapping. Arbitrary nested payloads are not accepted. |
| Secret handling — Recon `redact_value` | NATIVE BLACKBOX | Strict metadata allowlist and sensitive-shaped input rejection before persistence replace arbitrary-payload redaction. No generic raw result storage. |
| Append claims and local correction/contest chronology — Ledger repository/timeline | NATIVE BLACKBOX | New linked records, immutable originals, derived status and recording-sequence snapshots. Cross-session and effective-time distinctions remain G2/G3. |
| Duplicate ID rejection — Ledger `_claim_id_exists` | NATIVE BLACKBOX | Database primary/unique keys and deterministic identity replace full directory scans. Caller-assigned legacy IDs are not accepted as canonical IDs. |
| Consistency checking — Ledger `validate_claims`, CLI `check` | NATIVE BLACKBOX | SQLite/foreign-key/schema/receipt checks and single-transaction writes; no mutable reciprocal status fields. Donor source-path existence audit is separate, below. |
| Claim evidence/type/time/text retrieval — Ledger models/search/MCP | REQUIRED / MISSING | G1–G5; no blanket declaration of parity. |
| CPEX request identity and passive observation transport — `ledger.gateway`, `recon.gateway` | EVECOR ADAPTER RESPONSIBILITY | Sanitize and translate envelopes through the public API. Decide lossless supported identity/correlation mapping before shadow acceptance. Gateway best-effort success after failed persistence must not become a durability claim. |
| MCP tools and request-context attribution — `ledger.mcp_server`, `recall.mcp_server` | EVECOR ADAPTER RESPONSIBILITY | Federation, authentication, tool schemas and remote identity stay outside core. Existing governance gates are not bypassed by direct SQLite access. |
| Stop hook finalization and repository selection — `recon.finalize` | EVECOR ADAPTER RESPONSIBILITY | Hook transport, surface/model/session mapping, non-Git skip behavior and startup ownership. Built-in collection remains native. |
| Evidence roots, containment and relocation aliases — `ledger.validate` | EVECOR ADAPTER RESPONSIBILITY | Resolve explicitly approved environment roots externally. Donor checks existence and hash syntax, not full file-content hash or quote authenticity. No EVECOR path table enters BlackBox. |
| Daily handoff digest, timezone, summaries and activity counts — `recall.compose`, `sources` | EVECOR ADAPTER RESPONSIBILITY | Read-only consumer presentation over public queries plus external sources. Local-time day grouping and stable output persistence remain consumer concerns. |
| Wiki-turn aggregation — `recall.sources.activity_on` | EVECOR ADAPTER RESPONSIBILITY | External memory/wiki ownership. Recall code inspected here performs deterministic aggregation, not semantic memory selection; no Historian functionality is inferred or copied. |
| Timer, ACL reader grants, ports, service identities — `deploy/*`, `finalize._grant_reader_access` | EVECOR ADAPTER RESPONSIBILITY | WP #5 deployment contract, private database ownership, backups, migrations, failure behavior and rollback. No services changed here. |
| `FLIGHT_RECORDER_STORE`, `LEDGER_*`, `RECALL_*`, `RECON_*` path/config discovery | EVECOR ADAPTER RESPONSIBILITY | Explicit database path passed into public API; deployment interprets environment. |
| Markdown/YAML canonical claim serialization and parsers — `ledger.parse`, `yaml_subset`, render helpers | OBSOLETE | No new BlackBox filesystem claim store or YAML round-trip mechanism. Historical readers may remain external until retention disposition. |
| Mutable target status/confidence, reciprocal links, lock/intent files — Ledger repository | OBSOLETE | Replace mechanisms with append-only relation rows, derived status, SQLite locks/WAL and transactions. This does not retire the correction responsibility. |
| Human CLI stats/Markdown audit formatting — Ledger CLI/Recon rendering | EVECOR ADAPTER RESPONSIBILITY | Derived consumer reporting using public views. Do not create another canonical evidence representation. |
| Snitch naming/env fallback, pre-rename headings/filenames — Recon config and Recall parser | RECON/SNITCH LEGACY — RETIRE | No compatibility aliases or legacy schema added. Historical material stays preserved. |
| Date-first/flat layout readers and old standalone/retired system units | HISTORICAL ONLY | Evidence of prior deployment; not runnable BlackBox defaults. Removal belongs to reviewed retirement after cutover. |
| PostgreSQL recovery compose — `deploy/recovery/...` | HISTORICAL ONLY | Recovery reference for an old deployment; never a BlackBox backend or dependency. |
| Existing storage/receipts/archive/spool payloads | RUNTIME DATA — DO NOT MIGRATE | Preserve locally. No evidence copied into Git, no live-store conversion or deletion here. |
| README files, package entry points, lockfile, ignore rules | HISTORICAL ONLY | Explain the old grouping/build; BlackBox owns its current packaging and API. |
| Ledger Pluto fixtures, wiki fixtures, Snitch fixture, donor tests | HISTORICAL ONLY | Requirements archaeology. Retained behaviors are tested natively; no legacy parser fixtures or operational data imported. |

## Preserved four-file Ledger patch: final mechanism disposition

The full patch remains privately preserved, but its changes are no longer an
unclassified migration artifact. Closure of these remediation goals does not
resolve the separate donor model gaps G1–G5.

| File / change | Disposition and evidence |
| --- | --- |
| `ledger/cli.py`: fallback to `FLIGHT_RECORDER_STORE` when `--repo` is absent | EVECOR ADAPTER RESPONSIBILITY. Deployment resolves a store and passes an explicit database path. No implicit EVECOR environment lookup in core. |
| `ledger/repository.py`: `_claim_id_exists` and duplicate guards for add/supersede/contest | NATIVE BLACKBOX. Unique canonical IDs, request fingerprints and idempotent claim insertion. Existing conflicting-request/duplicate tests plus `test_correction_chain_keeps_prior_evidence_and_recording_cutoffs`. |
| `repository.py`: per-target flock, re-read state after lock, release on exit | NATIVE BLACKBOX goal; OBSOLETE mechanism. BEGIN IMMEDIATE plus one-successor constraint; `test_concurrent_supersession_has_one_durable_winner`. |
| `repository.py`: transaction directory/constants, intent path/write/read/update/cleanup, serialization/reconstruction helpers, recovery routine | NATIVE BLACKBOX goal; OBSOLETE mechanism. Atomic rows/events/receipts with rollback/WAL replace partial file writes and recovery journals. `test_failed_correction_rolls_back_record_event_and_receipts` and existing process-crash/retry tests. Do not copy the donor's state-machine implementation. |
| `repository.py`: new-first supersede/contest writes, target status/confidence/note rewrites, existing-contest reuse | NATIVE BLACKBOX append-only relations and deterministic duplicate handling. Target rows never change; statuses derive from relationships. No confidence promotion or mutable reciprocal metadata. |
| `ledger/validate.py`: added contradicts/disputes status/confidence checks; reciprocal supersedes/superseded_by status checks | NATIVE BLACKBOX goal; OBSOLETE denormalized-field checks. Foreign-key links, immutable target, derived status and receipts eliminate two-writer disagreement. A successor may itself later be superseded; do not copy the donor validator's blanket requirement that every referenced successor remain active forever. |
| `tests/test_ledger.py`: eight added consistency/duplicate tests | NATIVE BLACKBOX semantic counterparts in current claim, integrity, concurrency and new disposition-parity tests. Assertions about Markdown fields/status mutation are obsolete, not ported. |

The donor recovery function only handles `prepared` (and cleanup of `committed`)
while writers also emit `new_written` / `contest_written`; its own historical
remediation status is not proof of crash recovery. BlackBox tests independently
establish native guarantees rather than inheriting a claim of donor correctness.

## Three preserved untracked files

- `ledger/AUDIT_REMEDIATION.md`: HISTORICAL ONLY. Source of LF-01..04 intent, not
  authoritative evidence that every historical gate passed. Native tests replace
  that assertion; no document imported into runtime/source.
- `spool/ledger_spool.py`: EVECOR ADAPTER RESPONSIBILITY for any approved outage
  buffering requirement; old script is not imported. Current operator governance
  prohibits fallback audit files, so no new spool is created here. WP #5 must
  resolve delivery policy through its approved governance path.
- `spool/pending.jsonl`: RUNTIME DATA — DO NOT MIGRATE. Preserved privately;
  neither opened for content nor flushed/deleted. Its owner must reconcile pending
  operational evidence before retirement; source-code disposition is not that act.

Their original hashes remain in the private `untracked-manifest.json`; the exact
migration backup is retained. The 58-file inventory does not include these
untracked files, caches, virtual environments, or excluded storage documents.

## Verification and release position

`tests/test_disposition_parity.py` proves retained semantics through public APIs:
recording-time history and unchanged original claims, atomic correction rollback
(including new sources/events/receipts), deterministic retry, and concurrent
single-successor behavior. Existing tests cover request identity, Git state,
artifacts, authority, failure/restart and receipt corruption. These are semantic
contract tests, not a claim of byte-level or runtime shadow parity with the donor.

No supported runtime behavior changes in this draft; package stays 0.3.0. Decide
release numbering only when the approved gap resolution is implemented. PR #4
must remain draft until G1–G5 have explicit retained/retired/externally-owned
answers and retained missing capabilities have passing tests. Then WP #5 still
needs whole-EVECOR discovery, shadow evidence, cutover and rollback proof. Flight
Recorder is not retired by this document and migration is not complete.

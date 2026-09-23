# Attributed trace relationships — package 0.4.0 / schema v3

`actor assertion != observer observation != BlackBox persistence`.
BlackBox records attribution and chronology; it makes no policy or semantic
support decision. Caller-supplied source names are attribution, not authenticated
identity. Naming a source `blackbox.git` cannot confer observer authority.

## Evidence links

`link_evidence(database, session, link)` records an identified source's assertion
that an existing observation, evidence receipt, or artifact `supports`,
`contradicts`, or provides `context` for a claim. Input is bounded metadata:
`source`, `claim_id`, `record_type` (`observation`, `evidence`, `artifact`),
`evidence_record_id`, `relation`. No quotes, payloads, arbitrary notes or confidence.
The committed session supplied to the operation is the assertion's true origin;
the claim and evidence keep their own sessions. Source authority remains
`caller_asserted`, regardless of the evidence's verification classification.
Identical input in the same session is idempotent.

`get_evidence_links` returns typed detached views, optionally filtered by
`claim_id`, origin `session`, or inclusive event `through`. Each view exposes the
link ID, claim ID, evidence reference/type, relation, source ID, origin session ID,
recorded time and sequence. Referenced records remain available through session
reconstruction. A missing reference raises `NotFoundError` without partial writes.

## Claim relations

`append_claim(..., target=older_id, relation=...)` permits `supersedes`, `contests`
and `retracts` across sessions. The new claim owns the relation and its source;
neither claim's origin changes. Targets must already exist. One direct successor
may supersede a target; competing successors raise `ConflictError`. Multiple
contests or retractions are retained as separate assertions. Identical appends
are idempotent. Relations never mutate, delete or conceal their target.

`get_claim_relations` exposes new/target claim IDs, both origin sessions, asserting
source ID, relation, recorded time and sequence. Optional filters are `claim_id`
(the newer claim), `target_id`, origin `session`, and inclusive event `through`.
`get_claims` derives status from incoming relations within the cutoff, with
precedence `retracted`, `superseded`, `contested`, `active`. These statuses describe
recorded assertions, not BlackBox acceptance or adjudication. Retracting a
correction does not erase its outgoing relation or automatically reactivate its
target. `get_session` preserves the existing claim projection shape.

## Time and migration

`recorded_at` is BlackBox's authoritative local recording timestamp, taken inside
the write transaction; it is not a commit-time guarantee and clocks can move.
`sequence` is BlackBox local event persistence order, not distributed causality.
No `occurred_at` or `observed_at` is fabricated. Future observer clocks require an
identified source and explicit semantics; causality requires explicit relations.

Schema v3 adds only `claim_relations` and `evidence_links`, their indexes and
immutability triggers. Claims' historical storage columns and all v1/v2 canonical
rows, event sequences and existing record receipts remain unchanged. Earlier
claim target/relation columns remain populated for supersedes/contests; retractions
use only the new relation table. Public projections resolve the relation table.
Migration backfills historical relations against their existing CLAIM events;
recorded times therefore remain the original recording times. Backfill receipts
extend the receipt chain without creating fictional historical events.
New relation/link receipt material uses `blackbox.record.v3`; existing record
types retain `blackbox.record.v2`, including after migration. The chain is still
unauthenticated: integrity is not independent observation or proof of authorship.

Writer operations may initialize/migrate. Readers never initialize or migrate;
old stores require writer initialization. Source schema, evidence and existing
receipt integrity must pass before migration. DDL, backfill, metadata and version
changes commit atomically or roll back together. Original schema definitions stay
frozen.

## Scope and observers

Claim evidence links and cross-session corrections use the bounded forms above.
Effective validity windows, generic notes and mutable statuses are outside core.
BM25, semantic ranking and interpretation belong to downstream consumers.

BlackBox already independently observes local Git metadata. Tool/process execution
is the next planned observer boundary: the first execution observer. Additional
filesystem, CI, model-traffic and evaluation-specific observers are future work.
The current core has no execution-observer runtime or gateway and makes no policy
decisions.

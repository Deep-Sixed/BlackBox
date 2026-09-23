# BlackBox architecture

BlackBox is local evidence infrastructure, not a governor. Producers submit
bounded metadata and claims. A local Git observer independently collects repository
metadata. Ingest validates inputs and commits provenance, observations, claims,
artifact references and lifecycle events together. Read-only reconstruction APIs
serve downstream consumers; no downstream inference upgrades source authority.

The durable boundary is one local SQLite database. Sessions have stable identity
from a caller's request ID, with a fingerprint binding retries to identical input.
A durable RESERVED event precedes capture. A single transaction commits capture
and COMMITTED; rollback leaves a reusable reservation. Capture failures append
FAILED_RETRYABLE without persisting exception text; this lifecycle state means the
reservation can be retried after remediation, while each durable failure row separately
records whether an unchanged automatic retry is appropriate. Invalid input is rejected before
reservation. Conflicting reuse of an ID fails instead of overwriting evidence.

Sources distinguish caller-asserted identity from the built-in local Git observer.
Receipt integrity is separate from authority. Caller observations are always
unverified, regardless of their supplied digest. Local Git metadata is marked
locally observed, not authenticated remote identity. The observed repository's
own config is untrusted: the observer disables its `core.fsmonitor` hook, which
could run programs or hide changed paths, strips inherited `GIT_*` variables and
takes no optional index locks. Unstaged changes are found by hashing raw
working-tree bytes against the index, so Git never runs the repository's clean
filters on the observer's behalf and `assume-unchanged` flags cannot hide edits;
see the [threat model](threat-model.md). Local process/OS ownership is
the trust boundary; arbitrary code with database-file access is outside this model.

Corrections, contests and retractions insert linked claims across sessions. The original claim is immutable.
Only one superseding successor is permitted. A chronological event sequence and
UTC recording times support reconstruction. Derived summaries are read-only views,
never source records.

The Python library and CLI are the supported interfaces. Network transports,
production deployment integration, retention/deletion, external authentication,
and remote observer infrastructure are separate system concerns.

Schema v2 adds atomic forward migration from released v1 databases and chained
receipts for all canonical record types. Read-only handles never migrate. See the
[SQLite contract](sqlite-contract.md) for validation, ordering and threat limits.

The [public API](public-api.md) is the consumer boundary. It accepts validated
input mappings and returns detached, frozen typed views. Storage handles,
transaction helpers and migration functions stay internal. The CLI uses this
same boundary; expected implementation failures become bounded BlackBox errors.


Schema v3 adds two bounded relationship tables: attributed evidence-to-claim
assertions and immutable newer-to-older claim relations. Actor assertion,
observer observation and BlackBox persistence remain distinct. No new observer
runtime or Actor/Observer/Evaluation/Trace entities are introduced. See
[trace relationships](trace-relationships.md) for attribution, local clock/order,
status precedence and the tool/process boundary for the first future observer.

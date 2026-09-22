# BlackBox architecture

BlackBox is local evidence infrastructure, not a governor. Producers submit
bounded metadata and claims. A local Git observer independently collects repository
metadata. Ingest validates inputs and commits provenance, observations, claims,
artifact references and lifecycle events together. Read-only reconstruction APIs
serve downstream consumers; no downstream inference upgrades source authority.

The durable boundary is one local SQLite database. Sessions have stable identity
from a caller's request ID, with a fingerprint binding retries to identical input.
A durable RESERVED event precedes capture. A single transaction commits capture
and COMMITTED; rollback leaves a retryable reservation. Recoverable errors append
FAILED_RETRYABLE without persisting exception text. Invalid input is rejected before
reservation. Conflicting reuse of an ID fails instead of overwriting evidence.

Sources distinguish caller-asserted identity from the built-in local Git observer.
Receipt integrity is separate from authority. Caller observations are always
unverified, regardless of their supplied digest. Local Git metadata is marked
locally observed, not authenticated remote identity. Local process/OS ownership is
the trust boundary; arbitrary code with database-file access is outside this model.

Corrections and contests insert linked claims. The original claim is immutable.
Only one superseding successor is permitted. A chronological event sequence and
UTC recording times support reconstruction. Claimed occurrence times remain distinct
from recording times. Derived summaries are read-only views, never source records.

The Python library and CLI are the initial API. HTTP, MCP deployment, production
data conversion, retention/deletion, external authentication, and EVECOR cutover
are separate work. No legacy client compatibility is promised.

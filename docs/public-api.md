# Supported public API — BlackBox 0.3.0

Use `import blackbox` (or named imports from `blackbox`). Its explicit `__all__`
is the supported namespace, including result models, errors and `__version__`.
The seven operations are also explicitly exported by `blackbox.api`.
Other submodules are implementation details even when Python makes them
accessible as package attributes. Do not depend on raw connections, migration
functions, row dictionaries or private helpers. The wheel includes `py.typed`.

Package 0.3.0 establishes this contract; the database remains schema v2.
Historical tags and canonical persisted material are unchanged. Existing
internal imports have not been removed, but receive no compatibility promise.
Future public breaking changes require an explicit versioned contract change.

## Callable disposition

| Existing callable or group | Classification | Rationale and consequence |
| --- | --- | --- |
| `ingest.ingest` | PUBLIC responsibility; INTERNAL implementation | `capture` accepts an input mapping and returns `CaptureResult`, not an implementation dictionary. |
| `ingest.append_claim` | PUBLIC responsibility; INTERNAL implementation | Public `append_claim` returns `ClaimResult` rather than a bare ID. |
| `query.reconstruct` | PUBLIC responsibility; INTERNAL implementation | `get_session` returns a typed, detached `SessionView`. |
| `query.timeline`, `query.claims` | PUBLIC responsibility; INTERNAL implementation | `get_timeline` / `get_claims` return tuples of typed events/views. |
| `query.integrity` | PUBLIC diagnostic responsibility; INTERNAL implementation | `check_integrity` returns typed findings; inability to open/validate raises bounded exceptions rather than hiding failure categories. |
| `db.connect` | INTERNAL | Public administrative `initialize` returns version/status and closes its handle. Consumers never own a BlackBox connection. |
| `db.now`, `db.transaction`, `db.verify_schema`, `db.schema_digest` | INTERNAL | Clock, transaction, schema validation and digest mechanics are not consumer contracts. |
| `ingest.event`, `state`, `source`, `observation`, `claim_row` | INTERNAL | These helpers require transaction context and must not bypass input/receipt rules. |
| `integrity.digest`, `append_receipt`, `evidence_errors`, `record_errors` | INTERNAL | Canonical material and chain mechanics remain storage concerns. |
| `migrations.schema_digest`, `schema_objects`, `validate`, `install`, `v002.upgrade` | INTERNAL | Only writer initialization owns schema evolution; no direct migration API. |
| `models.canonical`, `identity`, `safe_strings`, model validators | INTERNAL | IDs, serialization and input validation are accessed through public operations. |
| `provenance.git`, `paths`, `collect_git` | INTERNAL | Opt into local Git observation using `capture(repo=..., baseline=...)`; do not invoke collection internals. |
| `cli.main` | ADMINISTRATIVE CLI entry point | The supported executable is `blackbox`; importing `main` is not an integration contract. |

## Operations

All database/repository paths accept `str` or `pathlib.Path`. Operations close
their handles before returning; no long-lived service, connection pool or global
store is introduced.

| Operation | Return | Storage access |
| --- | --- | --- |
| `initialize(database)` | `InitializationResult(status="initialized", schema_version=2)` | Writer; creates or migrates. Idempotent. |
| `capture(database, request, *, repo=None, baseline=None)` | `CaptureResult(session_id, status, duplicate)` | Writer; creates/migrates as needed, reserves and captures using existing transactions. |
| `append_claim(database, session, claim, *, target=None, relation=None)` | `ClaimResult(claim_id)` | Writer; may initialize/migrate, then requires an existing committed session. |
| `get_session(database, session)` | `SessionView` | Read-only consistent snapshot; unknown session raises `NotFoundError`. |
| `get_timeline(database, *, through=None)` | `tuple[TimelineEvent, ...]` | Read-only global event order. |
| `get_claims(database, *, through=None, topic=None)` | `tuple[ClaimView, ...]` | Read-only, derived claim status at the cutoff. |
| `check_integrity(database)` | `IntegrityResult(ok, schema_version, errors)` | Read-only; false `ok` reports observed inconsistencies. |

`through` is an inclusive, nonnegative SQLite sequence number (maximum
`2**63 - 1`); booleans and coercible strings are rejected. Omitting it means the
current snapshot. Timeline and claim-query results are ordered by event sequence.
Session component collections are ordered by canonical ID except events, which
are chronological. `topic` uses the input identifier grammar.

Reader operations never create or migrate a database. A recognized v1 database
raises `MigrationRequiredError`; call `initialize` with writer access, then retry
the read. Capture and claim writes retain automatic writer initialization for
CLI compatibility. Deployments can call `initialize` at startup to own this
transition explicitly. Missing/unopenable database files raise `DatabaseError`;
`NotFoundError` refers to an absent session in a valid database.

## Inputs, stored records and views

Inputs are mappings, not result objects. Capture accepts `request_id`, `producer`,
and optional lists `observations`, `claims`, `artifacts`. See the unchanged
[capture schema](../schemas/capture.schema.json) for limits and required fields.
Each claim mapping contains `source`, `topic`, `statement`. Public operations
validate before writing and reject unknown fields and sensitive-shaped content.
The existing internal Pydantic input models (`Capture`, `Claim`, `Observation`,
`Artifact`) implement validation; they are deliberately not exported as public
constructors. Pass ordinary mappings so all input errors cross the bounded
operation boundary. A baseline must be a full Git commit hash and requires `repo`.

The public Pydantic result models are frozen and their collections are tuples:

- Operation results: `InitializationResult`, `CaptureResult`, `ClaimResult`.
- Canonical record projections: `SessionRecord`, `SourceRecord`,
  `ObservationRecord`, `EvidenceRecord`, `ClaimRecord`, `ArtifactRecord`,
  `FailureRecord`, `TimelineEvent`. These are detached copies, not writable rows.
- Typed observation data: `CallerObservation` and `GitObservation`. Their fields
  retain the existing caller/local observer distinction.
- Derived views: `SessionView` combines canonical projections and lifecycle status;
  `ClaimView` adds `active`, `superseded` or `contested` status to a claim.
- Diagnostic result: `IntegrityResult` reports local consistency, not authority.

`SessionView` exposes `session`, `status`, `sources`, `observations`, `evidence`,
`claims`, `artifacts`, `failures`, and `events`. Its claims are original records;
use `get_claims` for derived status. Timestamps remain UTC strings. IDs are opaque:
retain returned IDs rather than reproducing BlackBox's hashing implementation.

Use attributes for typed access and `.model_dump(mode="json")` for serializable
copies (tuples become JSON arrays). Editing a dumped copy cannot modify stored
history. Result constructors and Pydantic serialization/validation utilities are
ordinary Pydantic facilities, not database operations; consumers normally receive
models from operations rather than instantiate them.

## Bounded errors and retries

Every public operation normalizes expected input, filesystem, Git and SQLite
failures. Catch `BlackBoxError`. Its `str()` and `code` are a fixed category;
`retryable` is a boolean. Underlying exception messages, input values, paths and
SQLite diagnostics are not included in public messages or displayed chains.
Unexpected programmer defects and process-control exceptions are not an automatic
retry contract.

| Error | Code | Retryable |
| --- | --- | --- |
| `ValidationError` | `invalid_input` | false |
| `DatabaseError` | `database_unavailable` | false |
| `SchemaError` (a DatabaseError) | `unsupported_schema` | false |
| `MigrationRequiredError` (a SchemaError) | `migration_required` | false; requires writer initialization |
| `IntegrityError` (a DatabaseError) | `integrity_failure` | false |
| `ConflictError` | `conflict` | false |
| `BusyError` (a ConflictError) | `database_busy` | true |
| `NotFoundError` | `not_found` | false |
| `ObservationError` | `observation_failed` | true |

Busy means SQLite lock contention. Conflicting request reuse or a second
superseding claim is a nonretryable conflict. An observer failure can be retried
with the same request after the local observation problem is resolved; the existing
durable reservation/failure history remains. BlackBox does not perform retry loops.
False retryability means inspect/remediate, not that repair is impossible. Disk
full, unavailable storage and corruption must not trigger blind automatic retries.

`check_integrity` returns `ok=False` plus sorted bounded categories when it can
inspect the store and finds mismatches. It raises `SchemaError`,
`MigrationRequiredError`, `IntegrityError` or `DatabaseError` when opening or
inspection cannot proceed. It never upgrades authority, authenticates identity,
or proves the truth of a claim. Other read operations do not implicitly perform
a full integrity scan.

## Example consumer

```python
import blackbox

blackbox.initialize("blackbox.sqlite3")
captured = blackbox.capture("blackbox.sqlite3", {
    "request_id": "task-001",
    "producer": "example",
    "claims": [{"source": "caller", "topic": "tests", "statement": "Tests passed"}],
})
claim = blackbox.append_claim("blackbox.sqlite3", captured.session_id, {
    "source": "reviewer", "topic": "review", "statement": "Reviewed locally",
})
session = blackbox.get_session("blackbox.sqlite3", captured.session_id)
events = blackbox.get_timeline("blackbox.sqlite3")
claims = blackbox.get_claims("blackbox.sqlite3", topic="tests")
result = blackbox.check_integrity("blackbox.sqlite3")
assert result.ok
```

## CLI and installed-package verification

Commands, arguments and successful JSON shapes remain unchanged. `init` still
prints only `status`; `claim` prints `claim_id`; `check` prints `ok`,
`schema_version`, `errors`. Exit codes remain 0 for success, 1 for operation or
integrity failure, and 2 for argparse usage errors. Operation failures now expose
bounded `error` and `retryable` fields. Input-file read/decode failures report
`input_unavailable_or_invalid`. `check` retains its result envelope for errors,
including `schema_integrity`, `sqlite_integrity` and `database_unavailable`.

CI installs the built wheel into a disposable environment and executes a copied
external consumer in isolated Python mode, outside the checkout. That consumer
uses only public imports for initialize, capture, append claim, reconstruction,
timeline, claims and integrity. CLI smoke checks run against the same installed
wheel. Released-v1 migration/rollback tests remain in the full suite.

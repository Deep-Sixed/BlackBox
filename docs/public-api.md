# Supported public API — BlackBox 0.6.4

Use `import blackbox` (or named imports from `blackbox`). Its explicit `__all__`
is the supported namespace, including result models, errors and `__version__`.
The eleven operations are also explicitly exported by `blackbox.api`.
Other submodules are implementation details even when Python makes them
accessible as package attributes. Do not depend on raw connections, migration
functions, row dictionaries or private helpers. The wheel includes `py.typed`.

Package 0.3.0 established the public boundary. Package 0.4.0 adds attributed
relationships and schema v3; see [relationship semantics](trace-relationships.md).
Package 0.4.1 keeps schema v3 and hardens observer rejection/retry reporting.
Package 0.5.0 keeps schema v3 and adds chain-head export, anchored integrity
checks and the first broken receipt position; see [chain anchoring](#chain-anchoring).
Package 0.5.1 keeps schema v3; `check_integrity` also recomputes evidence-link IDs
and checks that a link follows the evidence it references.
Package 0.6.0 adds the `blackbox hook` CLI command for host lifecycle hooks and
schema v4, whose only change is the `host_reported` source authority for the
events it records; see [hooks](hooks.md). The Python operations are unchanged;
`SourceRecord.authority` gains the `host_reported` value.
Package 0.6.1 keeps schema v4 and fixes the Git observer's working-tree reads
(below) and the CLI's handling of deeply nested input files.
Package 0.6.2 keeps schema v4; the Git observer ignores the observed repository's
replace refs, which could otherwise hide committed or staged changes.
Package 0.6.3 keeps schema v4; `blackbox hook` records oversized payloads by
digest instead of rejecting them, and a symlinked `.git` is accepted as in Git.
Package 0.6.4 keeps schema v4; the Git observer no longer lets the observed
repository's `diff.ignoreSubmodules`, `.gitmodules` `ignore`, `core.fileMode`,
`.git/info/exclude` or `core.excludesFile` settings hide changes (below).
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
| `integrity.digest`, `append_receipt`, `evidence_errors`, `record_errors`, `receipt_findings`, `chain_head`, `anchor_errors` | INTERNAL | Canonical material and chain mechanics remain storage concerns. |
| `migrations.schema_digest`, `schema_objects`, `validate`, `install`, `v002.upgrade` | INTERNAL | Only writer initialization owns schema evolution; no direct migration API. |
| `models.canonical`, `identity`, `safe_strings`, model validators | INTERNAL | IDs, serialization and input validation are accessed through public operations. |
| `provenance.git`, `paths`, `collect_git` | INTERNAL | Opt into local Git observation using `capture(repo=..., baseline=...)`; do not invoke collection internals. |
| `api._capture_host_report`, `hooks.hook_requests` | INTERNAL | The one code path that records `host_reported` authority. Use the `blackbox hook` CLI; a public Python entry point would let any caller claim host authority. |
| `cli.main` | ADMINISTRATIVE CLI entry point | The supported executable is `blackbox`; importing `main` is not an integration contract. |

## Operations

All database/repository paths accept `str` or `pathlib.Path`. Operations close
their handles before returning; no long-lived service, connection pool or global
store is introduced.

| Operation | Return | Storage access |
| --- | --- | --- |
| `initialize(database)` | `InitializationResult(status="initialized", schema_version=4)` | Writer; creates or migrates. Idempotent. |
| `capture(database, request, *, repo=None, baseline=None)` | `CaptureResult(session_id, status, duplicate)` | Writer; creates/migrates as needed, reserves and captures using existing transactions. |
| `append_claim(database, session, claim, *, target=None, relation=None)` | `ClaimResult(claim_id)` | Writer; may initialize/migrate, then requires an existing committed session. |
| `link_evidence(database, session, link)` | `EvidenceLinkResult(link_id)` | Writer; requires committed origin and existing references. |
| `get_evidence_links(database, *, claim_id=None, session=None, through=None)` | `tuple[EvidenceLinkView, ...]` | Read-only, attributed evidence assertions. |
| `get_claim_relations(database, *, claim_id=None, target_id=None, session=None, through=None)` | `tuple[ClaimRelationView, ...]` | Read-only, newer-to-older relations with both sessions. |
| `get_session(database, session)` | `SessionView` | Read-only consistent snapshot; unknown session raises `NotFoundError`. |
| `get_timeline(database, *, through=None)` | `tuple[TimelineEvent, ...]` | Read-only global event order. |
| `get_claims(database, *, through=None, topic=None)` | `tuple[ClaimView, ...]` | Read-only, derived claim status at the cutoff. |
| `check_integrity(database, *, anchor=None)` | `IntegrityResult(ok, schema_version, errors, first_broken_sequence)` | Read-only; false `ok` reports observed inconsistencies. |
| `get_chain_head(database)` | `ChainHead(sequence, digest)` | Read-only; exports the receipt chain head for safekeeping outside the database. |

`through` is an inclusive, nonnegative SQLite sequence number (maximum
`2**63 - 1`); booleans and coercible strings are rejected. Omitting it means the
current snapshot. Timeline and claim-query results are ordered by event sequence.
Session component collections are ordered by canonical ID except events, which
are chronological. `topic` uses the input identifier grammar.

Reader operations never create or migrate a database. A recognized v1, v2 or v3 database
raises `MigrationRequiredError`; call `initialize` with writer access, then retry
the read. Capture and claim writes retain automatic writer initialization for
CLI compatibility. Deployments can call `initialize` at startup to own this
transition explicitly. Missing/unopenable database files raise `DatabaseError`;
`NotFoundError` refers to an absent session, correction target or evidence-link reference in a valid database.

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

- Operation results: `InitializationResult`, `CaptureResult`, `ClaimResult`,
  `EvidenceLinkResult`.
- Canonical record projections: `SessionRecord`, `SourceRecord`,
  `ObservationRecord`, `EvidenceRecord`, `ClaimRecord`, `ArtifactRecord`,
  `FailureRecord`, `TimelineEvent`. These are detached copies, not writable rows.
- `SourceRecord.authority` is `caller_asserted`, `local_git` (the built-in Git
  observer) or, since 0.6.0, `host_reported` (an event delivered by `blackbox
  hook`). Consumers matching on it exhaustively must handle the new value.
- Typed observation data: `CallerObservation` and `GitObservation`. Their fields
  retain the existing caller/local observer distinction.
  `GitObservation.unstaged_delta` lists tracked paths whose raw working-tree
  bytes, file type or executable bit differ from the index, plus deleted and
  unmerged paths. It never runs repository filters. So since 0.5.0, a file whose
  only difference is a legitimate clean filter (for example LFS or line-ending
  conversion) counts as changed, `assume-unchanged` flags are ignored, and a
  submodule counts only when a different commit is checked out. Since 0.6.1,
  a supplied repository subdirectory is normalized to the filesystem-discovered
  worktree root (without trusting `core.worktree`), so every path is repository-
  root-relative and changes elsewhere in the worktree remain visible. Like Git,
  a path under a symlinked or non-directory parent counts as deleted
  (the symlink is never followed), a sparse-checkout (skip-worktree) path counts
  only if it is present and differs, and a submodule without a checked-out
  commit counts as changed rather than failing the capture. Since 0.6.4 the
  executable bit is compared even when the repository sets `core.fileMode=false`,
  so on a filesystem without executable bits every non-executable file may count
  as changed, and `committed_delta` and `staged_delta` include submodule commit
  changes regardless of the repository's submodule `ignore` settings.
  `GitObservation.untracked_files` lists untracked paths not ignored by a
  `.gitignore` in the working tree; since 0.6.4 `.git/info/exclude` and
  `core.excludesFile` are not applied, and an untracked `.gitignore` is always
  listed, even when it ignores itself. A path marked with `git add -N`
  (intent-to-add) counts as unstaged, not staged, as in Git.
- Derived views: `SessionView` combines canonical projections and lifecycle status;
  `ClaimView` adds `active`, `superseded`, `contested` or `retracted` status to a claim.
  `EvidenceLinkView` and `ClaimRelationView` expose attributed relations and local order.
- Diagnostic results: `IntegrityResult` reports local consistency, not authority;
  `ChainHead` is the receipt chain's latest position and digest.

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
| `ObservationRejectedError` (an ObservationError) | `observation_rejected` | false; remediate the repository first |

Busy means SQLite lock contention. Conflicting request reuse or a second
superseding claim is a nonretryable conflict. An observer failure can be retried
with the same request after the local observation problem is resolved; the existing
durable reservation/failure history remains. A Git snapshot whose HEAD or index
file changes while it is taken fails this way, since its staged and unstaged
reads would describe different moments.
When the Git observer's own metadata (a path or branch name) looks like a
credential, capture raises `ObservationRejectedError`: an unchanged retry fails
the same way, so rename or remove the offending path, then retry the same
request. The session stays `FAILED_RETRYABLE` until then because that lifecycle
state means the durable reservation remains reusable after remediation; the stored
failure has `retryable=0`, matching the public error/CLI hint against blind retry.
BlackBox does not perform retry loops.
False retryability means inspect/remediate, not that repair is impossible. Disk
full, unavailable storage and corruption must not trigger blind automatic retries.

`check_integrity` returns `ok=False` plus sorted bounded categories when it can
inspect the store and finds mismatches. It raises `SchemaError`,
`MigrationRequiredError`, `IntegrityError` or `DatabaseError` when opening or
inspection cannot proceed. It never upgrades authority, authenticates identity,
or proves the truth of a claim. Other read operations do not implicitly perform
a full integrity scan.

`first_broken_sequence` is the stored sequence of the first receipt, in chain
order, that fails continuity, linkage, identity or digest verification: the point
where the local chain stops being trustworthy. It is `None` when the chain is
intact or when findings have no receipt position (for example `record_coverage`,
`foreign_keys` or relationship findings).

## Chain anchoring

The receipt chain alone proves internal consistency, not history: a party able to
rewrite the database file can edit records and recompute every receipt, or roll
the file back to an older consistent state, and `check_integrity` cannot tell.
Anchoring closes that gap up to the moment an anchor was taken.

`get_chain_head` returns `ChainHead(sequence, digest)` for the last receipt; an
empty chain returns sequence `0` and the all-zero genesis digest. It only reads
and does not verify the chain, so check integrity before trusting a head. Store
the head somewhere the database writer cannot rewrite, such as a transparency
log, a timestamping service, a signed commit or a separate operator. BlackBox
deliberately contains no anchoring backend.

`check_integrity(database, anchor=head.model_dump())` accepts that mapping
(`sequence`, `digest`; the anchor is input, so pass a mapping, not the result
object). Each receipt digest covers the previous one, so a matching digest at
the anchored sequence pins every receipt up to it. Two findings are added:
`anchor_mismatch` (the receipt at that sequence has a different digest: history
up to the anchor was rewritten) and `anchor_missing` (the chain no longer reaches
that sequence: records were truncated or the file was rolled back). An anchor
proves nothing about records appended after it; take anchors regularly. Invalid
anchors raise `ValidationError` before the database is opened.

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

Existing successful JSON shapes remain unchanged; `claim --relation` also accepts
`retracts`. New evidence-link operations are exposed through the Python API. `init` still
prints only `status`; `claim` prints `claim_id`; `check` prints `ok`,
`schema_version`, `errors` and, since 0.5.0, `first_broken_sequence`. `head`
prints `sequence` and `digest`; `check --anchor FILE` reads that JSON back, so
`blackbox head > anchor.json` round-trips. An invalid anchor prints the bounded
`invalid_input` error rather than a check envelope. Exit codes remain 0 for success, 1 for operation or
integrity failure, and 2 for argparse usage errors, except `hook`: it never
exits 2, prints nothing on stdout and reports failures on stderr (see
[hooks](hooks.md)). Operation failures now expose
bounded `error` and `retryable` fields. Input-file read/decode failures report
`input_unavailable_or_invalid`. `check` retains its result envelope for errors,
including `schema_integrity`, `sqlite_integrity` and `database_unavailable`.

CI installs the built wheel into a disposable environment and executes a copied
external consumer in isolated Python mode, outside the checkout. That consumer
uses only public imports for initialize, capture, append claim, reconstruction,
timeline, claims, attributed evidence links, cross-session retraction,
integrity and chain-head anchoring. CLI smoke checks against the same installed
wheel run `--help`, `init`, two `capture`s, a `hook` event, a cross-session `claim --relation
retracts`, `claims`, `check`, `head` and `check --anchor`; evidence links have
no CLI command. Migration and rollback tests against the released v1, v2 and
v3 stores remain in the full suite.

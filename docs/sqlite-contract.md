# SQLite contract

BlackBox uses one private local SQLite database. The application ID is
`0x42425831` (`1111644209`). The package requires Python 3.14; CI
verifies on 3.14.5. Writers require WAL,
`foreign_keys=ON`, `synchronous=FULL`, and a 5000 ms busy timeout. Database files
are created with mode `0600`; writers reject existing files with group/other
permissions and reject symlinks. SQLite manages WAL and shared-memory sidecars.
This durability contract depends on the filesystem and device honoring SQLite's
locking and synchronization requests.

## Versions and migration

`PRAGMA user_version` identifies the current format. Schema v1 is the released
`v0.1.0` contract, frozen in `migrations/v001.py`. Its SHA-256 DDL digest is
`75b723971fc692f55bab27b8b8264b134f9f3afeb51038ff5dd19e19350aec17`.
The original `schema_metadata` row is preserved, including its timestamp and
digest. The digest hashes the canonical JSON list of DDL statements, not the
physical database file. Validation also compares actual tables, indexes and
triggers against the known schema; copying a digest cannot hide schema changes.

Schema v2 added `schema_migrations` and `record_receipts`; its definition is frozen
in `migrations/v002.py`. Schema v3 adds `claim_relations` and `evidence_links`.
Fresh databases install v3 directly (`0 → 3`); existing stores use sequential
`1 → 2 → 3` or `2 → 3` upgrades. Each historical definition remains frozen. There is no downgrade or automatic malformed-data repair.

Writer initialization (`blackbox init`, or a capture's writer open) holds
`BEGIN IMMEDIATE`, rechecks the version under the write lock, validates the old
schema, existing observation evidence and (from v2) record receipts, installs the additive schema, backfills
receipts, appends metadata/history and advances `user_version`. All these changes
commit together. Failure, including a write error, rolls them back. No canonical
row, identifier, event sequence, authority or verification classification changes.
The migration history has version, previous version, schema digest and installation
time only. V1 had no migration journal: v2 records the real upgrade, without
inventing a timestamp for a historical v1 installation. Fresh v3 records `0 → 3`.
History and metadata reject UPDATE/DELETE.

Read-only connections use SQLite URI `mode=ro` and `query_only=ON`. They never
migrate. V1/v2 readers using current code must first arrange writer initialization;
`blackbox check` returns a bounded schema error until that occurs. Unknown versions,
wrong application IDs and malformed schemas fail closed. Queries use a consistent
read transaction where multiple reads form one result; WAL allows concurrent
readers while a writer commits.

## Record integrity

V1 observation evidence receipts retain their original meaning and digest.
V2 additionally covers sessions, sources, observations, evidence, claims,
artifacts, events and failures with one receipt per `(record_type, record_id)`.
The exact canonical fields are declared in `integrity.RECORD_FIELDS`. Every stored
field in those records is included, including timestamps, event sequence,
authority and verification; observation JSON is parsed before canonicalization.
JSON key order and whitespace do not change canonical material.

Each chain digest is SHA-256 of canonical JSON containing a format domain
(`blackbox.record.v2` for existing record types, `blackbox.record.v3` for the two
new relation types), receipt sequence, record type, record ID, explicit material
and the previous digest. The first previous digest is 64 zeroes. Receipt sequences
start at 1 and are contiguous. Live receipts are appended in insertion order,
inside the record's transaction. Duplicate requests do not append receipts.
V1 backfill uses the declared record-type order, then ID order within each type
(event sequence for events). This receipt order does not reinterpret event
chronology. Existing records lacking receipts are never silently repaired by a
subsequent capture.

`blackbox check` checks SQLite integrity, foreign keys, v1 observation receipt
coverage/digests, canonical record coverage/digests, orphan/duplicate identities,
sequence continuity, chain continuity and relationship attribution/chronology. Results contain `ok`, `schema_version` and sorted,
deduplicated error categories. Unreadable databases report `database_unavailable`;
unrecognized/malformed schemas report `schema_integrity`, with null schema version.
Results contain no evidence values, exception messages or paths. The CLI exits 1
on failure. Schema v2 adds `schema_version` to the previous check result shape.

## Threat model

Hash integrity is not observer authority. Receipt integrity does not prove truth,
remote identity, or upgrade authority. A backfilled receipt records the canonical
material present at migration time; v1 had no receipts authenticating claims or
other non-observation records before that point.

The chain has no signature or external trusted anchor. A party able to rewrite
the database can rewrite records and recompute receipts, or delete a consistent
suffix of records and receipts. Such a rewrite is outside the local integrity
model. Immutable SQL triggers guard ordinary writes, not arbitrary file access.
The checker detects inconsistencies against locally stored metadata; it cannot
prove complete history against a hostile owner. Receipt checking/backfill currently
requires memory proportional to the stored records and migration holds a writer
lock for the backfill. No external observer authentication is introduced.

## Python consumer boundary

Package 0.4.0 uses schema v3. Public reader operations raise
`MigrationRequiredError` for v1/v2; `initialize` and capture/claim/link writer operations
may migrate. `check_integrity` returns typed findings when inspection succeeds
and raises bounded errors when the database cannot be opened or validated.
The CLI continues to serialize these outcomes into its check-result envelope.
See the [public API contract](public-api.md).


Schema-v3 backfill creates relation rows for historical corrections, binds them
to existing CLAIM events and appends new receipts. It preserves the entire old
receipt prefix and original canonical rows/events. Missing or inconsistent claim
events fail migration rather than receiving fabricated timestamps. See the
[relationship contract](trace-relationships.md) for projections and status rules.

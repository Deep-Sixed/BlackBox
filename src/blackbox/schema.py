"""Versioned SQLite schema; migrations run atomically and reject unknown versions."""

VERSION = 1
APPLICATION_ID = 0x42425831
TABLES = (
    "schema_metadata",
    "sessions",
    "sources",
    "observations",
    "evidence",
    "claims",
    "artifacts",
    "events",
    "failures",
)

DDL = [
    """CREATE TABLE schema_metadata (
        version INTEGER PRIMARY KEY, installed_at TEXT NOT NULL,
        schema_digest TEXT NOT NULL) STRICT""",
    """CREATE TABLE sessions (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL, producer TEXT NOT NULL,
        created_at TEXT NOT NULL) STRICT""",
    """CREATE TABLE sources (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        identity TEXT NOT NULL,
        authority TEXT NOT NULL CHECK(authority IN ('caller_asserted','local_git'))
        ) STRICT""",
    """CREATE TABLE observations (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        source_id TEXT NOT NULL REFERENCES sources(id),
        kind TEXT NOT NULL, data TEXT NOT NULL CHECK(json_valid(data)),
        recorded_at TEXT NOT NULL) STRICT""",
    """CREATE TABLE evidence (
        id TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES observations(id),
        digest TEXT NOT NULL,
        verification TEXT NOT NULL CHECK(verification IN ('unverified','locally_observed'))
        ) STRICT""",
    "CREATE UNIQUE INDEX one_receipt_per_observation ON evidence(observation_id)",
    """CREATE TABLE claims (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        source_id TEXT NOT NULL REFERENCES sources(id),
        topic TEXT NOT NULL, statement TEXT NOT NULL,
        target_id TEXT REFERENCES claims(id),
        relation TEXT CHECK(relation IN ('supersedes','contests')),
        CHECK((target_id IS NULL) = (relation IS NULL))) STRICT""",
    """CREATE UNIQUE INDEX one_successor ON claims(target_id)
        WHERE relation = 'supersedes'""",
    """CREATE TABLE artifacts (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        source_id TEXT NOT NULL REFERENCES sources(id), path TEXT NOT NULL,
        digest TEXT NOT NULL, verification TEXT NOT NULL CHECK(verification='unverified')
        ) STRICT""",
    """CREATE TABLE events (
        sequence INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        kind TEXT NOT NULL, entity_id TEXT NOT NULL,
        recorded_at TEXT NOT NULL) STRICT""",
    """CREATE TABLE failures (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        event_id TEXT NOT NULL REFERENCES events(id),
        code TEXT NOT NULL, retryable INTEGER NOT NULL CHECK(retryable IN (0,1))) STRICT""",
    "CREATE INDEX session_events ON events(session_id, sequence)",
]

for table in TABLES:
    for operation in ("UPDATE", "DELETE"):
        DDL.append(
            f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} "
            f"ON {table} BEGIN SELECT RAISE(ABORT, 'immutable history'); END"
        )

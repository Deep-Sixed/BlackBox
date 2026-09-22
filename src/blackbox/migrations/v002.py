"""Additive v2 schema: migration provenance and canonical record receipts."""

from . import v001

VERSION = 2
TABLES = (*v001.TABLES, "schema_migrations", "record_receipts")
ADDITIONS = [
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        previous_version INTEGER NOT NULL CHECK(previous_version >= 0),
        schema_digest TEXT NOT NULL CHECK(length(schema_digest)=64),
        installed_at TEXT NOT NULL) STRICT""",
    """CREATE TABLE record_receipts (
        sequence INTEGER PRIMARY KEY CHECK(sequence > 0),
        record_type TEXT NOT NULL,
        record_id TEXT NOT NULL,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        previous_digest TEXT NOT NULL CHECK(length(previous_digest)=64),
        UNIQUE(record_type, record_id)) STRICT""",
]
for table in TABLES[-2:]:
    for operation in ("UPDATE", "DELETE"):
        ADDITIONS.append(
            f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} "
            f"ON {table} BEGIN SELECT RAISE(ABORT, 'immutable history'); END"
        )
DDL = [*v001.DDL, *ADDITIONS]


def upgrade(connection):
    from ..integrity import RECORD_FIELDS, append_receipt

    for statement in ADDITIONS:
        connection.execute(statement)
    # Historical ordering is deliberately separate from event chronology.
    # This stable table order + primary key order never rewrites canonical rows.
    for table in RECORD_FIELDS:
        order = "sequence" if table == "events" else "id"
        for row in connection.execute(f"SELECT id FROM {table} ORDER BY {order}"):
            append_receipt(connection, table, row[0])

"""Host-reported source authority for events delivered by host lifecycle hooks.

The only change is one more allowed value in the frozen v1 `sources.authority`
CHECK. SQLite cannot ALTER a CHECK constraint, and rebuilding `sources` would
rename it (its stored definition would then differ from a fresh install) and
drop and recreate a table other tables reference. Widening a CHECK does not
change how any row is stored, so the upgrade uses SQLite's documented procedure
for such changes: rewrite the table's definition in `sqlite_schema` and bump
the schema cookie, inside the caller's transaction. No row, receipt or event is
touched and no receipt is appended.
"""

from .._signals import SchemaIssue
from . import v001, v003

VERSION = 4
TABLES = v003.TABLES
V3_SOURCES = v001.DDL[2]
SOURCES = V3_SOURCES.replace(
    "CHECK(authority IN ('caller_asserted','local_git'))",
    "CHECK(authority IN ('caller_asserted','local_git','host_reported'))",
)
if SOURCES == V3_SOURCES:
    raise SchemaIssue("v1 sources definition changed")
DDL = [SOURCES if statement == V3_SOURCES else statement for statement in v003.DDL]


def upgrade(connection):
    if not connection.in_transaction:
        raise SchemaIssue("migration requires a transaction")
    cookie = connection.execute("PRAGMA schema_version").fetchone()[0]
    connection.execute("PRAGMA writable_schema=ON")
    try:
        changed = connection.execute(
            "UPDATE sqlite_schema SET sql=? "
            "WHERE type='table' AND name='sources' AND sql=?",
            (SOURCES, V3_SOURCES),
        ).rowcount
        # Other connections reload the schema when the cookie changes.
        connection.execute(f"PRAGMA schema_version={cookie + 1}")
    finally:
        connection.execute("PRAGMA writable_schema=OFF")
    if changed != 1:
        raise SchemaIssue("invalid source schema")
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise SchemaIssue("invalid source schema")

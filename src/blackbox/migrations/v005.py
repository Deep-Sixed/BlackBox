"""Query indexes for operational-event lookups; no table, row or receipt changes.

Events are filtered by recording time, source identity and the optional event
fields stored inside `observations.data` (0.8.0). These indexes make those
filters scale without touching the canonical tables: creating an index rewrites
no record, so every receipt and the chain head are unchanged by the upgrade.
"""

from .._signals import SchemaIssue
from . import v004

VERSION = 5
TABLES = v004.TABLES
ADDITIONS = [
    "CREATE INDEX observations_recorded ON observations(recorded_at, id)",
    "CREATE INDEX sources_identity ON sources(identity)",
    """CREATE INDEX observations_correlation ON observations(
        json_extract(data, '$.correlation_id'))
        WHERE json_extract(data, '$.correlation_id') IS NOT NULL""",
    """CREATE INDEX observations_occurred ON observations(
        json_extract(data, '$.occurred_at'))
        WHERE json_extract(data, '$.occurred_at') IS NOT NULL""",
    """CREATE INDEX observations_severity ON observations(
        json_extract(data, '$.severity'), recorded_at)
        WHERE json_extract(data, '$.severity') IS NOT NULL""",
]
DDL = [*v004.DDL, *ADDITIONS]


def upgrade(connection):
    if not connection.in_transaction:
        raise SchemaIssue("migration requires a transaction")
    for statement in ADDITIONS:
        connection.execute(statement)

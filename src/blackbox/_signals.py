"""Internal failure categories, translated at the public boundary."""

import sqlite3


class SchemaIssue(ValueError):
    pass


class MigrationRequired(SchemaIssue):
    pass


class EvidenceIssue(ValueError):
    pass


class DatabaseIssue(ValueError):
    pass


class RequestConflict(ValueError):
    pass


class MissingRecord(ValueError):
    pass


class ClaimConflict(sqlite3.IntegrityError):
    pass


class ObservationIssue(ValueError):
    pass

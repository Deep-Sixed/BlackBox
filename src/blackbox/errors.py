"""Bounded public exceptions; messages never include underlying exception text."""


class BlackBoxError(Exception):
    code = "blackbox_error"
    retryable = False

    def __init__(self):
        super().__init__(self.code)


class ValidationError(BlackBoxError):
    code = "invalid_input"


class DatabaseError(BlackBoxError):
    code = "database_unavailable"


class SchemaError(DatabaseError):
    code = "unsupported_schema"


class MigrationRequiredError(SchemaError):
    code = "migration_required"


class IntegrityError(DatabaseError):
    code = "integrity_failure"


class ConflictError(BlackBoxError):
    code = "conflict"


class BusyError(ConflictError):
    code = "database_busy"
    retryable = True


class NotFoundError(BlackBoxError):
    code = "not_found"


class ObservationError(BlackBoxError):
    code = "observation_failed"
    retryable = True

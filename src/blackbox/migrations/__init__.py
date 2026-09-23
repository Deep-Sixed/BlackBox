"""Sequential, transactional migrations. The caller holds BEGIN IMMEDIATE."""

import hashlib
import sqlite3
from contextlib import closing

from .._signals import EvidenceIssue, SchemaIssue
from ..models import canonical
from . import v001, v002, v003, v004

VERSIONS = (v001, v002, v003, v004)
DEFINITIONS = {module.VERSION: module.DDL for module in VERSIONS}
# UPGRADES[n] moves a version n database to version n + 1.
UPGRADES = {module.VERSION - 1: module.upgrade for module in VERSIONS[1:]}
CURRENT = VERSIONS[-1].VERSION


def schema_digest(version=CURRENT):
    return hashlib.sha256(canonical(DEFINITIONS[version]).encode()).hexdigest()


def schema_objects(connection):
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
        )
    ]


def validate(connection, version):
    if version not in DEFINITIONS:
        raise SchemaIssue("unsupported database identity or schema version")
    with closing(sqlite3.connect(":memory:")) as expected:
        for statement in DEFINITIONS[version]:
            expected.execute(statement)
        if schema_objects(connection) != schema_objects(expected):
            raise SchemaIssue("invalid source schema")
    metadata = list(
        connection.execute("SELECT * FROM schema_metadata ORDER BY version")
    )
    versions = [row["version"] for row in metadata]
    if (
        not versions
        or versions[-1] != version
        or versions != list(range(versions[0], version + 1))
        or any(item not in DEFINITIONS for item in versions)
    ):
        raise SchemaIssue("schema migration digest mismatch")
    for row in metadata:
        if row["schema_digest"] != schema_digest(row["version"]):
            raise SchemaIssue("schema migration digest mismatch")
    if version >= 2:
        history = [
            tuple(row)
            for row in connection.execute(
                "SELECT version,previous_version,schema_digest,installed_at "
                "FROM schema_migrations ORDER BY version"
            )
        ]
        expected_history = [
            (
                row["version"],
                metadata[index - 1]["version"] if index else 0,
                row["schema_digest"],
                row["installed_at"],
            )
            for index, row in enumerate(metadata)
            if row["version"] >= 2
        ]
        if history != expected_history:
            raise SchemaIssue("invalid migration history")


def _record(connection, version, previous_version, timestamp):
    digest = schema_digest(version)
    connection.execute(
        "INSERT INTO schema_metadata VALUES (?,?,?)", (version, timestamp, digest)
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?,?,?,?)",
        (version, previous_version, digest, timestamp),
    )
    connection.execute(f"PRAGMA user_version={version}")


def install(connection, timestamp):
    if not connection.in_transaction:
        raise SchemaIssue("migration requires a transaction")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    if version == 0:
        if application != 0 or schema_objects(connection):
            raise SchemaIssue("refusing to adopt a non-BlackBox database")
        for statement in DEFINITIONS[CURRENT]:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id={v001.APPLICATION_ID}")
        _record(connection, CURRENT, 0, timestamp)
    else:
        if application != v001.APPLICATION_ID:
            raise SchemaIssue("unsupported database identity or schema version")
        validate(connection, version)
        if version < CURRENT:
            from ..integrity import evidence_errors, record_errors

            if evidence_errors(connection) or (
                version >= 2 and record_errors(connection, version=version)
            ):
                raise EvidenceIssue("invalid source evidence")
        while version < CURRENT:
            UPGRADES[version](connection)
            _record(connection, version + 1, version, timestamp)
            version += 1
    validate(connection, CURRENT)

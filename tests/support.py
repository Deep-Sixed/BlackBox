"""Helpers shared by several test modules."""

import sqlite3
import subprocess

from blackbox.integrity import GENESIS, digest

UPGRADE_LEDGER = ("record_receipts", "schema_metadata", "schema_migrations")


def snapshot(path, tables):
    """Every row of `tables`, in storage order."""
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def run_release(release, script, *args, check=True):
    """Run `script` with the interpreter of a released_vN fixture."""
    _, _, python, source, env = release
    return subprocess.run(
        [str(python), "-c", script, *map(str, args)],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def rewrite_consistently(path, *statements):
    """Edit history and recompute every receipt, as a party with file access can.

    Each statement is an SQL string or an (sql, parameters) pair.
    """
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        triggers = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        for trigger in triggers:
            db.execute(f"DROP TRIGGER {trigger['name']}")
        for statement in statements:
            sql, parameters = (
                (statement, ()) if isinstance(statement, str) else statement
            )
            db.execute(sql, parameters)
        previous = GENESIS
        for receipt in db.execute(
            "SELECT * FROM record_receipts ORDER BY sequence"
        ).fetchall():
            table = receipt["record_type"]
            row = db.execute(
                f"SELECT * FROM {table} WHERE id=?", (receipt["record_id"],)
            ).fetchone()
            current = digest(table, row, receipt["sequence"], previous)
            db.execute(
                "UPDATE record_receipts SET previous_digest=?, digest=? "
                "WHERE sequence=?",
                (previous, current, receipt["sequence"]),
            )
            previous = current
        for trigger in triggers:
            db.execute(trigger["sql"])


# Integrity-check a store and print the given sessions, using the public API
# of a released version (v0.3.0 and later).
READ_RECORDS = """
import blackbox as bb, json, sys
assert bb.check_integrity(sys.argv[1]).ok
print(json.dumps([bb.get_session(sys.argv[1], s).model_dump(mode="json")
                  for s in json.loads(sys.argv[2])]))
"""

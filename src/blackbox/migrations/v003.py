"""Attributed evidence links and immutable cross-session claim relations."""

from .._signals import EvidenceIssue
from ..models import identity
from . import v001, v002

VERSION = 3
TABLES = (*v002.TABLES, "claim_relations", "evidence_links")
ADDITIONS = [
    """CREATE TABLE claim_relations (
        id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL UNIQUE REFERENCES claims(id),
        target_id TEXT NOT NULL REFERENCES claims(id),
        relation TEXT NOT NULL CHECK(relation IN ('supersedes','contests','retracts')),
        event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
        CHECK(claim_id != target_id)) STRICT""",
    "CREATE UNIQUE INDEX one_relation_successor ON claim_relations(target_id) WHERE relation='supersedes'",
    """CREATE TABLE evidence_links (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        source_id TEXT NOT NULL REFERENCES sources(id),
        claim_id TEXT NOT NULL REFERENCES claims(id),
        observation_id TEXT REFERENCES observations(id),
        evidence_id TEXT REFERENCES evidence(id),
        artifact_id TEXT REFERENCES artifacts(id),
        relation TEXT NOT NULL CHECK(relation IN ('supports','contradicts','context')),
        recorded_at TEXT NOT NULL,
        CHECK((observation_id IS NOT NULL) + (evidence_id IS NOT NULL) +
              (artifact_id IS NOT NULL) = 1)) STRICT""",
    "CREATE INDEX evidence_links_claim ON evidence_links(claim_id)",
]
ADDITIONS += v001.immutability_triggers(TABLES[-2:])
DDL = [*v002.DDL, *ADDITIONS]


def upgrade(connection):
    from ..integrity import append_receipt, relationship_errors

    for statement in ADDITIONS:
        connection.execute(statement)
    for claim in connection.execute(
        "SELECT * FROM claims WHERE target_id IS NOT NULL ORDER BY id"
    ):
        events = connection.execute(
            "SELECT * FROM events WHERE kind='CLAIM' AND entity_id=?",
            (claim["id"],),
        ).fetchall()
        if len(events) != 1 or events[0]["session_id"] != claim["session_id"]:
            raise EvidenceIssue("invalid historical claim event")
        key = identity("rel", [claim["id"], claim["target_id"], claim["relation"]])
        connection.execute(
            "INSERT INTO claim_relations VALUES (?,?,?,?,?)",
            (key, claim["id"], claim["target_id"], claim["relation"], events[0]["id"]),
        )
        append_receipt(connection, "claim_relations", key)
    if relationship_errors(connection):
        raise EvidenceIssue("invalid historical claim relations")

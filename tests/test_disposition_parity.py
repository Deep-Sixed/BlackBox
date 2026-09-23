"""Retained donor semantics through BlackBox's public contract, not file parity."""

import importlib
from concurrent.futures import ThreadPoolExecutor

import pytest

import blackbox as bb


def test_correction_chain_keeps_prior_evidence_and_recording_cutoffs(tmp_path):
    database = tmp_path / "parity.sqlite3"
    session = bb.capture(
        database, {"request_id": "parity", "producer": "test"}
    ).session_id
    first = bb.append_claim(
        database, session, {"source": "caller", "topic": "fact", "statement": "Initial"}
    ).claim_id
    cutoff = bb.get_timeline(database)[-1].sequence
    correction = {"source": "reviewer", "topic": "fact", "statement": "Corrected"}
    second = bb.append_claim(
        database, session, correction, target=first, relation="supersedes"
    ).claim_id
    count = len(bb.get_timeline(database))
    assert (
        bb.append_claim(
            database, session, correction, target=first, relation="supersedes"
        ).claim_id
        == second
    )
    assert len(bb.get_timeline(database)) == count
    bb.append_claim(
        database,
        session,
        {"source": "reviewer", "topic": "fact", "statement": "Disputed"},
        target=second,
        relation="contests",
    )
    assert [
        (row.id, row.statement, row.status)
        for row in bb.get_claims(database, through=cutoff)
    ] == [(first, "Initial", "active")]
    assert [(row.id, row.status) for row in bb.get_claims(database)][:2] == [
        (first, "superseded"),
        (second, "contested"),
    ]
    bb.initialize(database)  # reopen; no external recovery journal needed
    assert bb.check_integrity(database).ok


def test_failed_correction_rolls_back_record_event_and_receipts(tmp_path, monkeypatch):
    database = tmp_path / "rollback.sqlite3"
    session = bb.capture(
        database, {"request_id": "rollback", "producer": "test"}
    ).session_id
    original = bb.append_claim(
        database, session, {"source": "caller", "topic": "test", "statement": "Before"}
    ).claim_id
    before = bb.get_session(database, session)
    module = importlib.import_module("blackbox.ingest")
    real_connect = module.connect

    def fail_after_claim(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.execute(
            "CREATE TEMP TRIGGER fail_claim_event BEFORE INSERT ON events WHEN NEW.kind='CLAIM' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
        return connection

    monkeypatch.setattr(module, "connect", fail_after_claim)
    with pytest.raises(bb.IntegrityError):
        bb.append_claim(
            database,
            session,
            {"source": "new-reviewer", "topic": "test", "statement": "After"},
            target=original,
            relation="supersedes",
        )
    assert bb.get_session(database, session) == before
    assert bb.check_integrity(database).ok
    monkeypatch.setattr(module, "connect", real_connect)
    bb.append_claim(
        database,
        session,
        {"source": "new-reviewer", "topic": "test", "statement": "After"},
        target=original,
        relation="supersedes",
    )
    assert bb.get_claims(database)[0].status == "superseded"
    assert bb.check_integrity(database).ok


def test_concurrent_supersession_has_one_durable_winner(tmp_path):
    database = tmp_path / "concurrent.sqlite3"
    session = bb.capture(
        database, {"request_id": "concurrent", "producer": "test"}
    ).session_id
    original = bb.append_claim(
        database, session, {"source": "caller", "topic": "test", "statement": "Before"}
    ).claim_id

    def correct(number):
        try:
            return bb.append_claim(
                database,
                session,
                {
                    "source": "reviewer",
                    "topic": "test",
                    "statement": f"Correction {number}",
                },
                target=original,
                relation="supersedes",
            ).claim_id
        except bb.ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = [key for key in pool.map(correct, range(4)) if key is not None]
    assert len(winners) == 1
    assert len(bb.get_claims(database)) == 2
    assert bb.get_claims(database)[0].status == "superseded"
    assert bb.check_integrity(database).ok

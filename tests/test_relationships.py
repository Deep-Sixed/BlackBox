"""Public trust/chronology contract and atomic persistence of attributed links."""

import concurrent.futures
import importlib
import sqlite3

import pytest
from pydantic import ValidationError as ModelValidationError
from support import rewrite_consistently

import blackbox as bb
from blackbox.db import connect
from blackbox.models import identity


@pytest.fixture
def trace(tmp_path):
    path = tmp_path / "trace.sqlite3"
    first = bb.capture(
        path,
        {
            "request_id": "actor",
            "producer": "test",
            "claims": [
                {"source": "actor", "topic": "run", "statement": "Tests passed"}
            ],
            "observations": [{"source": "actor", "kind": "test", "name": "pytest"}],
            "artifacts": [
                {"source": "actor", "path": "report.txt", "digest": "a" * 64}
            ],
        },
    ).session_id
    second = bb.capture(path, {"request_id": "review", "producer": "test"}).session_id
    return path, bb.get_session(path, first), second


def link_input(snapshot, record_type="evidence", relation="supports"):
    field = {
        "observation": "observations",
        "evidence": "evidence",
        "artifact": "artifacts",
    }[record_type]
    return {
        "source": "blackbox.git",
        "claim_id": snapshot.claims[0].id,
        "evidence_record_id": getattr(snapshot, field)[0].id,
        "record_type": record_type,
        "relation": relation,
    }


def counts(path):
    with sqlite3.connect(path) as db:
        tables = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
        }


@pytest.mark.parametrize("kind", ["observation", "evidence", "artifact"])
@pytest.mark.parametrize("relation", ["supports", "contradicts", "context"])
def test_attributed_evidence_link_keeps_origins_and_authority(trace, kind, relation):
    path, old, origin = trace
    before = bb.get_timeline(path)[-1].sequence
    link = link_input(old, kind, relation)
    result = bb.link_evidence(path, origin, link)
    assert isinstance(result, bb.EvidenceLinkResult)
    saved = counts(path)
    assert bb.link_evidence(path, origin, link) == result
    assert counts(path) == saved
    (view,) = bb.get_evidence_links(path, claim_id=old.claims[0].id, session=origin)
    assert isinstance(view, bb.EvidenceLinkView)
    assert view.id == result.link_id and view.origin_session_id == origin
    assert view.relation == relation and view.record_type == kind
    assert view.evidence_record_id == link["evidence_record_id"]
    assert bb.get_evidence_links(path, through=before) == ()
    assert bb.get_evidence_links(path, through=view.sequence) == (view,)
    assert bb.get_evidence_links(path, session=old.session.id) == ()
    assert bb.get_evidence_links(path, claim_id="absent") == ()
    (source,) = bb.get_session(path, origin).sources
    assert view.source_id == source.id and source.authority == "caller_asserted"
    assert source.identity == "blackbox.git"  # A caller cannot self-promote.
    assert bb.get_session(path, old.session.id) == old
    event = next(e for e in bb.get_timeline(path) if e.entity_id == result.link_id)
    assert event.sequence == view.sequence and event.recorded_at == view.recorded_at
    assert bb.check_integrity(path).ok
    with pytest.raises(ModelValidationError):
        view.relation = "context"


@pytest.mark.parametrize(
    "relation,status",
    [
        ("supersedes", "superseded"),
        ("contests", "contested"),
        ("retracts", "retracted"),
    ],
)
def test_cross_session_relation_preserves_history_and_cutoff(trace, relation, status):
    path, old, origin = trace
    cutoff = bb.get_timeline(path)[-1].sequence
    value = {
        "source": "reviewer",
        "topic": "review",
        "statement": "Different conclusion",
    }
    result = bb.append_claim(
        path, origin, value, target=old.claims[0].id, relation=relation
    )
    saved = counts(path)
    assert (
        bb.append_claim(path, origin, value, target=old.claims[0].id, relation=relation)
        == result
    )
    assert counts(path) == saved
    (rel,) = bb.get_claim_relations(path, target_id=old.claims[0].id, session=origin)
    assert isinstance(rel, bb.ClaimRelationView)
    assert rel.claim_id == result.claim_id and rel.relation == relation
    assert rel.origin_session_id == origin and rel.target_session_id == old.session.id
    assert rel.source_id == bb.get_session(path, origin).claims[0].source_id
    assert bb.get_claim_relations(
        path, claim_id=result.claim_id, through=rel.sequence
    ) == (rel,)
    assert bb.get_claim_relations(path, through=cutoff) == ()
    assert bb.get_claim_relations(path, session=old.session.id) == ()
    assert bb.get_claim_relations(path, target_id="absent") == ()
    assert bb.get_claims(path, through=cutoff)[0].status == "active"
    assert bb.get_claims(path, topic="run")[0].status == status
    assert bb.get_session(path, old.session.id) == old
    (projected,) = bb.get_session(path, origin).claims
    assert projected.target_id == old.claims[0].id and projected.relation == relation
    assert bb.check_integrity(path).ok


def test_status_precedence_is_assertion_history_not_recursive_truth(trace):
    path, old, origin = trace
    target = old.claims[0].id
    new = {}
    for relation in ("contests", "supersedes", "retracts"):
        new[relation] = bb.append_claim(
            path,
            origin,
            {"source": "reviewer", "topic": "review", "statement": relation},
            target=target,
            relation=relation,
        ).claim_id
    assert bb.get_claims(path, topic="run")[0].status == "retracted"
    bb.append_claim(
        path,
        origin,
        {"source": "actor", "topic": "review", "statement": "Withdraw retraction"},
        target=new["retracts"],
        relation="retracts",
    )
    assert bb.get_claims(path, topic="run")[0].status == "retracted"
    assert bb.check_integrity(path).ok


@pytest.mark.parametrize(
    "change",
    [
        {"quote": "raw content"},
        {"confidence": 0.9},
        {"observed_at": "today"},
        {"relation": "proves"},
        {"record_type": "payload"},
        {"source": "Bearer hidden"},
        {"evidence_record_id": 10},
        {"claim_id": ""},
    ],
)
def test_link_rejects_unbounded_or_invalid_input_before_writes(trace, change):
    path, old, origin = trace
    before = counts(path)
    with pytest.raises(bb.ValidationError):
        bb.link_evidence(path, origin, {**link_input(old), **change})
    assert counts(path) == before


@pytest.mark.parametrize("reference", ["claim_id", "evidence_record_id", "origin"])
def test_missing_link_reference_has_no_partial_effect(trace, reference):
    path, old, origin = trace
    value = link_input(old)
    if reference == "origin":
        origin = "absent"
    else:
        value[reference] = "absent"
    before = counts(path)
    with pytest.raises(bb.NotFoundError):
        bb.link_evidence(path, origin, value)
    assert counts(path) == before


def test_missing_correction_target_has_no_partial_effect(trace):
    path, _, origin = trace
    before = counts(path)
    with pytest.raises(bb.NotFoundError):
        bb.append_claim(
            path,
            origin,
            {"source": "new", "topic": "run", "statement": "Absent"},
            target="absent",
            relation="retracts",
        )
    assert counts(path) == before


@pytest.mark.parametrize("table", ["claim_relations", "evidence_links"])
def test_relationship_failure_rolls_back_sources_events_receipts_and_retries(
    trace, monkeypatch, table
):
    path, old, origin = trace
    module = importlib.import_module("blackbox.ingest")
    original = module.connect
    before = counts(path)

    def fail(path):
        db = original(path)
        db.execute(
            f"CREATE TEMP TRIGGER fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        return db

    monkeypatch.setattr(module, "connect", fail)

    def write():
        if table == "evidence_links":
            return bb.link_evidence(path, origin, link_input(old))
        return bb.append_claim(
            path,
            origin,
            {"source": "new", "topic": "run", "statement": "Withdraw"},
            target=old.claims[0].id,
            relation="retracts",
        )

    with pytest.raises(bb.IntegrityError):
        write()
    assert counts(path) == before
    monkeypatch.setattr(module, "connect", original)
    write()
    assert bb.check_integrity(path).ok


def test_concurrent_cross_session_supersession_has_one_winner(trace):
    path, old, _ = trace
    origins = [
        bb.capture(path, {"request_id": f"review-{n}", "producer": "test"}).session_id
        for n in range(4)
    ]

    def write(origin):
        try:
            return bb.append_claim(
                path,
                origin,
                {"source": "reviewer", "topic": "run", "statement": "Replace"},
                target=old.claims[0].id,
                relation="supersedes",
            )
        except bb.ConflictError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, origins))
    assert sum(r is not None for r in results) == 1
    assert len(bb.get_claim_relations(path)) == 1
    assert bb.check_integrity(path).ok


def test_concurrent_identical_links_commit_once(trace):
    path, old, origin = trace

    def write(_):
        return bb.link_evidence(path, origin, link_input(old))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    assert len({r.link_id for r in results}) == 1
    assert len(bb.get_evidence_links(path)) == 1
    assert bb.check_integrity(path).ok


def test_new_readers_never_create_or_migrate(tmp_path, v2_database):
    missing = tmp_path / "missing.sqlite3"
    for reader in (bb.get_evidence_links, bb.get_claim_relations):
        with pytest.raises(bb.DatabaseError):
            reader(missing)
        assert not missing.exists()
        before = v2_database.read_bytes()
        with pytest.raises(bb.MigrationRequiredError):
            reader(v2_database)
        assert v2_database.read_bytes() == before


@pytest.mark.parametrize("reader", [bb.get_evidence_links, bb.get_claim_relations])
def test_new_readers_bound_invalid_filters(trace, reader):
    path, _, _ = trace
    for kwargs in (
        {"through": True},
        {"through": -1},
        {"session": 8},
        {"claim_id": ""},
    ):
        with pytest.raises(bb.ValidationError):
            reader(path, **kwargs)


def test_reserved_origin_cannot_assert_relationship(trace, monkeypatch):
    path, old, _ = trace
    module = importlib.import_module("blackbox.ingest")

    def interrupted(*args, **kwargs):
        raise SystemExit(73)

    monkeypatch.setattr(module, "claim_row", interrupted)
    with pytest.raises(SystemExit):
        bb.capture(
            path,
            {
                "request_id": "reserved",
                "producer": "test",
                "claims": [
                    {"source": "actor", "topic": "run", "statement": "Interrupted"}
                ],
            },
        )
    origin = identity("ses", "reserved")
    before = counts(path)
    with pytest.raises(bb.ConflictError):
        bb.link_evidence(path, origin, link_input(old))
    assert counts(path) == before


def test_reader_snapshot_survives_concurrent_link_commit(trace):
    path, old, origin = trace
    reader = connect(path, readonly=True)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM evidence_links").fetchone()[0] == 0
        bb.link_evidence(path, origin, link_input(old))
        assert reader.execute("SELECT count(*) FROM evidence_links").fetchone()[0] == 0
        reader.execute("COMMIT")
        assert len(bb.get_evidence_links(path)) == 1
    finally:
        reader.close()


@pytest.mark.parametrize("table", ["claim_relations", "evidence_links"])
def test_new_rows_are_immutable_and_tampering_is_detected(trace, table):
    path, old, origin = trace
    bb.link_evidence(path, origin, link_input(old))
    bb.append_claim(
        path,
        origin,
        {"source": "reviewer", "topic": "run", "statement": "Withdraw"},
        target=old.claims[0].id,
        relation="retracts",
    )
    with sqlite3.connect(path) as db:
        for operation in (f"DELETE FROM {table}", f"UPDATE {table} SET id=id"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(operation)
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (table + "_no_update",)
        ).fetchone()[0]
        db.execute(f"DROP TRIGGER {table}_no_update")
        value = "context" if table == "evidence_links" else "contests"
        db.execute(f"UPDATE {table} SET relation=?", (value,))
        db.execute(ddl)
    assert "record_integrity" in bb.check_integrity(path).errors


def test_relation_coverage_detects_deleted_retraction(trace):
    path, old, origin = trace
    bb.append_claim(
        path,
        origin,
        {"source": "reviewer", "topic": "run", "statement": "Withdraw"},
        target=old.claims[0].id,
        relation="retracts",
    )
    with sqlite3.connect(path) as db:
        triggers = list(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE tbl_name='claim_relations' AND type='trigger'"
            )
        )
        for name, _ in triggers:
            db.execute(f"DROP TRIGGER {name}")
        db.execute("DELETE FROM claim_relations")
        for _, ddl in triggers:
            db.execute(ddl)
    assert "orphan_receipt" in bb.check_integrity(path).errors


def test_observed_evidence_does_not_promote_linking_source(trace):
    from pathlib import Path

    path, old, origin = trace
    witnessed = bb.capture(
        path,
        {"request_id": "observer", "producer": "test"},
        repo=Path(__file__).resolve().parents[1],
    ).session_id
    observation = bb.get_session(path, witnessed)
    assert observation.evidence[0].verification == "locally_observed"
    assert observation.sources[0].authority == "local_git"
    value = {**link_input(old), "evidence_record_id": observation.evidence[0].id}
    bb.link_evidence(path, origin, value)
    (view,) = bb.get_evidence_links(path)
    assert len({old.session.id, origin, witnessed}) == 3
    assert view.origin_session_id == origin and view.claim_id == old.claims[0].id
    assert bb.get_session(path, origin).sources[0].authority == "caller_asserted"
    assert bb.get_session(path, witnessed) == observation
    assert bb.check_integrity(path).ok


@pytest.mark.parametrize("kind", ["observation", "evidence", "artifact"])
def test_link_content_must_match_its_id(trace, kind):
    path, old, origin = trace
    bb.link_evidence(path, origin, link_input(old, kind))
    assert bb.check_integrity(path).ok
    rewrite_consistently(path, ("UPDATE evidence_links SET relation='contradicts'", ()))
    result = bb.check_integrity(path)
    assert result.errors == ("relationship_integrity",)
    assert result.first_broken_sequence is None  # every receipt was recomputed


def test_link_cannot_point_at_evidence_recorded_after_it(trace):
    path, old, origin = trace
    link = link_input(old)
    link_id = bb.link_evidence(path, origin, link).link_id
    later = bb.get_session(
        path,
        bb.capture(
            path,
            {
                "request_id": "later",
                "producer": "test",
                "observations": [{"source": "actor", "kind": "test", "name": "late"}],
            },
        ).session_id,
    )
    moved = {**link, "evidence_record_id": later.evidence[0].id}
    moved_id = identity("link", [origin, moved])
    assert bb.check_integrity(path).ok
    rewrite_consistently(
        path,
        (
            "UPDATE evidence_links SET id=?, evidence_id=? WHERE id=?",
            (moved_id, later.evidence[0].id, link_id),
        ),
        ("UPDATE events SET entity_id=? WHERE entity_id=?", (moved_id, link_id)),
        (
            "UPDATE record_receipts SET record_id=? WHERE record_id=?",
            (moved_id, link_id),
        ),
    )
    # The ID now matches its content; only chronology can catch the move.
    assert bb.check_integrity(path).errors == ("relationship_integrity",)

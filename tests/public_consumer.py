"""External consumer acceptance: supported imports only; copied outside the repo."""

import sys
from importlib.metadata import version
from pathlib import Path

import blackbox as bb


def exercise(database):
    assert version("blackbox") == bb.__version__ == "0.6.7"
    initialized = bb.initialize(database)
    assert isinstance(initialized, bb.InitializationResult)
    assert initialized.schema_version == 4
    request = {
        "request_id": "external-consumer",
        "producer": "consumer",
        "observations": [{"source": "caller", "kind": "test", "name": "contract"}],
        "claims": [{"source": "caller", "topic": "contract", "statement": "First"}],
        "artifacts": [{"source": "caller", "path": "result.txt", "digest": "a" * 64}],
    }
    result = bb.capture(database, request)
    assert isinstance(result, bb.CaptureResult)
    assert result.status == "COMMITTED" and not result.duplicate
    assert bb.capture(database, request).duplicate
    snapshot = bb.get_session(database, result.session_id)
    assert isinstance(snapshot, bb.SessionView)
    assert snapshot.session.request_id == "external-consumer"
    assert snapshot.evidence[0].verification == "unverified"
    assert snapshot.sources[0].authority == "caller_asserted"
    added = bb.append_claim(
        database,
        result.session_id,
        {"source": "caller", "topic": "contract", "statement": "Correction"},
        target=snapshot.claims[0].id,
        relation="supersedes",
    )
    assert isinstance(added, bb.ClaimResult)
    assert len(bb.get_session(database, result.session_id).claims) == 2
    claims = bb.get_claims(database, topic="contract")
    assert all(isinstance(claim, bb.ClaimView) for claim in claims)
    assert claims[0].status == "superseded" and claims[1].id == added.claim_id
    events = bb.get_timeline(database)
    assert all(isinstance(event, bb.TimelineEvent) for event in events)
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )
    assert bb.get_timeline(database, through=events[0].sequence) == events[:1]
    review = bb.capture(
        database, {"request_id": "review", "producer": "consumer"}
    ).session_id
    retract = bb.append_claim(
        database,
        review,
        {"source": "reviewer", "topic": "contract", "statement": "Withdraw correction"},
        target=added.claim_id,
        relation="retracts",
    )
    (relation,) = bb.get_claim_relations(database, claim_id=retract.claim_id)
    assert isinstance(relation, bb.ClaimRelationView)
    assert (
        relation.origin_session_id == review
        and relation.target_session_id == result.session_id
    )
    link = bb.link_evidence(
        database,
        review,
        {
            "source": "reviewer",
            "claim_id": retract.claim_id,
            "record_type": "evidence",
            "evidence_record_id": snapshot.evidence[0].id,
            "relation": "context",
        },
    )
    assert isinstance(link, bb.EvidenceLinkResult)
    (linked,) = bb.get_evidence_links(database, session=review)
    assert isinstance(linked, bb.EvidenceLinkView) and linked.id == link.link_id
    assert bb.get_evidence_links(database, through=linked.sequence - 1) == ()
    assert (
        next(c for c in bb.get_claims(database) if c.id == added.claim_id).status
        == "retracted"
    )
    assert bb.get_session(database, result.session_id).session == snapshot.session
    checked = bb.check_integrity(database)
    assert isinstance(checked, bb.IntegrityResult) and checked.ok
    assert checked.model_dump(mode="json") == {
        "ok": True,
        "schema_version": 4,
        "errors": [],
        "first_broken_sequence": None,
    }
    head = bb.get_chain_head(database)
    assert isinstance(head, bb.ChainHead) and head.sequence > 0
    assert bb.check_integrity(database, anchor=head.model_dump()).ok
    try:
        bb.capture(database, {**request, "producer": "different"})
    except bb.ConflictError as error:
        assert error.code == "conflict" and not error.retryable
    else:
        raise AssertionError("conflicting identity accepted")
    print(
        "Installed public API: initialize, capture, claims, reconstruction, timeline, attributed evidence links, cross-session retraction, integrity, chain head passed"
    )


if __name__ == "__main__":
    exercise(Path(sys.argv[1]))

"""External consumer acceptance: supported imports only; copied outside the repo."""

import sys
from importlib.metadata import version
from pathlib import Path

import blackbox as bb


def exercise(database):
    assert version("blackbox") == bb.__version__ == "0.3.0"
    initialized = bb.initialize(database)
    assert isinstance(initialized, bb.InitializationResult)
    assert initialized.schema_version == 2
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
    checked = bb.check_integrity(database)
    assert isinstance(checked, bb.IntegrityResult) and checked.ok
    assert checked.model_dump(mode="json") == {
        "ok": True,
        "schema_version": 2,
        "errors": [],
    }
    try:
        bb.capture(database, {**request, "producer": "different"})
    except bb.ConflictError as error:
        assert error.code == "conflict" and not error.retryable
    else:
        raise AssertionError("conflicting identity accepted")
    print(
        "Installed public API: initialize, capture, claims, reconstruction, timeline, integrity passed"
    )


if __name__ == "__main__":
    exercise(Path(sys.argv[1]))

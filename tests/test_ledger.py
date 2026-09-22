from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from ledger.gateway import _receipt_from_body, app
from ledger.models import SourceRef
from ledger.parse import claim_from_mapping, parse_claim_file
from ledger.repository import (
    _format_scalar,
    add_claim,
    claim_path,
    claims_dir,
    contest,
    load_claims,
    receipts_jsonl,
    supersede,
)
from ledger.search import search_claims
from ledger.validate import validate_claims

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ledger_pluto"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "yes",
        "no",
        "on",
        "off",
        "true",
        "false",
        "null",
        "~",
        "12",
        "1.0",
        "0x1F",
        ".inf",
        ".nan",
        "-",
        "2026-08-08",
        "build:",
        "https://example.test/a",
        "evidence with spaces",
    ],
)
def test_format_scalar_preserves_strings_through_standard_yaml(value: str) -> None:
    parsed = yaml.safe_load(f"value: {_format_scalar(value)}\n")["value"]

    assert isinstance(parsed, str)
    assert parsed == value


def test_pluto_fixture_loads_four_claims() -> None:
    claims = load_claims(FIXTURE)
    assert len(claims) == 4
    assert {c.id for c in claims} == {
        "clm-2026-0001",
        "clm-2026-0002",
        "clm-2026-0003",
        "clm-2026-0004",
    }


def test_pluto_check_passes() -> None:
    claims = load_claims(FIXTURE)
    report = validate_claims(FIXTURE, claims)
    assert report.ok, report.errors


def test_search_as_of_2005_returns_two_claims() -> None:
    claims = load_claims(FIXTURE)
    results = search_claims(claims, "pluto planet", as_of=date(2005, 1, 1))
    ids = [claim.id for _, claim in results]
    assert "clm-2026-0001" in ids
    assert "clm-2026-0002" in ids
    assert "clm-2026-0003" not in ids


def test_supersession_reciprocal_links() -> None:
    path = FIXTURE / "30-ledger" / "claims" / "pluto.md"
    claims = parse_claim_file(path)
    by_id = {c.id: c for c in claims}
    assert by_id["clm-2026-0003"].supersedes == "clm-2026-0002"
    assert by_id["clm-2026-0002"].superseded_by == "clm-2026-0003"


def test_valid_on_end_exclusive() -> None:
    claims = load_claims(FIXTURE)
    by_id = {c.id: c for c in claims}
    claim = by_id["clm-2026-0002"]
    assert claim.valid_on(date(2006, 8, 23))
    assert not claim.valid_on(date(2006, 8, 24))


def _claim(claim_id: str, statement: str = "Ledger records a fact"):
    return claim_from_mapping(
        {
            "id": claim_id,
            "statement": statement,
            "topic": "ledger",
            "type": "fact",
            "sources": [{"ref": "docs/source.md", "quote": "evidence"}],
            "confidence": "high",
            "status": "active",
            "created": "2026-07-02T16:00:00+00:00",
            "updated": "2026-07-02T16:00:00+00:00",
            "source_type": "file",
            "source_ref": "docs/source.md",
            "source_hash": "sha256:abc123",
        }
    )


def test_claim_creation_uses_canonical_repository_layout(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-1001"))

    assert claims_dir(tmp_path) == tmp_path / "storage" / "ledger" / "claims"
    assert claim_path(tmp_path, "clm-2026-1001").is_file()
    assert not (tmp_path / "storage" / "ledger" / "claims" / "claims.jsonl").exists()
    assert [claim.id for claim in load_claims(tmp_path)] == ["clm-2026-1001"]
    assert "```yaml" in claim_path(tmp_path, "clm-2026-1001").read_text(encoding="utf-8")


def test_load_claims_accepts_ledger_storage_root(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-1001"))

    claims = load_claims(tmp_path / "storage" / "ledger")

    assert [claim.id for claim in claims] == ["clm-2026-1001"]


def test_supersede_writes_old_superseded_and_new_active_claim(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-1001"))
    supersede(tmp_path, "clm-2026-1001", _claim("clm-2026-1002", "Ledger records a corrected fact"))

    by_id = {claim.id: claim for claim in load_claims(tmp_path)}
    assert by_id["clm-2026-1001"].status == "superseded"
    assert by_id["clm-2026-1001"].superseded_by == "clm-2026-1002"
    assert by_id["clm-2026-1002"].supersedes == "clm-2026-1001"


def test_contest_marks_claim_contested(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-1001"))
    contest(tmp_path, "clm-2026-1001", reason="source is disputed", contested_by="Codex_Desktop")

    claim = load_claims(tmp_path)[0]
    assert claim.status == "contested"
    assert claim.confidence == "contested"


def test_source_pointer_fields_round_trip(tmp_path: Path) -> None:
    add_claim(tmp_path, _claim("clm-2026-1001"))

    claim = load_claims(tmp_path)[0]
    assert claim.source_type == "file"
    assert claim.source_ref == "docs/source.md"
    assert claim.source_hash == "sha256:abc123"


def test_receipt_enrichment_excludes_sensitive_payload_fields() -> None:
    receipt = _receipt_from_body(
        {
            "surface": "ledger",
            "tool": "shell.exec",
            "request_id": "req-1",
            "session_id": "sess-1",
            "agent_identity": "Codex_Desktop",
            "user_identity": "Charles",
            "result": {"secret": "do-not-log"},
            "args": {"cmd": "do-not-log"},
            "Authorization": "Bearer do-not-log",
        }
    )

    assert receipt["schema_version"] == "ledger.receipt.v1"
    assert receipt["receipt_id"].startswith("rcpt-")
    assert receipt["timestamp"]
    assert receipt["surface"] == "ledger"
    assert receipt["tool"] == "shell.exec"
    assert receipt["agent_identity"] == "Codex_Desktop"
    assert receipt["user_identity"] == "Charles"
    assert receipt["session_id"] == "sess-1"
    assert receipt["request_id"] == "req-1"
    assert receipt["correlation_id"] == "req-1"
    assert receipt["status"] == "observed"
    assert "args" not in receipt
    assert "result" not in receipt
    assert "Authorization" not in receipt


def test_missing_identity_receipt_uses_empty_fields() -> None:
    receipt = _receipt_from_body({"surface": "ledger", "tool": "fs.read"})

    assert receipt["agent_identity"] == ""
    assert receipt["user_identity"] == ""
    assert receipt["request_id"] == ""


def test_record_endpoint_writes_passive_receipt_not_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))

    client = fastapi_testclient.TestClient(app)
    response = client.post("/v1/record", json={"surface": "ledger", "tool": "fs.read", "request_id": "req-1", "result": {"ok": True}})

    assert response.status_code == 200
    receipt = json.loads(receipts_jsonl(tmp_path).read_text(encoding="utf-8"))
    assert receipt["request_id"] == "req-1"
    assert not claims_dir(tmp_path).exists()


def test_record_endpoint_rejects_malformed_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))

    client = fastapi_testclient.TestClient(app)

    assert client.post("/v1/record", content="{bad json").status_code == 400
    assert client.post("/v1/record", json=["not", "object"]).status_code == 400


def test_search_uses_bm25_relevance(tmp_path: Path) -> None:
    broad = _claim("clm-2026-1001", "Ledger records a claim")
    specific = _claim("clm-2026-1002", "Ledger records BM25 BM25 BM25 ranking behavior")
    add_claim(tmp_path, broad)
    add_claim(tmp_path, specific)

    results = search_claims(load_claims(tmp_path), "bm25 ranking")

    assert [claim.id for _score, claim in results][:2] == ["clm-2026-1002"]


def test_validator_rejects_bad_source_hash_and_unknown_contradiction(tmp_path: Path) -> None:
    source = tmp_path / "docs" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("evidence", encoding="utf-8")
    claim = _claim("clm-2026-1001")
    claim.source_hash = "md5:not-ok"
    claim.contradicts = ["clm-2026-9999"]
    add_claim(tmp_path, claim)

    report = validate_claims(tmp_path, load_claims(tmp_path))

    assert not report.ok
    assert any("source_hash must use sha256" in error for error in report.errors)
    assert any("contradicts unknown id" in error for error in report.errors)


def test_superseded_claim_bad_source_ref_downgrades_to_warning(tmp_path: Path) -> None:
    bad_source = SourceRef(ref="/absolute/escapes/repo.py", quote="q", source_type="repo-file")
    old = _claim("clm-2026-1001")
    old.sources = [bad_source]
    add_claim(tmp_path, old)

    # Still active: escaping repo-file source is a hard error (live evidence must resolve).
    report = validate_claims(tmp_path, load_claims(tmp_path))
    assert not report.ok
    assert any("source ref escapes repo" in error for error in report.errors)

    new = _claim("clm-2026-1002", "Corrected fact")
    supersede(tmp_path, "clm-2026-1001", new)

    # Once superseded, the old claim's now-immutable bad ref can never be fixed —
    # it must not keep failing the whole store closed.
    report = validate_claims(tmp_path, load_claims(tmp_path))
    assert report.ok, report.errors
    assert any("legacy external source ref is not verified" in warning for warning in report.warnings)


def test_validator_warns_for_legacy_unresolved_evidence(tmp_path: Path) -> None:
    claim = _claim("clm-2026-1001")
    add_claim(tmp_path, claim)

    report = validate_claims(tmp_path, load_claims(tmp_path))

    assert report.ok
    assert any("legacy unresolved source ref" in warning for warning in report.warnings)


def test_validator_rejects_missing_typed_repo_file(tmp_path: Path) -> None:
    claim = _claim("clm-2026-1001")
    claim.sources = [SourceRef(ref="docs/missing.md", quote="evidence", source_type="repo-file")]
    add_claim(tmp_path, claim)

    report = validate_claims(tmp_path, load_claims(tmp_path))

    assert not report.ok
    assert any("source ref not found" in error for error in report.errors)


def test_validator_accepts_evecor_root_evidence_for_agentsync_repo(tmp_path: Path) -> None:
    agentsync = tmp_path / "AgentSync"
    source = tmp_path / "RouterCore" / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("evidence", encoding="utf-8")
    claim = _claim("clm-2026-1001")
    claim.sources = [SourceRef(ref="RouterCore/README.md", quote="evidence", source_type="repo-file")]
    add_claim(agentsync, claim)

    report = validate_claims(agentsync, load_claims(agentsync))

    assert report.ok, report.errors


def test_validator_resolves_known_evecor_migration_aliases(tmp_path: Path) -> None:
    store = tmp_path / "EVECOR" / "governance" / "flight-recorder" / "storage" / "ledger-test"
    archived = (
        tmp_path
        / "EVECOR"
        / "governance"
        / "flight-recorder"
        / "storage"
        / "archive"
        / "AI_SYNC_LEDGER.md"
    )
    archived.parent.mkdir(parents=True)
    archived.write_text("retired", encoding="utf-8")
    claim = _claim("clm-2026-1001")
    claim.sources = [SourceRef(ref="AI_SYNC_LEDGER.md", quote="retired", source_type="repo-file")]
    add_claim(store, claim)

    report = validate_claims(store, load_claims(store))

    assert report.ok, report.errors


def test_contest_linked_creates_dispute_claim_and_preserves_target(tmp_path: Path) -> None:
    from ledger.repository import contest_linked

    source = tmp_path / "docs" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("evidence", encoding="utf-8")
    add_claim(tmp_path, _claim("clm-2026-1001"))

    contest_claim, target = contest_linked(
        tmp_path,
        "clm-2026-1001",
        rationale="Newer measurement contradicts the recorded value",
        sources=_claim("clm-2026-0000").sources,
    )

    by_id = {claim.id: claim for claim in load_claims(tmp_path)}
    assert by_id[contest_claim.id].relations == ["disputes:clm-2026-1001"]
    assert by_id[contest_claim.id].contradicts == ["clm-2026-1001"]
    assert by_id[contest_claim.id].type == "claim"
    assert by_id[contest_claim.id].status == "active"
    assert by_id["clm-2026-1001"].status == "contested"
    assert by_id["clm-2026-1001"].confidence == "contested"
    assert by_id["clm-2026-1001"].statement == "Ledger records a fact"
    assert f"contested_by={contest_claim.id}" in (by_id["clm-2026-1001"].note or "")

    report = validate_claims(tmp_path, load_claims(tmp_path))
    assert report.ok, report.errors


def test_contest_linked_rejects_unknown_target_and_missing_evidence(tmp_path: Path) -> None:
    from ledger.repository import contest_linked

    add_claim(tmp_path, _claim("clm-2026-1001"))
    with pytest.raises(ValueError, match="unknown claim id"):
        contest_linked(tmp_path, "clm-2026-9999", rationale="r", sources=_claim("x").sources)
    with pytest.raises(ValueError, match="evidence source"):
        contest_linked(tmp_path, "clm-2026-1001", rationale="r", sources=[])
    with pytest.raises(ValueError, match="rationale"):
        contest_linked(tmp_path, "clm-2026-1001", rationale="  ", sources=_claim("x").sources)


def test_gateway_no_longer_exposes_claim_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))

    client = fastapi_testclient.TestClient(app)
    for path in ("/v1/claims/add", "/v1/claims/supersede", "/v1/claims/contest", "/v1/claims/search"):
        assert client.post(path, json={}).status_code == 404


def test_mcp_contest_writes_linked_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.mcp_server import SourceInput, do_contest

    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))
    add_claim(tmp_path, _claim("clm-2026-1001"))

    body = do_contest(
        "clm-2026-1001",
        "Disputed by a newer source",
        [SourceInput(ref="docs/source.md", quote="conflicting evidence")],
    )

    assert body["ok"] is True
    assert body["contested_claim_id"] == "clm-2026-1001"
    by_id = {claim.id: claim for claim in load_claims(tmp_path)}
    assert by_id[body["contest_claim_id"]].relations == ["disputes:clm-2026-1001"]
    assert by_id["clm-2026-1001"].status == "contested"

    with pytest.raises(ValueError, match="unknown claim id"):
        do_contest("clm-2026-9999", "r", [SourceInput(ref="a", quote="b")])


def test_mcp_search_compact_and_relations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.mcp_server import do_search

    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))
    add_claim(tmp_path, _claim("clm-2026-1001", "Ledger records BM25 ranking behavior"))
    add_claim(tmp_path, _claim("clm-2026-1002", "Ledger records an unrelated fact"))

    body = do_search("bm25 ranking")
    assert body["ok"] is True
    assert body["limit"] == 20
    top = body["results"][0]
    assert top["claim_id"] == "clm-2026-1001"
    assert top["sources"][0]["ref"] == "docs/source.md"
    assert "relations" not in top

    assert "relations" in do_search("bm25", include_relations=True)["results"][0]
    assert do_search("bm25", limit=500)["limit"] == 100
    assert do_search("bm25", limit=-3)["limit"] == 1

    with pytest.raises(ValueError, match="query is required"):
        do_search("  ")
    with pytest.raises(ValueError):
        do_search("bm25", as_of="not-a-date")


def test_mcp_search_topic_and_as_of_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.mcp_server import do_search

    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))
    dated = _claim("clm-2026-1001", "Ledger fact valid only until 2020")
    dated.valid_until = date(2020, 1, 1)
    add_claim(tmp_path, dated)
    other = _claim("clm-2026-1002", "Ledger fact in another topic")
    other.topic = "other-topic"
    add_claim(tmp_path, other)

    scoped = do_search("ledger fact", topic="other-topic")
    assert [r["claim_id"] for r in scoped["results"]] == ["clm-2026-1002"]

    recent = do_search("ledger fact", as_of="2026-07-02")
    assert "clm-2026-1001" not in [r["claim_id"] for r in recent["results"]]


def test_mcp_build_claim_validation() -> None:
    from ledger.mcp_server import SourceInput, _build_claim

    sources = [SourceInput(ref="docs/source.md", quote="evidence")]
    claim = _build_claim(topic="t", type="fact", statement="s", sources=sources, confidence="high", valid_from=None)
    assert claim.status == "active"
    assert claim.sources[0].ref == "docs/source.md"
    assert claim.valid_from is None

    with pytest.raises(ValueError, match="invalid claim type"):
        _build_claim(topic="t", type="session_summary", statement="s", sources=sources, confidence="high", valid_from=None)
    with pytest.raises(ValueError, match="invalid confidence"):
        _build_claim(topic="t", type="fact", statement="s", sources=sources, confidence="contested", valid_from=None)
    with pytest.raises(ValueError, match="at least one source"):
        _build_claim(topic="t", type="fact", statement="s", sources=[], confidence="high", valid_from=None)


def test_mcp_supersede_links_old_and_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.mcp_server import SourceInput, do_supersede

    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))
    add_claim(tmp_path, _claim("clm-2026-1001"))

    body = do_supersede(
        "clm-2026-1001", "ledger", "fact", "Corrected fact",
        [SourceInput(ref="docs/source.md", quote="newer evidence")],
    )

    assert body["ok"] is True
    by_id = {claim.id: claim for claim in load_claims(tmp_path)}
    assert by_id["clm-2026-1001"].status == "superseded"
    assert by_id["clm-2026-1001"].superseded_by == body["claim_id"]
    assert by_id[body["claim_id"]].supersedes == "clm-2026-1001"


def test_timeline_chain_and_as_of_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.mcp_server import do_timeline

    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))
    old = _claim("clm-2026-1001", "Original fact")
    old.valid_from = date(2000, 1, 1)
    old.valid_until = date(2010, 1, 1)
    add_claim(tmp_path, old)
    new = _claim("clm-2026-1002", "Corrected fact")
    new.valid_from = date(2010, 1, 1)
    supersede(tmp_path, "clm-2026-1001", new)

    chain = do_timeline(claim_id="clm-2026-1002")
    assert [e["claim_id"] for e in chain["entries"]] == ["clm-2026-1001", "clm-2026-1002"]
    assert chain["entries"][0]["superseded_by"] == "clm-2026-1002"

    snapshot = do_timeline(topic="ledger", as_of="2005-06-01")
    assert [e["claim_id"] for e in snapshot["valid_as_of"]] == ["clm-2026-1001"]

    with pytest.raises(ValueError, match="unknown claim id"):
        do_timeline(claim_id="clm-2026-9999")


def test_audit_reports_store_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.mcp_server import do_audit

    monkeypatch.setenv("LEDGER_REPO_ROOT", str(tmp_path))
    source = tmp_path / "docs" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("evidence", encoding="utf-8")
    add_claim(tmp_path, _claim("clm-2026-1001"))

    good = do_audit()
    assert good["ok"] is True
    assert good["claim_count"] == 1

    bad = _claim("clm-2026-1002")
    bad.contradicts = ["clm-2026-9999"]
    add_claim(tmp_path, bad)
    broken = do_audit()
    assert broken["ok"] is False
    assert any("contradicts unknown id" in error for error in broken["errors"])


def test_mcp_server_registers_six_ledger_tools() -> None:
    pytest.importorskip("mcp")
    import asyncio

    from ledger.mcp_server import _build_server

    server = _build_server()
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "ledger_add_claim",
        "ledger_supersede",
        "ledger_contest",
        "ledger_search",
        "ledger_timeline",
        "ledger_audit",
        # Recall rides in the Ledger runtime as a derived-memory read tool;
        # its storage and identity stay separate from Ledger claims.
        "recall_read",
    }
    identity_params = {"agent", "agent_identity", "user", "user_identity", "surface", "session_id"}
    for tool in tools:
        assert not identity_params & set(tool.inputSchema.get("properties", {}))


def test_validator_accepts_interior_parent_segment_that_lands_inside_a_root(tmp_path: Path) -> None:
    """``EVECOR/../labs/x`` normalizes back inside the projects root.

    Claim authors standing in the EVECOR checkout wrote sibling-tree evidence
    this way. Rejecting it lexically flagged a real, resolvable file as a
    traversal attempt.
    """
    store = tmp_path / "EVECOR" / "governance" / "flight-recorder" / "storage" / "ledger-test"
    sibling = tmp_path / "labs" / "cerberus" / "deploy" / "admin-sso-setup.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("evidence", encoding="utf-8")
    claim = _claim("clm-2026-1001")
    claim.source_ref = None
    claim.sources = [
        SourceRef(
            ref="EVECOR/../labs/cerberus/deploy/admin-sso-setup.md",
            quote="evidence",
            source_type="repo-file",
        )
    ]
    add_claim(store, claim)

    report = validate_claims(store, load_claims(store))

    assert report.ok, report.errors
    assert not any("escapes repo" in warning for warning in report.warnings)


def test_validator_still_rejects_refs_that_climb_above_every_root(tmp_path: Path) -> None:
    store = tmp_path / "EVECOR" / "governance" / "flight-recorder" / "storage" / "ledger-test"
    claim = _claim("clm-2026-1001")
    claim.source_ref = None
    claim.sources = [
        SourceRef(ref="EVECOR/../../escape.md", quote="evidence", source_type="repo-file")
    ]
    add_claim(store, claim)

    report = validate_claims(store, load_claims(store))

    assert not report.ok
    assert any("source ref escapes repo" in error for error in report.errors)


def test_validator_accepts_the_id_shape_the_generator_actually_mints(tmp_path: Path) -> None:
    minted = _claim("clm-2026-14f24faf")
    add_claim(tmp_path, minted)
    hand_authored = _claim("clm-2026-phase01b-freeze", "Hand-authored slug id")
    add_claim(tmp_path, hand_authored)

    report = validate_claims(tmp_path, load_claims(tmp_path))
    id_warnings = [w for w in report.warnings if "id does not match" in w]

    assert id_warnings == ["clm-2026-phase01b-freeze: id does not match clm-YYYY-<8 hex>"]

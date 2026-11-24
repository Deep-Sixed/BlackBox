"""Service layer: authentication, authorization, event mapping, queries, limits."""

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

import blackbox as bb
from blackbox import api
from blackbox.errors import DatabaseError
from blackbox.service.auth import (
    ConfigError,
    Registry,
    Unauthorized,
    check_config_file,
    hash_token,
    new_token,
    parse_principals,
)
from blackbox.service.core import Service

WRITER_UID, READER_UID, HOOK_UID = 1000, 1001, 1002
NOW = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)


def principals_toml(entries):
    lines = []
    for name, entry in entries.items():
        lines.append(f"[principals.{name}]")
        for key, value in entry.items():
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def tokens():
    return {name: new_token() for name in ("forge", "reader", "hooks", "other")}


@pytest.fixture
def registry(tokens):
    return Registry(
        parse_principals(
            principals_toml(
                {
                    "forge": {
                        "uids": [WRITER_UID],
                        "scopes": ["events:write", "events:read"],
                        "token_sha256": [tokens["forge"][1]],
                    },
                    "reader": {
                        "uids": [READER_UID],
                        "scopes": ["events:read", "metrics:read"],
                        "token_sha256": [tokens["reader"][1]],
                        "can_read": ["*"],
                    },
                    "hooks": {
                        "uids": [HOOK_UID],
                        "scopes": ["host:report", "events:read"],
                        "token_sha256": [tokens["hooks"][1]],
                    },
                    "other": {
                        "uids": [WRITER_UID],
                        "scopes": ["events:write", "events:read"],
                        "token_sha256": [tokens["other"][1]],
                    },
                }
            )
        )
    )


@pytest.fixture
def service(tmp_path, registry):
    svc = Service(tmp_path / "db" / "bb.sqlite3", registry, clock=lambda: NOW)
    svc.start()
    assert svc.ready_reason is None
    return svc


def call(service, tokens, who, uid, method, route, **kwargs):
    return service.handle(method, route, token=tokens[who][0], uid=uid, **kwargs)


EVENT = {
    "idempotency_key": "evt-1",
    "source": "contextforge",
    "type": "tool.invoke",
    "outcome": "success",
    "severity": "info",
    "occurred_at": "2026-11-24T07:59:00Z",
    "correlation_id": "req-1",
    "duration_ms": 12,
}


def post(service, tokens, body=EVENT, who="forge", uid=WRITER_UID):
    return call(service, tokens, who, uid, "POST", "/api/v1/events", body=body)


def listing(service, tokens, who="reader", uid=READER_UID, **parameters):
    return call(
        service, tokens, who, uid, "GET", "/api/v1/events", parameters=parameters
    )


# --- principals file ------------------------------------------------------------


def test_principals_require_token_hashes_uids_and_known_scopes():
    good = {"uids": [1], "scopes": ["events:read"], "token_sha256": ["a" * 64]}
    for bad in (
        {**good, "uids": []},
        {**good, "uids": ["1000"]},
        {**good, "scopes": ["events:delete"]},
        {**good, "token_sha256": []},
        {**good, "token_sha256": ["a" * 64, "b" * 64, "c" * 64]},
        {**good, "token_sha256": ["not-a-hash"]},
        {**good, "extra": 1},
    ):
        with pytest.raises(ConfigError):
            parse_principals(principals_toml({"p": bad}))
    with pytest.raises(ConfigError):
        parse_principals(principals_toml({"Bad_Name": good}))
    with pytest.raises(ConfigError):
        parse_principals(principals_toml({"a": good, "b": good}))  # shared hash
    with pytest.raises(ConfigError):
        parse_principals("not = [valid")


def test_principals_file_must_not_be_writable_or_readable_by_others(tmp_path):
    path = tmp_path / "principals.toml"
    path.write_text("")
    for mode in (0o666, 0o644, 0o620):
        path.chmod(mode)
        with pytest.raises(ConfigError):
            check_config_file(path)
    path.chmod(0o640)
    check_config_file(path)


# --- authentication and authorization ----------------------------------------


def test_token_and_uid_are_both_required(service, tokens):
    assert post(service, tokens).status == 201
    for token, uid in (
        (None, WRITER_UID),
        ("", WRITER_UID),
        (tokens["forge"][0], None),
        (tokens["forge"][0], READER_UID),  # valid token, wrong uid
        (tokens["reader"][0], WRITER_UID),  # valid uid of another principal
        ("bbx_" + "x" * 43, WRITER_UID),
    ):
        outcome = service.handle(
            "POST", "/api/v1/events", token=token, uid=uid, body=EVENT
        )
        assert outcome.status == 401
        assert outcome.body == {"error": "unauthorized"}
        assert outcome.headers["WWW-Authenticate"] == "Bearer"


def test_scopes_separate_read_and_write(service, tokens):
    assert post(service, tokens, who="reader", uid=READER_UID).status == 403
    outcome = call(service, tokens, "hooks", HOOK_UID, "GET", "/metrics")
    assert outcome.status == 403
    assert call(service, tokens, "reader", READER_UID, "GET", "/metrics").status == 200


def test_rotation_and_revocation_need_only_a_reload(service, tokens):
    old_token, old_hash = tokens["forge"]
    new, new_hash = new_token()
    entry = {"uids": [WRITER_UID], "scopes": ["events:write"]}
    service.reload(
        Registry(
            parse_principals(
                principals_toml(
                    {"forge": {**entry, "token_sha256": [old_hash, new_hash]}}
                )
            )
        )
    )
    for token in (old_token, new):
        body = {**EVENT, "idempotency_key": hash_token(token)[:20]}
        outcome = service.handle(
            "POST", "/api/v1/events", token=token, uid=WRITER_UID, body=body
        )
        assert outcome.status == 201
    service.reload(
        Registry(
            parse_principals(
                principals_toml({"forge": {**entry, "token_sha256": [new_hash]}})
            )
        )
    )
    outcome = service.handle(
        "POST", "/api/v1/events", token=old_token, uid=WRITER_UID, body=EVENT
    )
    assert outcome.status == 401


def test_rate_limit_returns_429_with_retry_after(service, tokens):
    principal = service.registry.authenticate(tokens["forge"][0], WRITER_UID)
    object.__setattr__(principal, "write_rate", (0.001, 2))
    statuses = [
        post(service, tokens, {**EVENT, "idempotency_key": f"r{i}"}).status
        for i in range(3)
    ]
    assert statuses == [201, 201, 429]
    limited = post(service, tokens, {**EVENT, "idempotency_key": "r9"})
    assert limited.headers["Retry-After"] == "1"


# --- writes ---------------------------------------------------------------------


def test_event_ids_match_what_the_library_stores(service, tokens):
    outcome = post(service, tokens)
    assert outcome.status == 201
    view = bb.get_session(service.database, outcome.body["session_id"])
    assert [o.id for o in view.observations] == outcome.body["event_ids"]
    assert outcome.body["event_id"] == outcome.body["event_ids"][0]
    assert view.session.producer == "forge"
    assert view.session.request_id == "forge:evt-1"
    assert bb.check_integrity(service.database).ok


def test_idempotent_retry_and_conflicting_reuse(service, tokens):
    first = post(service, tokens)
    again = post(service, tokens)
    assert (again.status, again.body["duplicate"]) == (200, True)
    assert again.body["event_ids"] == first.body["event_ids"]
    conflict = post(service, tokens, {**EVENT, "outcome": "failure"})
    assert conflict.status == 409 and conflict.body == {"error": "conflict"}


def test_principals_cannot_collide_on_idempotency_keys(service, tokens):
    mine = post(service, tokens)
    theirs = post(service, tokens, who="other")
    assert mine.status == theirs.status == 201
    assert mine.body["session_id"] != theirs.body["session_id"]


def test_batch_is_atomic(service, tokens):
    events = [
        {k: v for k, v in EVENT.items() if k != "idempotency_key"} for _ in range(3)
    ]
    ok = call(
        service,
        tokens,
        "forge",
        WRITER_UID,
        "POST",
        "/api/v1/events/batch",
        body={"idempotency_key": "b1", "events": events},
    )
    assert ok.status == 201 and len(set(ok.body["event_ids"])) == 3
    bad = call(
        service,
        tokens,
        "forge",
        WRITER_UID,
        "POST",
        "/api/v1/events/batch",
        body={"idempotency_key": "b2", "events": [*events, {"source": "x"}]},
    )
    assert bad.status == 422
    with sqlite3.connect(service.database) as db:
        assert db.execute(
            "SELECT count(*) FROM sessions WHERE request_id='forge:b2'"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "token=hunter2"),
        ("source", "Bearer abc"),
        ("correlation_id", "ghp_" + "a" * 36),
        ("occurred_at", "2026-11-24T09:00:00Z"),  # an hour in the future
        ("severity", "fatal"),
        ("unknown", "x"),
    ],
)
def test_invalid_events_are_rejected_without_echo(service, tokens, field, value):
    outcome = post(service, tokens, {**EVENT, field: value})
    assert outcome.status == 422
    assert outcome.body == {"error": "invalid_input"}
    assert value not in json.dumps(outcome.body)


def test_occurred_at_is_normalized_for_range_queries(service, tokens):
    post(service, tokens, {**EVENT, "occurred_at": "2026-11-24T07:59:00.5Z"})
    (event,) = listing(service, tokens).body["events"]
    assert event["occurred_at"] == "2026-11-24T07:59:00.500000Z"


def test_unready_service_refuses_writes(service, tokens):
    service.ready_reason = "integrity_failure"
    outcome = post(service, tokens)
    assert outcome.status == 503 and outcome.headers["Retry-After"] == "5"
    assert service.ready().status == 503


def test_database_failure_is_visible_not_silent(service, tokens, monkeypatch):
    def unavailable(*args, **kwargs):
        raise DatabaseError()

    monkeypatch.setattr(api, "capture", unavailable)
    outcome = post(service, tokens)
    assert outcome.status == 503
    assert outcome.body == {"error": "database_unavailable"}
    assert (
        service.metrics.counter(
            "blackbox_ingest_failures_total", reason="database_unavailable"
        )
        == 1
    )


def test_service_start_reports_a_corrupt_store_as_not_ready(tmp_path, registry):
    path = tmp_path / "db" / "bb.sqlite3"
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"not a database" * 100)
    path.chmod(0o600)
    svc = Service(path, registry)
    svc.start()
    assert svc.ready_reason is not None
    assert svc.ready().status == 503


# --- reads ----------------------------------------------------------------------


def seed(service, tokens, count=25):
    base = NOW - timedelta(hours=1)
    for i in range(count):
        body = {
            **EVENT,
            "idempotency_key": f"seed-{i}",
            "source": f"svc-{i % 3}",
            "severity": ("info", "warning", "error")[i % 3],
            "correlation_id": f"corr-{i % 5}",
            "occurred_at": (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        assert post(service, tokens, body).status == 201


def test_filters(service, tokens):
    seed(service, tokens)
    events = listing(service, tokens, source="svc-1").body["events"]
    assert {e["source"] for e in events} == {"svc-1"} and len(events) == 8
    assert len(listing(service, tokens, severity="error").body["events"]) == 8
    assert len(listing(service, tokens, correlation_id="corr-2").body["events"]) == 5
    window = listing(
        service,
        tokens,
        time="occurred",
        since="2026-11-24T07:10:00Z",
        until="2026-11-24T07:15:00Z",
    ).body["events"]
    assert [e["occurred_at"][14:16] for e in window] == ["10", "11", "12", "13", "14"]
    assert listing(service, tokens, producer="nobody").body["events"] == []
    for bad in (
        {"limit": "0"},
        {"limit": "501"},
        {"order": "sideways"},
        {"cursor": "garbage"},
        {"since": "yesterday"},
        {"drop": "table"},
    ):
        assert listing(service, tokens, **bad).status == 422


def test_pagination_is_complete_and_stable(service, tokens):
    seed(service, tokens)
    for order in ("asc", "desc"):
        seen, cursor = [], None
        while True:
            parameters = {"limit": "7", "order": order}
            if cursor:
                parameters["cursor"] = cursor
            page = listing(service, tokens, **parameters).body
            seen += [e["event_id"] for e in page["events"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 25
        everything = listing(service, tokens, limit="100", order=order).body["events"]
        assert seen == [e["event_id"] for e in everything]


def test_principals_read_only_what_they_are_allowed(service, tokens):
    mine = post(service, tokens).body["event_id"]
    post(service, tokens, {**EVENT, "idempotency_key": "x"}, who="other")
    own = listing(service, tokens, who="forge", uid=WRITER_UID).body["events"]
    assert {e["producer"] for e in own} == {"forge"}
    assert len(listing(service, tokens).body["events"]) == 2  # reader: "*"
    hidden = call(
        service,
        tokens,
        "other",
        WRITER_UID,
        "GET",
        "/api/v1/events/{event_id}",
        event_id=mine,
    )
    assert hidden.status == 404
    shown = call(
        service,
        tokens,
        "reader",
        READER_UID,
        "GET",
        "/api/v1/events/{event_id}",
        event_id=mine,
    )
    assert shown.status == 200 and shown.body["event_id"] == mine
    assert shown.body["verification"] == "unverified"


def test_integrity_and_chain_head_endpoints(service, tokens):
    post(service, tokens)
    head = call(service, tokens, "reader", READER_UID, "GET", "/api/v1/chain-head")
    assert head.status == 200 and head.body["sequence"] > 0
    checked = call(
        service,
        tokens,
        "reader",
        READER_UID,
        "GET",
        "/api/v1/integrity",
        parameters={
            "sequence": str(head.body["sequence"]),
            "digest": head.body["digest"],
        },
    )
    assert checked.status == 200 and checked.body["ok"]
    wrong = call(
        service,
        tokens,
        "reader",
        READER_UID,
        "GET",
        "/api/v1/integrity",
        parameters={"sequence": str(head.body["sequence"]), "digest": "0" * 64},
    )
    assert wrong.body["ok"] is False and "anchor_mismatch" in wrong.body["errors"]


def test_status_endpoints_need_no_credentials(service):
    for route in ("/api/v1/health", "/api/v1/ready", "/api/v1/version"):
        assert service.handle("GET", route, token=None, uid=None).status == 200
    version = service.handle("GET", "/api/v1/version", token=None, uid=None).body
    assert version["schema_version"] == 5 and version["version"] == bb.__version__


# --- host reports and Git -------------------------------------------------------


HOST = {
    "request_id": "session-1:PostToolUse:toolu_01",
    "producer": "claude-code",
    "observations": [
        {
            "source": "claude-code",
            "kind": "command",
            "name": "PostToolUse:Bash",
            "content_digest": "b" * 64,
        }
    ],
}


def test_host_reports_get_host_authority_and_a_bound_producer(service, tokens):
    outcome = call(
        service,
        tokens,
        "hooks",
        HOOK_UID,
        "POST",
        "/api/v1/host-reports",
        body={"capture": HOST},
    )
    assert outcome.status == 201 and outcome.body["git"] == "not_requested"
    view = bb.get_session(service.database, outcome.body["session_id"])
    assert view.session.producer == "hooks"
    assert view.sources[0].authority == "host_reported"
    assert [o.id for o in view.observations] == outcome.body["event_ids"]
    # The event-write scope cannot reach host authority.
    denied = call(
        service,
        tokens,
        "forge",
        WRITER_UID,
        "POST",
        "/api/v1/host-reports",
        body={"capture": HOST},
    )
    assert denied.status == 403


def test_git_snapshots_only_for_allowlisted_repositories(tmp_path, registry, tokens):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "c",
        ],
        check=True,
    )
    svc = Service(tmp_path / "db" / "bb.sqlite3", registry, repositories=[repo])
    sandbox_before = os.environ.get("BLACKBOX_GIT_SANDBOX")
    try:
        svc.start()
        assert os.environ["BLACKBOX_GIT_SANDBOX"] == "required"
        outside = call(
            svc,
            tokens,
            "hooks",
            HOOK_UID,
            "POST",
            "/api/v1/host-reports",
            body={"capture": HOST, "git_cwd": str(tmp_path)},
        )
        assert outside.body["git"] == "not_allowlisted"
        inside = call(
            svc,
            tokens,
            "hooks",
            HOOK_UID,
            "POST",
            "/api/v1/host-reports",
            body={"capture": {**HOST, "request_id": "s2"}, "git_cwd": str(repo)},
        )
        # Fail closed: without bubblewrap the snapshot fails instead of running
        # unsandboxed; with it, the snapshot is recorded.
        expected = "recorded" if shutil.which("bwrap") else "failed"
        assert inside.body["git"] == expected
    finally:
        if sandbox_before is None:
            os.environ.pop("BLACKBOX_GIT_SANDBOX", None)
        else:
            os.environ["BLACKBOX_GIT_SANDBOX"] = sandbox_before


def test_metrics_render_without_caller_text(service, tokens):
    post(service, tokens)
    post(service, tokens, {**EVENT, "type": "token=leak"})
    text = call(service, tokens, "reader", READER_UID, "GET", "/metrics").body
    assert 'blackbox_events_ingested_total{principal="forge"} 1' in text
    assert "leak" not in text
    assert "blackbox_db_bytes" in text and "blackbox_chain_head_sequence" in text


def test_unknown_routes_and_methods_are_404(service, tokens):
    assert service.handle("GET", "/admin", token=None, uid=None).status == 404
    assert call(service, tokens, "forge", WRITER_UID, "DELETE", "/nope").status == 404


def test_unauthorized_error_carries_nothing(registry):
    with pytest.raises(Unauthorized):
        registry.authenticate("x" * 300, WRITER_UID)

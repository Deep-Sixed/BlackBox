import hashlib
import json
import subprocess
import sys

import pytest

import blackbox as bb
from blackbox.api import _capture_host_report

SESSION = "0f8e2c1a-5b6d-4e7f-8a9b-0c1d2e3f4a5b"


@pytest.fixture
def database(tmp_path):
    return tmp_path / "runtime" / "blackbox.sqlite3"


def hook(database, payload, *args, cwd=None):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return subprocess.run(
        [sys.executable, "-m", "blackbox.cli", "--database", str(database)]
        + ["hook", *args],
        input=raw,
        capture_output=True,
        check=False,
        cwd=cwd,
    )


def tool_event(event, tool="Bash", tool_use_id="toolu_01AbCdEf", **extra):
    return {
        "session_id": SESSION,
        "transcript_path": "/nonexistent/transcript.jsonl",
        "cwd": "/nonexistent",
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": {"command": "echo marker-7f3a", "password": "hunter2-value"},
        "tool_use_id": tool_use_id,
        **extra,
    }


def sessions(database):
    return {
        view.session.request_id: view
        for view in (
            bb.get_session(database, event.session_id)
            for event in bb.get_timeline(database)
            if event.kind == "COMMITTED"
        )
    }


def test_tool_event_records_only_metadata_and_payload_digest(database):
    payload = tool_event("PostToolUse", tool_response={"stdout": "marker-7f3a"})
    raw = json.dumps(payload).encode()
    result = hook(database, raw)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    (view,) = sessions(database).values()
    assert view.session.request_id == f"{SESSION}:PostToolUse:toolu_01AbCdEf"
    assert view.session.producer == "claude-code"
    (observation,) = view.observations
    assert observation.data.model_dump() == {
        "source": "claude-code",
        "kind": "command",
        "name": "PostToolUse:Bash",
        "exit_code": None,
        "duration_ms": None,
        "content_digest": hashlib.sha256(raw).hexdigest(),
    }
    # The host reported it; BlackBox did not witness the tool run.
    assert [s.authority for s in view.sources] == ["host_reported"]
    assert [e.verification for e in view.evidence] == ["unverified"]
    stored = b"".join(p.read_bytes() for p in database.parent.iterdir())
    assert b"marker-7f3a" not in stored and b"hunter2-value" not in stored
    assert bb.check_integrity(database).ok


def test_redelivered_tool_event_is_a_duplicate(database):
    payload = tool_event("PreToolUse", tool="Read")
    assert hook(database, payload).returncode == 0
    assert hook(database, payload).returncode == 0
    (view,) = sessions(database).values()
    assert view.observations[0].data.kind == "activity"
    assert view.observations[0].data.name == "PreToolUse:Read"


def test_pre_and_post_of_one_tool_call_are_separate_records(database):
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        assert hook(database, tool_event(event)).returncode == 0
    assert sorted(sessions(database)) == [
        f"{SESSION}:{event}:toolu_01AbCdEf"
        for event in ("PostToolUse", "PostToolUseFailure", "PreToolUse")
    ]


def test_events_without_host_ids_are_never_merged(database):
    stop = {"session_id": SESSION, "hook_event_name": "Stop", "cwd": "/"}
    prompt = {**stop, "hook_event_name": "UserPromptSubmit", "prompt": "marker-7f3a"}
    for payload in (stop, stop, prompt):
        result = hook(database, payload)
        # Stdout of UserPromptSubmit would be injected into the agent's context.
        assert (result.returncode, result.stdout) == (0, b"")
    names = sorted(v.observations[0].data.name for v in sessions(database).values())
    assert names == ["Stop", "Stop", "UserPromptSubmit"]


def test_git_snapshot_at_turn_end_is_locally_observed(database, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@t.invalid"]
        + ["commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    (repo / "new.txt").write_text("x")
    stop = {"session_id": SESSION, "hook_event_name": "Stop", "cwd": str(repo)}
    assert hook(database, stop, "--git").returncode == 0
    # Tool events are not snapshotted, even with --git.
    assert hook(database, tool_event("PostToolUse"), "--git").returncode == 0
    views = sessions(database)
    (git_id,) = [key for key in views if key.endswith(":git")]
    assert views.pop(git_id[: -len(":git")]).observations[0].data.name == "Stop"
    (source,) = views[git_id].sources
    assert source.authority == "local_git"
    assert views[git_id].observations[0].data.untracked_files == ("new.txt",)
    assert views[git_id].evidence[0].verification == "locally_observed"


def test_git_snapshot_from_a_subdirectory_covers_the_repository(database, tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@t.invalid"]
        + ["commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    (repo / "top.txt").write_text("x")
    (repo / "sub" / "new.txt").write_text("x")
    cwd = str(repo / "sub")
    stop = {"session_id": SESSION, "hook_event_name": "Stop", "cwd": cwd}
    assert hook(database, stop, "--git").returncode == 0
    (snapshot,) = [v for k, v in sessions(database).items() if k.endswith(":git")]
    data = snapshot.observations[0].data
    assert data.untracked_files == ("sub/new.txt", "top.txt")


def test_failed_git_snapshot_keeps_the_event_and_does_not_block(database, tmp_path):
    stop = {"session_id": SESSION, "hook_event_name": "Stop", "cwd": str(tmp_path)}
    result = hook(database, stop, "--git")
    assert (result.returncode, result.stdout) == (1, b"")
    assert json.loads(result.stderr) == {
        "error": "observation_failed",
        "retryable": True,
    }
    (view,) = sessions(database).values()
    assert view.observations[0].data.name == "Stop"
    assert {e.kind for e in bb.get_timeline(database)} >= {"FAILED_RETRYABLE"}


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b"\xff\xfe",
        b"[" * 100_000,
        json.dumps({"hook_event_name": "Stop"}).encode(),
        json.dumps(tool_event("PostToolUse", tool="")).encode(),
        json.dumps(tool_event("PostToolUse", tool="bad name!")).encode(),
        json.dumps(tool_event("PostToolUse", tool="token=abc")).encode(),
    ],
)
def test_invalid_payloads_fail_without_blocking_or_leaking(database, payload):
    result = hook(database, payload)
    assert (result.returncode, result.stdout) == (1, b"")
    assert json.loads(result.stderr) == {"error": "invalid_input", "retryable": False}
    assert b"abc" not in result.stderr


def test_hook_usage_errors_never_use_the_blocking_exit_code(database):
    result = hook(database, tool_event("PreToolUse"), "--unknown-flag")
    assert result.returncode == 1
    result = subprocess.run(
        [sys.executable, "-m", "blackbox.cli", "hook"],
        input=b"{}",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1


def test_host_reported_authority_comes_only_from_the_hook_path(database):
    observation = {"source": "claude-code", "kind": "activity", "name": "Stop"}
    request = {"request_id": "r1", "producer": "claude-code"}
    request["observations"] = [observation]
    caller = bb.capture(database, request)
    # Same request ID through the host path: a conflict, never a silent duplicate.
    with pytest.raises(bb.ConflictError):
        _capture_host_report(database, request)
    host = _capture_host_report(database, {**request, "request_id": "r2"})
    authorities = [
        bb.get_session(database, key).sources[0].authority
        for key in (caller.session_id, host.session_id)
    ]
    assert authorities == ["caller_asserted", "host_reported"]
    # Retrying the host report stays a duplicate of the host record.
    assert _capture_host_report(database, {**request, "request_id": "r2"}).duplicate
    assert bb.check_integrity(database).ok


@pytest.mark.parametrize(
    "extra",
    [
        {"claims": [{"source": "claude-code", "topic": "t", "statement": "s"}]},
        {"artifacts": [{"source": "claude-code", "path": "p", "digest": "0" * 64}]},
    ],
)
def test_host_reports_cannot_carry_claims_or_artifacts(database, extra):
    request = {"request_id": "r1", "producer": "claude-code", **extra}
    with pytest.raises(bb.ValidationError):
        _capture_host_report(database, request)
    assert not database.exists()

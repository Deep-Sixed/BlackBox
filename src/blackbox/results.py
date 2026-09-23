"""Detached, frozen public projections. They never grant write access to storage."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InitializationResult(_Result):
    status: Literal["initialized"] = "initialized"
    schema_version: int


class CaptureResult(_Result):
    session_id: str
    status: Literal["COMMITTED"]
    duplicate: bool


class ClaimResult(_Result):
    claim_id: str


class SessionRecord(_Result):
    id: str
    request_id: str
    fingerprint: str
    producer: str
    created_at: str


class SourceRecord(_Result):
    id: str
    session_id: str
    identity: str
    authority: Literal["caller_asserted", "local_git"]


class CallerObservation(_Result):
    source: str
    kind: Literal["command", "test", "activity"]
    name: str
    exit_code: int | None
    duration_ms: int | None
    content_digest: str | None


class GitObservation(_Result):
    commit_before: str | None
    commit_after: str
    branch: str
    committed_delta: tuple[str, ...] | None
    staged_delta: tuple[str, ...]
    unstaged_delta: tuple[str, ...]
    working_tree_delta: tuple[str, ...]
    untracked_files: tuple[str, ...]


class ObservationRecord(_Result):
    id: str
    session_id: str
    source_id: str
    kind: str
    data: CallerObservation | GitObservation
    recorded_at: str


class EvidenceRecord(_Result):
    id: str
    observation_id: str
    digest: str
    verification: Literal["unverified", "locally_observed"]


class ClaimRecord(_Result):
    id: str
    session_id: str
    source_id: str
    topic: str
    statement: str
    target_id: str | None
    relation: Literal["supersedes", "contests", "retracts"] | None


class ClaimView(ClaimRecord):
    status: Literal["active", "superseded", "contested", "retracted"]


class ArtifactRecord(_Result):
    id: str
    session_id: str
    source_id: str
    path: str
    digest: str
    verification: Literal["unverified"]


class TimelineEvent(_Result):
    sequence: int
    id: str
    session_id: str
    kind: str
    entity_id: str
    recorded_at: str


class FailureRecord(_Result):
    id: str
    session_id: str
    event_id: str
    code: str
    retryable: Literal[0, 1]


class SessionView(_Result):
    session: SessionRecord
    status: Literal["RESERVED", "COMMITTED", "FAILED_RETRYABLE"]
    sources: tuple[SourceRecord, ...]
    observations: tuple[ObservationRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    claims: tuple[ClaimRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]
    failures: tuple[FailureRecord, ...]
    events: tuple[TimelineEvent, ...]


class IntegrityResult(_Result):
    ok: bool
    schema_version: int | None
    errors: tuple[str, ...]


class EvidenceLinkResult(_Result):
    link_id: str


class EvidenceLinkView(_Result):
    id: str
    claim_id: str
    evidence_record_id: str
    record_type: Literal["observation", "evidence", "artifact"]
    relation: Literal["supports", "contradicts", "context"]
    source_id: str
    origin_session_id: str
    recorded_at: str
    sequence: int


class ClaimRelationView(_Result):
    id: str
    claim_id: str
    target_id: str
    relation: Literal["supersedes", "contests", "retracts"]
    source_id: str
    origin_session_id: str
    target_session_id: str
    recorded_at: str
    sequence: int

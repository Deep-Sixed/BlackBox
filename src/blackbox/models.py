"""Strict metadata-only input boundary. No arbitrary payload dictionaries."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=2048)]

# Defense in depth after shape validation. Never emit the rejected value.
SUSPICIOUS = re.compile(
    r"(?i)(bearer\s+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"[a-z][a-z0-9+.-]*://|\b(?:password|passwd|token|api[_-]?key|"
    r"authorization|cookie|secret)\s*[:=]|\bsk-[\w-]{12,}|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bAKIA[A-Z0-9]{16})"
)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def identity(kind: str, value: object) -> str:
    return kind + "_" + hashlib.sha256(canonical(value).encode()).hexdigest()


def safe_strings(value: object) -> None:
    if isinstance(value, str) and SUSPICIOUS.search(value):
        raise ValueError("sensitive or connection-shaped content is forbidden")
    if isinstance(value, dict):
        for key, item in value.items():
            safe_strings(key)
            safe_strings(item)
    if isinstance(value, (tuple, list)):
        for item in value:
            safe_strings(item)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @model_validator(mode="after")
    def reject_sensitive_content(self):
        safe_strings(self.model_dump(mode="json"))
        return self


class Observation(StrictModel):
    source: Identifier
    kind: Literal["command", "test", "activity"]
    name: Identifier
    exit_code: int | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    content_digest: Digest | None = None


class Claim(StrictModel):
    topic: Identifier
    statement: Text
    source: Identifier


class Artifact(StrictModel):
    path: Text
    digest: Digest
    source: Identifier


class Capture(StrictModel):
    request_id: Identifier
    producer: Identifier
    observations: Annotated[list[Observation], Field(max_length=1000)] = Field(
        default_factory=list
    )
    claims: Annotated[list[Claim], Field(max_length=1000)] = Field(default_factory=list)
    artifacts: Annotated[list[Artifact], Field(max_length=1000)] = Field(
        default_factory=list
    )


class EvidenceLink(StrictModel):
    source: Identifier
    claim_id: Identifier
    record_type: Literal["observation", "evidence", "artifact"]
    evidence_record_id: Identifier
    relation: Literal["supports", "contradicts", "context"]

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


CONFIDENCE_LEVELS = frozenset({"high", "medium", "low", "contested"})
STATUS_VALUES = frozenset({"active", "superseded", "contested", "retracted"})
CLAIM_TYPES = frozenset({"fact", "event", "definition", "claim", "metric", "relation"})


@dataclass(frozen=True)
class SourceRef:
    ref: str
    quote: str
    locator: str | None = None
    source_type: str | None = None
    source_hash: str | None = None


@dataclass
class ClaimRecord:
    id: str
    statement: str
    topic: str
    type: str
    sources: list[SourceRef]
    confidence: str
    status: str
    created: str
    updated: str
    valid_from: date | None = None
    valid_until: date | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    contradicts: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    note: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    source_hash: str | None = None
    source_file: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def valid_on(self, as_of: date) -> bool:
        if self.valid_from is not None and as_of < self.valid_from:
            return False
        if self.valid_until is not None and as_of >= self.valid_until:
            return False
        return True

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from ledger.models import (
    CLAIM_TYPES,
    CONFIDENCE_LEVELS,
    STATUS_VALUES,
    ClaimRecord,
)

# The generator is authoritative: ``repository._prepare_claim`` mints
# ``clm-<UTC year>-<8 hex>``. The older ``clm-YYYY-NNNN`` sequence form this
# check used to require was never minted by any writer, so every stored claim
# failed it. Hand-authored ids (slugs) still warn — that is real signal.
CLAIM_ID_PATTERN = re.compile(r"^clm-\d{4}-[0-9a-f]{8}$")
CLAIM_ID_SHAPE = "clm-YYYY-<8 hex>"


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ``file`` was used by pre-EVECOR claims without a stable evidence-root
# contract. New MCP writes use ``repo-file`` and must resolve at audit time.
_REPO_FILE_SOURCE_TYPES = frozenset({"repo-file"})
_LEGACY_SOURCE_RELOCATIONS = {
    "../.stignore": ".stignore",
    # 2026-07-15 AgentSync→governance move + earlier legacy script retirement
    "AI_SYNC_LEDGER.md": "governance/flight-recorder/storage/archive/AI_SYNC_LEDGER.md",
    "EVECOR/scripts/keepass_batch.py": "EVECOR/bin/keepass_batch.py",
    "scripts/jarvis-secret": "EVECOR/bin/jarvis-secret",
    # claim-file ref recorded against the flat standalone store; the date-first
    # archive shelves the cited claim under its recording day
    "governance/ledger/storage/ledger/claims/clm-2026-bc11d6f0.md": "governance/flight-recorder/storage/2026/07/16/ledger/clm-2026-bc11d6f0.md",
    # target-architecture doc moved from governance/ into the archive 2026-07-17
    "governance/FLIGHT-RECORDER-TARGET.md": "governance/flight-recorder/storage/FLIGHT-RECORDER-TARGET.md",
}
_LEGACY_SOURCE_PREFIX_RELOCATIONS = {
    "ops/references/litellm-local/": "gateway/litellm/local/",
    # ledger source moved out of AgentSync/src on 2026-07-16, then into the
    # flight-recorder software group the same day
    "src/ledger/": "governance/flight-recorder/ledger/",
    "governance/ledger/src/ledger/": "governance/flight-recorder/ledger/",
    "governance/ledger/": "governance/flight-recorder/ledger/",
    "governance/recon/src/recon/": "governance/flight-recorder/recon/",
    "governance/recon/": "governance/flight-recorder/recon/",
    "governance/recall/storage/recall/archive/": "governance/flight-recorder/storage/archive/",
    "governance/recall/": "governance/flight-recorder/recall/",
    # EVECOR f059d60a (2026-07-18) "relocate recovery assets to component
    # owners" moved the per-batch Docker recovery packages out of the shared
    # ops/ tree and under the component that owns each container.
    "ops/recovery/docker-environment/2026-07-17/batch-002-contextforge-agentpool/": "gateway/acp-servers/agentpool/recovery/2026-07-17/",
    "ops/recovery/docker-environment/2026-07-17/batch-003-contextforge-a2a-echo-agent/": "gateway/agents2agents/a2a-echo-agent/recovery/2026-07-17/",
    "ops/recovery/docker-environment/2026-07-17/batch-004-graphify-core-db/": "data/graphify/recovery/2026-07-17/",
}


def _canonical_source_ref(ref: str) -> str:
    """Map the few known EVECOR-layout moves without rewriting claim history."""
    if ref in _LEGACY_SOURCE_RELOCATIONS:
        return _LEGACY_SOURCE_RELOCATIONS[ref]
    for old_prefix, new_prefix in _LEGACY_SOURCE_PREFIX_RELOCATIONS.items():
        if ref.startswith(old_prefix):
            return f"{new_prefix}{ref.removeprefix(old_prefix)}"
    return ref


def _evidence_roots(repo: Path) -> tuple[Path, ...]:
    """Return the roots that can contain checked-in evidence.

    The claim store owns the repo root, while existing claims can cite the
    surrounding EVECOR checkout (for example ``gateway/...``) or its parent
    projects tree (for example ``third-party/...``). Historically the store
    lived at ``<projects>/EVECOR/AgentSync``; since 2026-07-16 it lives under
    the flight-recorder archive at
    ``<projects>/EVECOR/governance/flight-recorder/storage``. All layouts
    resolve to the same evidence roots: the store itself, the enclosing
    EVECOR checkout, and the projects tree above it.
    """
    resolved = repo.resolve()
    roots = [resolved]
    if repo.name == "AgentSync" and repo.parent.is_dir():
        roots.append(repo.parent.resolve())
        if repo.parent.parent.is_dir():
            roots.append(repo.parent.parent.resolve())
    else:
        for ancestor in resolved.parents:
            if ancestor.name == "EVECOR":
                roots.append(ancestor)
                if ancestor.parent.is_dir():
                    roots.append(ancestor.parent)
                break
    return tuple(roots)


def _normalized_repo_ref(ref: str) -> str | None:
    """Collapse ``.``/``..`` segments in a repo-relative ref.

    Returns ``None`` only for the two shapes that genuinely leave the evidence
    roots: an absolute path, or a ref that still climbs above the root once
    normalized. An interior ``..`` that lands back inside the tree — for
    example ``EVECOR/../labs/x`` written by a claim author standing in the
    EVECOR checkout — is a verbose spelling of a valid ref, not a traversal
    attempt, and must not be rejected lexically. Containment is still enforced
    per-root by ``_source_exists``.
    """
    if not ref or ref.startswith("/"):
        return None
    normalized = posixpath.normpath(ref)
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        return None
    return normalized


def _source_exists(repo: Path, ref: str) -> bool:
    normalized = _normalized_repo_ref(_canonical_source_ref(ref))
    if normalized is None:
        return False
    path = Path(normalized)

    for root in _evidence_roots(repo):
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            return True
    return False


def validate_claims(repo: Path, claims: list[ClaimRecord]) -> ValidationReport:
    report = ValidationReport()
    by_id: dict[str, ClaimRecord] = {}

    for claim in claims:
        prefix = f"{claim.id or '<missing-id>'}: "

        if not claim.id:
            report.errors.append("claim missing id")
            continue

        if claim.id in by_id:
            report.errors.append(f"{prefix}duplicate id")
        by_id[claim.id] = claim

        if not CLAIM_ID_PATTERN.match(claim.id):
            report.warnings.append(f"{prefix}id does not match {CLAIM_ID_SHAPE}")

        if not claim.statement:
            report.errors.append(f"{prefix}missing statement")

        if not claim.topic:
            report.errors.append(f"{prefix}missing topic")

        if claim.type not in CLAIM_TYPES:
            report.errors.append(f"{prefix}invalid type '{claim.type}'")

        if claim.confidence not in CONFIDENCE_LEVELS:
            report.errors.append(f"{prefix}invalid confidence '{claim.confidence}'")

        if claim.status not in STATUS_VALUES:
            report.errors.append(f"{prefix}invalid status '{claim.status}'")

        if not claim.sources:
            report.errors.append(f"{prefix}requires at least one source")

        # Claims are append-only: a superseded/retracted/contested claim's sources are a
        # historical record, not live-relied-upon evidence, and can never be edited to fix
        # a bad ref without violating that guarantee. Only active claims fail closed here.
        sources_are_live_evidence = claim.status == "active"

        for source in claim.sources:
            if not source.ref or not source.quote:
                report.errors.append(f"{prefix}source missing ref or quote")
                continue
            canonical_ref = _canonical_source_ref(source.ref)
            if _normalized_repo_ref(canonical_ref) is None:
                if source.source_type in _REPO_FILE_SOURCE_TYPES and sources_are_live_evidence:
                    report.errors.append(f"{prefix}source ref escapes repo: {source.ref}")
                else:
                    report.warnings.append(
                        f"{prefix}legacy external source ref is not verified: {source.ref}"
                    )
            elif not _source_exists(repo, source.ref):
                if source.source_type in _REPO_FILE_SOURCE_TYPES and sources_are_live_evidence:
                    report.errors.append(f"{prefix}source ref not found: {source.ref}")
                else:
                    report.warnings.append(
                        f"{prefix}legacy unresolved source ref is not verified: {source.ref}"
                    )
            if source.source_hash and not source.source_hash.startswith("sha256:"):
                report.errors.append(f"{prefix}source hash must use sha256: prefix")

        if claim.source_ref:
            if _normalized_repo_ref(claim.source_ref) is None:
                report.errors.append(f"{prefix}source_ref escapes repo: {claim.source_ref}")
            if claim.source_type and not claim.source_type.strip():
                report.errors.append(f"{prefix}source_type cannot be blank when source_ref is set")
            if claim.source_hash and not claim.source_hash.startswith("sha256:"):
                report.errors.append(f"{prefix}source_hash must use sha256: prefix")

        if claim.valid_from and claim.valid_until and claim.valid_from > claim.valid_until:
            report.errors.append(f"{prefix}valid_from after valid_until")

        if claim.status == "superseded" and not claim.superseded_by:
            report.warnings.append(f"{prefix}superseded without superseded_by")

        if claim.status == "contested" and claim.confidence != "contested":
            report.errors.append(f"{prefix}contested status requires contested confidence")

        if claim.status == "active" and claim.superseded_by:
            report.errors.append(f"{prefix}active claim cannot have superseded_by")

    for claim in claims:
        prefix = f"{claim.id}: "
        if claim.supersedes:
            other = by_id.get(claim.supersedes)
            if other is None:
                report.errors.append(f"{prefix}supersedes unknown id {claim.supersedes}")
            elif other.superseded_by != claim.id:
                report.errors.append(
                    f"{prefix}supersedes {claim.supersedes} but reciprocal link missing"
                )

        if claim.superseded_by:
            other = by_id.get(claim.superseded_by)
            if other is None:
                report.errors.append(
                    f"{prefix}superseded_by unknown id {claim.superseded_by}"
                )
            elif other.supersedes != claim.id:
                report.errors.append(
                    f"{prefix}superseded_by {claim.superseded_by} but reciprocal link missing"
                )

        for other_id in claim.contradicts:
            if other_id not in by_id:
                report.errors.append(f"{prefix}contradicts unknown id {other_id}")
            else:
                other = by_id[other_id]
                # If we contradict a claim, that claim MUST be marked contested
                if other.status != "contested":
                    report.errors.append(
                        f"{prefix}contradicts {other_id} but target status is '{other.status}', must be 'contested'"
                    )
                if other.confidence != "contested":
                    report.errors.append(
                        f"{prefix}contradicts {other_id} but target confidence is '{other.confidence}', must be 'contested'"
                    )

        # Check for contest_linked inconsistency: if we have relations "disputes:X",
        # target X must be contested
        for relation in claim.relations:
            if relation.startswith("disputes:"):
                target_id = relation.split(":", 1)[1]
                if target_id in by_id:
                    target = by_id[target_id]
                    if target.status != "contested":
                        report.errors.append(
                            f"{prefix}relations includes 'disputes:{target_id}' but target status is '{target.status}', must be 'contested'"
                        )
                    if target.confidence != "contested":
                        report.errors.append(
                            f"{prefix}relations includes 'disputes:{target_id}' but target confidence is '{target.confidence}', must be 'contested'"
                        )

    # Check for supersede inconsistency: if we have supersedes/superseded_by,
# reciprocal links and statuses must be correct
        if claim.supersedes:
            target_id = claim.supersedes
            if target_id in by_id:
                target = by_id[target_id]
                if target.status != "superseded":
                    report.errors.append(
                        f"{prefix}supersedes {target_id} but target status is '{target.status}', must be 'superseded'"
                    )
                if target.superseded_by != claim.id:
                    report.errors.append(
                        f"{prefix}supersedes {target_id} but target superseded_by is '{target.superseded_by}', must be '{claim.id}'"
                    )
        if claim.superseded_by:
            target_id = claim.superseded_by
            if target_id in by_id:
                target = by_id[target_id]
                if target.status != "active":
                    report.errors.append(
                        f"{prefix}superseded_by {target_id} but target status is '{target.status}', must be 'active'"
                    )
                if target.supersedes != claim.id:
                    report.errors.append(
                        f"{prefix}superseded_by {target_id} but target supersedes is '{target.supersedes}', must be '{claim.id}'"
                    )

    return report

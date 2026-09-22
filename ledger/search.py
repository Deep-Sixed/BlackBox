from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date

from ledger.models import ClaimRecord

TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in TOKEN_PATTERN.finditer(text)]


def claim_document(claim: ClaimRecord) -> str:
    source_text = " ".join(f"{source.ref} {source.quote}" for source in claim.sources)
    return " ".join(
        [
            claim.id,
            claim.topic,
            claim.type,
            claim.status,
            claim.confidence,
            claim.statement,
            source_text,
            claim.note or "",
        ]
    )


def score_claim(
    query_tokens: list[str] | set[str],
    claim: ClaimRecord,
    *,
    document_frequencies: dict[str, int] | None = None,
    document_count: int = 1,
    average_length: float | None = None,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    tokens = tokenize(claim_document(claim))
    if not query_tokens or not tokens:
        return 0.0

    query = list(query_tokens)
    frequencies = Counter(tokens)
    avg_len = average_length if average_length and average_length > 0 else len(tokens)
    score = 0.0
    for token in query:
        tf = frequencies.get(token, 0)
        if tf == 0:
            continue
        df = (document_frequencies or {}).get(token, 1)
        idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
        norm = tf + k1 * (1 - b + b * (len(tokens) / avg_len))
        score += idf * ((tf * (k1 + 1)) / norm)
    return score


def search_claims(
    claims: list[ClaimRecord],
    query: str,
    *,
    as_of: date | None = None,
    limit: int = 20,
) -> list[tuple[float, ClaimRecord]]:
    query_tokens = tokenize(query)
    candidates = [claim for claim in claims if as_of is None or claim.valid_on(as_of)]
    documents = {claim.id: tokenize(claim_document(claim)) for claim in candidates}
    document_count = len(candidates) or 1
    average_length = sum(len(tokens) for tokens in documents.values()) / document_count
    dfs: dict[str, int] = {}
    for tokens in documents.values():
        for token in set(tokens):
            dfs[token] = dfs.get(token, 0) + 1

    scored: list[tuple[float, ClaimRecord]] = []
    for claim in candidates:
        score = score_claim(
            query_tokens,
            claim,
            document_frequencies=dfs,
            document_count=document_count,
            average_length=average_length,
        )
        if score > 0:
            scored.append((score, claim))

    scored.sort(key=lambda item: (-item[0], item[1].id))
    return scored[:limit]

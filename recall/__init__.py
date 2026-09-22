"""Recall — human/handoff view over the EVECOR memory stores.

Recall does not capture anything. Recon (session flight-recorder) and the Ledger
(verifiable claims + passive receipts) already capture; Recall *composes* what they
hold into human-readable daily digests so an operator — or an incoming agent picking
up a colleague's work — can read back the last N days in one place.
"""

from recall import compose, sources

__all__ = ["compose", "sources"]

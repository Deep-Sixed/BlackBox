# Evidence model

BlackBox records bounded metadata so later readers can reconstruct what was
claimed, observed, and referenced without upgrading source authority by accident.

## Terms

- Claim: a caller-asserted statement about a topic.
- Observation: a bounded event or activity record, either caller asserted or
  locally observed by a built-in BlackBox observer.
- Receipt: a deterministic digest over explicit canonical record material;
  observation evidence retains its original receipt and v2 adds a record chain.
- Integrity: whether canonical records still match their recorded receipts and SQLite
  constraints.
- Authority: who supplied the record: `caller_asserted` (the caller, which may
  be the acting agent), `host_reported` (the agent's host runtime, through
  [lifecycle hooks](hooks.md)) or `local_git` (BlackBox's own Git observer).
- Verification: what BlackBox can say about an observation, currently
  `unverified` or `locally_observed`.
- Provenance: the metadata needed to explain where a record came from and how it
  relates to repository state.

## Invariant

Hash integrity is not observer authority.

A matching receipt establishes consistency with locally recorded canonical
material, subject to the [threat model](threat-model.md). It does not prove that the caller's statement is true, that a
remote system authenticated it, or that downstream analysis verified it.

Likewise, a local observer marks only the specific metadata it observed. A caller
cannot obtain `local_git` authority by naming its source `blackbox.git`; caller
input remains `caller_asserted` and its evidence remains `unverified`. In the
same way, only the hook code path records `host_reported`, and a host report is
still `unverified`: the host saw the action, BlackBox did not.

Derived views such as timelines, active claim status, reconstruction, and
integrity checks are interpretations over immutable rows. They do not create new
source records and do not change authority.

A link saying evidence `supports`, `contradicts` or provides `context` for a claim
is itself a caller-attributed assertion. BlackBox persists the link, source and
origin session without judging the claim. A locally observed evidence target
does not promote the linking source to observer authority. Cross-session
supersede/contest/retract claims keep both origins and derive status without
mutating originals. See [trace relationships](trace-relationships.md).

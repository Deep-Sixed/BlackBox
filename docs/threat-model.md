# Threat model

This page collects what BlackBox defends against, what it deliberately does not,
and what a production deployment has to add. The details live in the
[SQLite contract](sqlite-contract.md), the [evidence model](evidence-model.md),
the [architecture](architecture.md) and [trace relationships](trace-relationships.md);
if this page and those disagree, treat it as a documentation bug.

BlackBox records and proves; it does not decide. It is local evidence
infrastructure, not a gate: nothing here stops an agent from acting.

## Trust boundary

The boundary is the local operating-system user that runs BlackBox and owns the
database file. BlackBox protects the record against callers of its API and
against ordinary SQL writes. It does not protect the record from that user, from
code running as that user, or from anyone else with write access to the file,
beyond making tampering *detectable*, and then only in the ways listed below.

## Defended

| Threat | Mechanism | Detected as |
| --- | --- | --- |
| Editing or deleting history through ordinary SQL | `BEFORE UPDATE/DELETE` triggers on every table abort the write | write fails with `immutable history` |
| Editing a stored record with the triggers bypassed, without recomputing receipts | Every record has a receipt in an append-only SHA-256 chain; observation evidence keeps its own receipt | `record_integrity`, `receipt_integrity`; `first_broken_sequence` names the first failing receipt |
| Removing, reordering or duplicating records or receipts | Receipt sequences are contiguous and each links to the previous digest; every record needs exactly one receipt | `sequence_continuity`, `chain_integrity`, `record_coverage`, `orphan_receipt`, `duplicate_receipt` |
| Rewriting history and recomputing every receipt, or rolling the file back to an older consistent copy, **before the latest anchor** | The operator exports `get_chain_head` and keeps it outside the writer's control; `check_integrity(anchor=...)` compares it | `anchor_mismatch`, `anchor_missing` |
| Forged relationships: claims, claim relations or evidence links whose IDs don't match their content, or with wrong attribution or order | Claim, relation and evidence-link IDs are recomputed from content; all three are checked against their events and sources, and a link must follow both its claim and its evidence record in event order | `relationship_integrity` |
| Changing the schema, such as dropping a trigger or adding a table | Actual schema objects are compared with the frozen definitions; unknown application IDs or versions are refused | `SchemaError` / `schema_integrity` |
| Adopting or overwriting a non-BlackBox database | Writers refuse a database with foreign tables or identity | `SchemaError` |
| Reusing a request ID for different input | The session fingerprint binds the ID to its input | `ConflictError`; nothing is overwritten |
| A caller claiming observer or host authority, for example by naming its source `blackbox.git` | Authority comes from the code path, not the name: `capture` input is always `caller_asserted`/`unverified`; only `blackbox hook` records `host_reported` | cannot be expressed through `capture` |
| An observed repository running a `core.fsmonitor` hook, or redirecting the observer through inherited `GIT_*` variables | Git runs with `-c core.fsmonitor=false` and a scrubbed environment | not executed |
| An observed repository substituting commits through `refs/replace/` (`git replace`), so a replaced baseline or `HEAD` hides committed or staged changes | Git runs with `--no-replace-objects`, so diffs always use the named commits' real trees | changes reported |
| An observed repository hiding a submodule commit change through `diff.ignoreSubmodules` or a `.gitmodules` `ignore` setting | Committed and staged deltas compare raw commit-tree and index mode/object entries instead of using Git's diff policy | changes reported |
| An observed repository hiding an executable-bit change through `core.fileMode=false` | The setting is not read: the raw executable bit is always compared with the index mode | change reported |
| An observed repository hiding a new file through `core.ignoreCase`, so a name that differs only in case from a tracked path or an ignore rule is treated as tracked or ignored | Git runs with `-c core.ignoreCase=false`, so names and ignore rules match case-sensitively; on a case-insensitive filesystem a case-only rename may be over-reported as untracked | file reported |
| An observed repository hiding a new file through `.git/info/exclude` or `core.excludesFile` | Only `.gitignore` files in the working tree are applied, and edits to those are themselves reported; an untracked `.gitignore` is always listed | file, or the `.gitignore` hiding it, reported |
| An observed repository running a clean filter, or hiding edits through a filter or an `assume-unchanged` flag | Git never reads the working tree for the observer: unstaged changes compare raw working-tree bytes with index blob IDs, ignoring `assume-unchanged` | not executed; edit reported |
| An observed repository pointing `core.worktree` at a pristine decoy copy, so Git reads the decoy instead of the edited tree | The observer finds the working tree by the nearest `.git` and passes it as `--work-tree` to every Git command, which outranks repository config | edits and untracked files in the real tree reported |
| An observed repository hiding edits behind a `skip-worktree` flag | The flag is honoured only for files absent from disk (sparse checkout); a present file is hashed like any other | edit reported |
| An observed repository steering the observer's reads, by replacing a directory with a symlink out of the repository or a file with a FIFO | Working-tree paths are opened one directory handle at a time with `O_NOFOLLOW` and `O_NONBLOCK`; symlinked parents are never followed, and only regular files are read | path reported deleted or changed; capture does not block |
| An agent skipping or suppressing its own reports | With [host hooks](hooks.md), the host runtime reports each prompt, tool call and turn end; the agent is not involved | `host_reported` records, evidence still `unverified` |
| The recorder blocking or steering the observed agent | `blackbox hook` never exits 2 (the hook "block" code) and never writes to stdout, which some hooks feed back to the agent | cannot block |
| Other local users reading or replacing the database file | Created `0600`; writers refuse wider permissions and symlinks. Writers and readers refuse a database directory other users own or can write, checking every directory and symlink on the path as written (a directory on the way may be shared only with the sticky bit, and then each symlink in it must be the user's own), since directory write access allows replacement or a planted WAL sidecar whatever the file's mode | `DatabaseError` |
| Secrets entering the store or leaking through errors | Inputs and observed Git metadata pass a credential-shape filter; public errors are fixed codes with no input, path or exception text | `ValidationError`, `ObservationRejectedError` |

The credential filter is a pattern heuristic that rejects common token, key and
URL shapes. It is not data-loss prevention and will miss secrets that look like
ordinary text.

## Not defended (on purpose or not yet)

- **Rewrites after the latest anchor, or with no anchor at all.** The chain is
  unauthenticated: it has no signature or MAC. Anyone who can write the file can
  rewrite records and recompute every receipt, and `check_integrity` will report
  `ok`. Anchoring protects history only up to the anchor, and only if the anchor
  is stored where that party cannot change it.
- **The truth of what callers submit.** Claims, caller observations, artifact
  digests and evidence links are recorded attributions. A matching receipt proves
  the record is unchanged, not that its statement is true.
- **Producer identity.** Source and producer names are caller-chosen attribution,
  not authenticated identity. BlackBox has no credentials or signatures for
  producers.
- **Host hook authenticity and completeness.** Any process running as the
  BlackBox user can pipe a forged payload into `blackbox hook`, and a host that
  is misconfigured, or edited by the agent, can stop calling it. Hook records
  prove what was reported, not that the tool ran. An agent that can edit the
  host's hook settings can switch recording off; keep them where it cannot.
- **Code running as the BlackBox user.** Such code can alter the database, the
  installed package, the `git` executable found on `PATH`, or the operator's
  global and system Git configuration. The Git observer trusts all of these.
- **Repository state the agent writes.** The observed agent controls `.git`:
  `HEAD`, branches, the index, objects and any `.git` file or symlink pointing
  elsewhere. A Git observation reports that repository's state as Git reads it;
  it cannot tell honest history from history the agent rewrote. An agent can,
  for example, commit its edits so they leave `staged_delta` and
  `unstaged_delta`, or point `.git` at a different repository. Only a
  `baseline` the caller supplies as a full commit ID is fixed independently of
  the agent, and `committed_delta` is computed against its real tree (replace
  refs are ignored). Treat a Git observation as "what this repository showed
  the observer", not as proof of what the agent did.
- **Other repository configuration.** The Git observer still honours the
  observed repository's config for the commands it runs (`rev-parse`, `branch`,
  `ls-files`, `ls-tree` and `diff-index --cached`). None of these are known to run configured
  programs or read working-tree file contents. A Git feature that does
  would reopen this surface. Full isolation means running the observer in a
  sandbox (see production gaps).
- **A working tree changing during capture.** A snapshot reads HEAD, the index
  and the working tree separately. If HEAD or the index file changes before the
  snapshot finishes, it is refused with a retryable error rather than recorded,
  so a change staged mid-capture cannot fall between the staged and unstaged
  reads. Working-tree files are hashed one at a time, though, and are not
  captured atomically: a file edited while the walk is under way may be
  recorded in either state. Only a filesystem snapshot would close this.
- **Files matched by an unchanged `.gitignore`.** `untracked_files` omits paths
  ignored by a `.gitignore` in the working tree, to keep dependency and build
  directories out of every record. A change to a `.gitignore`, or a new one, is
  itself recorded; but a new file under an already-committed rule (for example
  `scratch/payload` under `scratch/`) leaves no trace in the observation.
- **Deleting a skip-worktree file.** A tracked file marked skip-worktree and
  absent from disk counts as unchanged, because that is exactly what sparse
  checkout does. So an actor that sets the flag and then deletes the file hides
  the deletion. Once committed, the deletion appears in `committed_delta`
  against an earlier baseline.
- **Large-worktree observation cost.** To avoid trusting file timestamps,
  repository filters or index hiding flags, each Git snapshot hashes the raw
  bytes of every present stage-0 tracked regular file. Work is therefore
  proportional to tracked working-tree bytes; on very large or cold repositories,
  `hook --git` can add noticeable turn-end latency. This is the deliberate
  availability/performance cost of the stronger observation rule.
- **Submodule contents.** A submodule counts as changed only when a different
  commit is checked out. Uncommitted edits inside a submodule are not observed,
  because inspecting them would mean running Git in that repository's working
  tree.
- **Wall-clock trust.** `recorded_at` is the local clock at write time. It is
  not attested and can move. `sequence` is local persistence order, not
  causality.
- **Replacement racing the open.** The database path is checked before SQLite
  opens it by name, so a directory on it that changes in between is not
  noticed. The checks leave another local user no directory to change; the
  race matters only if permissions are loosened while BlackBox runs. An external
  anchor still detects a swapped database.
- **Confidentiality.** Records are integrity-checked, not encrypted. Anyone who
  can read the file can read its metadata.
- **Availability and retention.** Deleting the file, filling the disk or
  withholding the database is outside the model. BlackBox has no retention,
  deletion or backup policy.
- **Runtime authorization.** BlackBox does not stop an agent from acting; it
  makes the record of the action checkable.

## Production gaps

| Concern | BlackBox today | A production deployment adds |
| --- | --- | --- |
| External anchoring | `get_chain_head` export and `check_integrity(anchor=...)`; no backend | Scheduled head export to a transparency log, an RFC 3161 timestamp authority or another operator |
| Record authentication | Unauthenticated SHA-256 chain | Signed chain heads with keys held outside the writer's host (HSM/KMS) |
| Producer identity | Caller-asserted names | Authenticated producers, for example signed submissions |
| Storage | One local SQLite file, WAL, `synchronous=FULL` | Backups and write-once storage for exported heads and database copies |
| Time | Local recording clock | Trusted timestamping; bounded clock-skew handling |
| Confidentiality | None beyond file permissions | Encryption at rest; access control on readers |
| Observer isolation | Git runs as the BlackBox user, against the observed repository's own config | Run the observer with a sandboxed, minimal Git configuration |
| Canonical form | [BlackBox canonical JSON v1](canonical-json.md), not RFC 8785 | Independent verifiers implement that spec |
| Platform | POSIX only; CI on Linux | Windows support would need its own ownership and path-safety checks |

None of these change the mechanism. They harden where its assumptions are
enforced.

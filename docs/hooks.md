# Host lifecycle hooks — package 0.6.0

`blackbox hook` records what an agent does without the agent reporting it. The
host runtime that runs the agent, for example Claude Code, runs the command on
its own lifecycle events and pipes each event to it as JSON on stdin. The agent
does not call BlackBox, cannot skip the call, and never sees it.

This is the first observer that is not driven by the agent's own submissions.
It is still not independent observation: the **host** reports the event, and
BlackBox records that report under its own `host_reported` authority. See
[authority](#authority) below.

## Claude Code setup

Add the hooks to `.claude/settings.json` (project) or `~/.claude/settings.json`
(user). Use an absolute database path; hooks run with the session's working
directory.

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook"}]}],
    "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook"}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook"}]}],
    "PostToolUseFailure": [{"matcher": "*", "hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook --git"}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook --git"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "blackbox --database /abs/path/blackbox.sqlite3 hook --git"}]}]
  }
}
```

Keep only the events you want recorded. Any other hook event is accepted and
recorded by its event name.

Options:

- `--producer NAME` sets the session producer and observation source (default
  `claude-code`). It is attribution, not authenticated identity.
- `--git` also takes an independent Git snapshot of the payload's `cwd` after
  `Stop`, `SubagentStop` and `SessionEnd`. It is ignored for other events.

## What is recorded

Each event becomes one capture session holding one observation:

| Field | Value |
| --- | --- |
| `request_id` | `<session_id>:<event>:<tool_use_id>` for `PreToolUse`, `PostToolUse` and `PostToolUseFailure`; otherwise `<session_id>:<event>:<random hex>` |
| `producer`, `source` | `--producer`; the source's authority is `host_reported` |
| `kind` | `command` for the `Bash` tool, otherwise `activity` |
| `name` | `<event>:<tool_name>` for tool events, otherwise `<event>` |
| `content_digest` | SHA-256 of the exact payload bytes the host delivered |
| `exit_code`, `duration_ms` | `null`: hook payloads do not report them, and BlackBox does not make them up |

With `--git`, a second session `<request_id>:git` holds the Git observation
(`local_git`, `locally_observed`), exactly as `capture --repo` would. It is a
separate session so that a failed snapshot, for example in a directory that is
not a repository, cannot lose the event itself.

The payload itself is never stored: tool inputs, tool output, prompts and file
contents are only hashed. Someone who kept a copy of the payload can check it
against `content_digest`; nothing else can be recovered from it. Because only
the digest is stored, a payload containing credential-shaped text is still
recorded. The credential filter applies to the stored fields (event name, tool
name, session ID); a tool name that looks like a credential is rejected.

A payload larger than 32 MiB is not parsed, but it is still recorded: since
0.6.3, `blackbox hook` reads at most that much into memory, hashes the rest as it
streams, and records an `OversizedHookPayload` observation whose
`content_digest` covers every payload byte. The event and tool names are unknown
without parsing, so they are not recorded. Since 0.6.7, with `--git` such an
event still gets a Git snapshot of the directory the host ran the hook in: it
might have ended a turn, and skipping it would let an oversized turn-end event
hide the working tree.
Before 0.6.3 such an event was rejected as `invalid_input` and not recorded at
all, so an agent could hide a tool result by making it huge.

Redelivering the same tool event is a duplicate, not a second record. The same
tool-use ID with different payload bytes is a `conflict`. Events without a host
identifier get a random one, so two identical `Stop` payloads are two stops.
The timeline orders all of them; `recorded_at` is BlackBox's clock when the hook
ran, not the host's.

## Authority

`actor assertion != host report != observer observation != BlackBox persistence`.

Hook events have their own authority, `host_reported` (schema v4): the host
reported the event, not the agent. The authority comes from the code path, not a
name: only `blackbox hook` produces it, and `capture` input stays
`caller_asserted` whatever it calls its source. A request ID first recorded
through one path conflicts on the other instead of being taken as a duplicate.
Host reports carry observations only, never claims or artifacts.

`host_reported` is not verification. BlackBox did not witness the tool run, so
the evidence stays `unverified`. The host's identity is not authenticated
either: any process running as the same user can pipe a payload into
`blackbox hook`. Only the `--git` snapshot is BlackBox's own observation
(`local_git`, `locally_observed`).

## Never blocks

BlackBox records and never governs. Hook hosts treat exit code 2 as "block this
action", so `blackbox hook` never exits 2, even on a command-line usage error:

- Success: exit 0, no output.
- Failure: exit 1 (a non-blocking error in Claude Code), stdout empty, and a
  bounded `{"error": ..., "retryable": ...}` on stderr with no input values.

Stdout always stays empty, because Claude Code adds the stdout of
`UserPromptSubmit` and `SessionStart` hooks to the agent's context.

Each hook starts a Python process and waits on the database's write lock, so
parallel tool calls queue briefly. A lock held longer than five seconds fails
that event with `database_busy`.

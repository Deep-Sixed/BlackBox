# Running BlackBox as a service (`blackboxd`)

`blackboxd` makes BlackBox an independent local service. It is the only
process that opens the database. Other components submit and read operational
events through a versioned HTTP API on a Unix socket. The design and its
rationale are in [design/service.md](design/service.md), and the API is
specified in [openapi.yaml](openapi.yaml).

BlackBox runs **alongside** EVECOR's Flight Recorder. It has its own account,
socket, storage and units, and it does not read, replace or migrate Flight
Recorder data.

## Layout

| Item | Path | Owner / mode |
| --- | --- | --- |
| Package | `/opt/blackbox/<version>/` (venv), `/opt/blackbox/current` symlink | root, 0755 |
| Database | `/var/lib/blackbox/db/blackbox.sqlite3` (+ `-wal`, `-shm`) | `blackbox-svc`, dir 0700, file 0600 |
| Socket | `/run/blackbox/blackbox.sock` | `blackbox-svc:blackbox-clients` 0660 |
| Principals | `/etc/blackbox/principals.toml` | `root:blackbox-svc` 0640 |
| Repository allowlist | `/etc/blackbox/repositories.toml` (optional) | `root:blackbox-svc` 0640 |
| Backups | `/var/backups/blackbox/blackbox-*.sqlite3` | dir `blackbox-svc:blackbox-backup` 2750, files 0640 |
| Anchor log | `~jarvis/.local/state/blackbox-anchors/anchors.jsonl` | `jarvis` 0600 |
| Units | [`deploy/systemd/`](../deploy/systemd) | — |

## Install (Friday)

Run as an administrator. None of these steps touches Flight Recorder.

```bash
# Accounts. blackbox-svc (993/970) already exists on Friday.
sudo groupadd --system blackbox-clients
sudo groupadd --system blackbox-backup
sudo usermod -aG blackbox-clients,blackbox-backup jarvis

# Package from a tagged release: hash-checked dependencies from the lockfile,
# then the wheel itself without dependency resolution.
uv export --frozen --no-dev --no-emit-project > requirements.txt   # in the tagged checkout
uv build --wheel
sudo python3.14 -m venv /opt/blackbox/0.8.0
sudo /opt/blackbox/0.8.0/bin/pip install --require-hashes -r requirements.txt
sudo /opt/blackbox/0.8.0/bin/pip install --no-deps dist/blackbox-0.8.0-py3-none-any.whl
sudo ln -sfn /opt/blackbox/0.8.0 /opt/blackbox/current

# Configuration and backup directory.
sudo install -d -o root -g blackbox-svc -m 0750 /etc/blackbox
sudo install -o root -g blackbox-svc -m 0640 principals.toml /etc/blackbox/
sudo install -d -o blackbox-svc -g blackbox-backup -m 2750 /var/backups/blackbox

# Units.
sudo install -m 0644 deploy/systemd/blackboxd.service \
     deploy/systemd/blackbox-backup.{service,timer} /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/blackboxd.service
sudo systemctl daemon-reload
sudo systemctl enable --now blackboxd.service blackbox-backup.timer
```

Dependencies come only from `uv.lock`, with hashes, so the host never resolves
versions on its own.

## Principals and tokens

Each client is a *principal*. It needs both a token and a connecting uid:
- the token is stored in `principals.toml` only as its SHA-256 hash;
- the uid is the one `blackboxd` reads from the socket's peer credentials.

```bash
blackbox token new        # {"sha256": "...", "token": "bbx_..."}
```

Give the token to the client in a 0600 file (for example
`~/.config/blackbox/token`). Put the hash in its principal entry (see
[`principals.toml.example`](../deploy/config/principals.toml.example)).

- **Rotate**
  1. List the current and the new hash.
  2. Run `systemctl reload blackboxd`.
  3. Switch the client to the new token.
  4. Remove the old hash and reload again.
- **Revoke:** remove the hash and run `systemctl reload blackboxd`.
- **A bad file** on reload is rejected and logged
  (`blackbox_config_reload_failures_total`). The running set stays in force.

Scopes: `events:write`, `events:read`, `host:report`, `metrics:read`.
`can_read` limits which producers' events a reader sees. The default is only
its own; `"*"` means all.

## Using the API

```bash
S=/run/blackbox/blackbox.sock
T=$(cat ~/.config/blackbox/token)

curl -s --unix-socket $S http://localhost/api/v1/ready

curl -s --unix-socket $S -H "Authorization: Bearer $T" \
     -H 'Content-Type: application/json' \
     -d '{"idempotency_key":"deploy-42","source":"evecor","type":"deploy.finished",
          "outcome":"success","severity":"notice","correlation_id":"deploy-42",
          "occurred_at":"2026-11-27T15:00:00Z"}' \
     http://localhost/api/v1/events

curl -s --unix-socket $S -H "Authorization: Bearer $T" \
     'http://localhost/api/v1/events?correlation_id=deploy-42'
```

Retrying with the same `idempotency_key` and body is safe: it returns `200`
with the same ids. Changing the body under the same key returns `409`.

A timed-out request (`408` or a client timeout) may still have been recorded.
Retry it with the same key, and it will be recorded at most once.

### Claude Code host hooks

Point the hooks at the daemon instead of a database. The hook digests each
payload locally with its HMAC key and sends only event and tool names plus the
digest:

```json
"command": "/opt/blackbox/current/bin/blackbox --socket /run/blackbox/blackbox.sock --token-file ~/.config/blackbox/hooks.token hook"
```

A hook never blocks the agent. If delivery fails, it exits 1 (never 2) and
writes the reason to stderr and to syslog (`blackbox-hook`).

## Backups, anchors, restore

- **Nightly backup:** `blackbox-backup.timer` runs `blackbox backup`. It uses
  SQLite's online backup API, so it never copies the live file. Each copy is
  verified with `integrity_check` and the receipt chain, renamed atomically,
  and the newest 14 are kept. The copies are 0640, so `evecor-backup` (as a
  `blackbox-backup` member) can include them.
- **Anchors:** `blackbox-anchor.timer` is a `jarvis` user unit. Every 15
  minutes it appends the chain head to the anchor log with
  `blackbox head --append`, which writes only successful heads.
  `blackbox-svc` cannot reach that log. The protection this does and does not
  give is described in [design/service.md §13.1](design/service.md).
- **Verify at any time:**
  ```bash
  blackbox --socket $S --token-file ~/.config/blackbox/anchor.token \
           check --anchor ~/.local/state/blackbox-anchors/anchors.jsonl
  ```
- **Restore:**
  1. `sudo systemctl stop blackboxd`
  2. Pick a copy. Check it with
     `blackbox --database <copy> check --anchor <a saved anchor at or below the copy's head>`.
  3. Move the current `blackbox.sqlite3` and its `-wal`/`-shm` aside. Keep them
     for analysis.
  4. Install the copy as `/var/lib/blackbox/db/blackbox.sqlite3`
     (`blackbox-svc`, 0600).
  5. `sudo systemctl start blackboxd`, then check `ready` and run
     `check --anchor`.

  Events recorded after the copy are lost. The anchor log shows how far the
  chain had advanced: `check --anchor` against a newer anchor reports
  `anchor_missing`.

  To verify a copy as another user (for example after a restic restore),
  first place it in a **private** directory, such as a new 0700 directory
  under `~jarvis`. BlackBox refuses to open a database below a directory that
  other users can write, and the CLI then reports `database_unavailable`. On
  Friday, `~jarvis/.cache` is 0777, so it does not qualify.

## Monitoring

`GET /metrics` (scope `metrics:read`) exposes:

| Metric | Meaning | Suggested alert |
| --- | --- | --- |
| `blackbox_requests_total{route,status}` | All requests | Sustained `5xx` |
| `blackbox_rejected_total{reason}` | Refused requests (auth, validation, limits, timeouts, queue) | Unexpected growth |
| `blackbox_ingest_failures_total{reason}` | Server-side failures | Any |
| `blackbox_events_ingested_total{principal}` | Recorded events | A producer goes silent |
| `blackbox_write_queue_depth` | Pending writes | > 100 |
| `blackbox_db_bytes`, `blackbox_db_filesystem_free_bytes` | Growth and headroom | Free < 10 GiB or < 10% |
| `blackbox_chain_head_sequence` | Chain length | Not advancing while producers report success |
| `blackbox_integrity_ok` | Last integrity check | `0` |

`/api/v1/ready` returns `503` until the store is opened, migrated and
integrity-checked. Logs go to the journal as JSON lines. They contain route,
status, uid and duration, never payloads or tokens.

## Failure behaviour

| Situation | What callers see |
| --- | --- |
| Bad or missing token, wrong uid | `401`, counted |
| Missing scope | `403` |
| Invalid or secret-shaped content | `422`, value not echoed |
| Same key, different content | `409` |
| Rate limit | `429` + `Retry-After` |
| Store busy, unavailable, or integrity not verified | `503` + `Retry-After`; `/ready` reports why |
| Write queue full | `503` + `Retry-After` |
| Oversized headers or body, chunked body, slow client | `431` / `413` / `411` / `408` |
| Daemon crash | Committed events persist (WAL, `synchronous=FULL`); a partial batch is never committed; retries with the same key are safe |

## Security notes

- `blackboxd` refuses to run as root.
- `blackbox-svc` has no sudo rights.
- The unit drops all capabilities and has no network.
- Authentication shows who submitted an event, not that it is true. API events
  are `caller_asserted`, and hook events are `host_reported`.
- `jarvis` has sudo, so it can bypass every local control, including anchors.
  The design document lists the off-host anchor options.

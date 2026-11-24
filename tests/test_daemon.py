"""blackboxd over a real Unix socket: framing, limits, auth, concurrency, restart."""

import asyncio
import concurrent.futures
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import blackbox as bb
from blackbox.service import daemon as daemon_module
from blackbox.service.auth import Registry, new_token, parse_principals
from blackbox.service.core import Service
from blackbox.service.daemon import DEFAULT_LIMITS, Daemon, Limits

UID = os.getuid()
EVENT = {
    "idempotency_key": "k1",
    "source": "contextforge",
    "type": "tool.invoke",
    "outcome": "success",
    "severity": "info",
}


def principals(token_hash, uid=UID):
    return (
        "[principals.forge]\n"
        f"uids = [{uid}]\n"
        'scopes = ["events:write", "events:read", "metrics:read"]\n'
        f'token_sha256 = ["{token_hash}"]\n'
    )


class Running:
    """A daemon on its own event loop thread, with a raw-socket client."""

    def __init__(self, tmp_path, limits=DEFAULT_LIMITS, uid=UID):
        self.token, token_hash = new_token()
        self.principals_path = tmp_path / "principals.toml"
        self.principals_path.write_text(principals(token_hash, uid))
        self.principals_path.chmod(0o600)
        self.socket = tmp_path / "run" / "bb.sock"
        self.socket.parent.mkdir()
        self.service = Service(
            tmp_path / "db" / "bb.sqlite3", Registry.load(self.principals_path)
        )
        self.service.start()
        self.daemon = Daemon(
            self.service,
            self.socket,
            principals_path=self.principals_path,
            limits=limits,
        )
        self.loop = asyncio.new_event_loop()
        started = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.daemon.start())
            started.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert started.wait(10)

    def close(self):
        asyncio.run_coroutine_threadsafe(self.daemon.stop(1), self.loop).result(15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    def raw(self, data: bytes, *, read=True, timeout=10) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.socket))
            client.sendall(data)
            if not read:
                return b""
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def request(self, method, path, body=None, token=..., headers=None):
        token = self.token if token is ... else token
        payload = b"" if body is None else json.dumps(body).encode()
        lines = [f"{method} {path} HTTP/1.1", "Host: blackbox"]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        if method == "POST":
            lines += [
                "Content-Type: application/json",
                f"Content-Length: {len(payload)}",
            ]
        lines += list(headers or [])
        response = self.raw(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
        return parse(response)


def parse(response: bytes):
    head, _, body = response.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    headers = dict(
        line.decode().split(": ", 1) for line in head.split(b"\r\n")[1:] if line
    )
    text = body.decode()
    try:
        return status, headers, json.loads(text)
    except ValueError:
        return status, headers, text


@pytest.fixture
def running(tmp_path):
    instance = Running(tmp_path)
    yield instance
    instance.close()


def test_socket_is_private_to_owner_and_group(running):
    mode = running.socket.stat().st_mode & 0o777
    assert mode == 0o660


def test_end_to_end_submit_and_read(running):
    status, headers, body = running.request("POST", "/api/v1/events", EVENT)
    assert status == 201 and headers["Connection"] == "close"
    event_id = body["event_id"]
    status, _, view = running.request("GET", f"/api/v1/events/{event_id}")
    assert status == 200 and view["type"] == "tool.invoke"
    status, _, listing = running.request("GET", "/api/v1/events?severity=info&limit=5")
    assert status == 200 and [e["event_id"] for e in listing["events"]] == [event_id]
    status, _, text = running.request("GET", "/metrics")
    assert status == 200 and "blackbox_events_ingested_total" in text


def test_peer_uid_comes_from_the_kernel(tmp_path):
    # Registered for another uid: this process's valid token is refused.
    instance = Running(tmp_path, uid=UID + 4242)
    try:
        status, _, body = instance.request("POST", "/api/v1/events", EVENT)
        assert (status, body) == (401, {"error": "unauthorized"})
    finally:
        instance.close()


def test_status_routes_need_no_token(running):
    for path in ("/api/v1/health", "/api/v1/ready", "/api/v1/version"):
        assert running.request("GET", path, token=None)[0] == 200


@pytest.mark.parametrize(
    "data,status",
    [
        (b"NOT A REQUEST\r\n\r\n", 400),
        (b"GET /api/v1/health HTTP/1.1\r\n\r\n", 400),  # HTTP/1.1 requires Host
        (b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n", 400),
        (b"GET /nowhere HTTP/1.1\r\nHost: x\r\n\r\n", 404),
        (b"DELETE /api/v1/events HTTP/1.1\r\nHost: x\r\n\r\n", 405),
        (b"PUT /api/v1/health HTTP/1.1\r\nHost: x\r\n\r\n", 405),
        (b"GET /api/v1/health?x=1 HTTP/1.1\r\nHost: x\r\n\r\n", 400),
        (
            (
                b"POST /api/v1/events HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/json\r\n\r\n"
            ),
            411,
        ),
        (
            (
                b"POST /api/v1/events HTTP/1.1\r\nHost: x\r\n"
                b"Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n"
                b"2\r\n{}\r\n0\r\n\r\n"
            ),
            411,
        ),
        (
            (
                b"POST /api/v1/events HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\n\r\n{}"
            ),
            415,
        ),
        (
            (
                b"POST /api/v1/events HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/json\r\nContent-Length: 5\r\n\r\nnope!"
            ),
            400,
        ),
        (
            (
                b"POST /api/v1/events HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n[]"
            ),
            400,
        ),
    ],
)
def test_malformed_requests_are_refused(running, data, status):
    got, headers, body = parse(running.raw(data))
    assert got == status
    assert set(body) == {"error"}
    assert headers["Connection"] == "close"


def test_oversized_headers_are_refused(running):
    big = b"X-Filler: " + b"a" * (20 * 1024) + b"\r\n"
    status, _, body = parse(
        running.raw(b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\n" + big + b"\r\n")
    )
    assert (status, body) == (431, {"error": "headers_too_large"})


def test_oversized_body_is_refused_before_it_is_read(running):
    head = (
        b"POST /api/v1/events HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: 2000000\r\n\r\n"
    )
    status, _, body = parse(running.raw(head))  # no body bytes are ever sent
    assert (status, body) == (413, {"error": "body_too_large"})


def test_incomplete_requests_time_out(tmp_path):
    instance = Running(tmp_path, limits=Limits(idle_seconds=0.3, request_seconds=1))
    try:
        started = time.monotonic()
        status, _, body = parse(
            instance.raw(b"POST /api/v1/events HTTP/1.1\r\nHost: x\r\n")
        )
        assert status == 408 and body == {"error": "timeout"}
        assert time.monotonic() - started < 3
        # The slot was released: the daemon still answers.
        assert instance.request("GET", "/api/v1/health", token=None)[0] == 200
    finally:
        instance.close()


def test_connection_cap_refuses_extra_connections(tmp_path):
    instance = Running(tmp_path, limits=Limits(max_connections=2, idle_seconds=2))
    holders = []
    try:
        for _ in range(2):
            held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            held.connect(str(instance.socket))
            held.sendall(b"GET /api/v1/health HTTP/1.1\r\n")  # never finished
            holders.append(held)
        time.sleep(0.2)
        status, headers, body = parse(
            instance.raw(b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
        )
        assert (status, body) == (503, {"error": "busy"})
        assert headers["Retry-After"] == "1"
    finally:
        for held in holders:
            held.close()
        instance.close()


def test_concurrent_submissions_all_land_once(running):
    def submit(i):
        return running.request(
            "POST", "/api/v1/events", {**EVENT, "idempotency_key": f"c{i}"}
        )[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(submit, range(48)))
    assert statuses.count(201) == 48
    retry = running.request(
        "POST", "/api/v1/events", {**EVENT, "idempotency_key": "c3"}
    )
    assert retry[0] == 200 and retry[2]["duplicate"] is True
    assert bb.check_integrity(running.service.database).ok
    _, _, page = running.request("GET", "/api/v1/events?limit=100")
    assert len(page["events"]) == 48


def test_full_write_queue_is_visible(running):
    running.daemon.pending_writes = running.daemon.limits.max_pending_writes
    status, headers, body = running.request("POST", "/api/v1/events", EVENT)
    assert (status, body) == (503, {"error": "busy"}) and headers["Retry-After"] == "1"
    running.daemon.pending_writes = 0


def test_reload_rotates_tokens_and_keeps_old_set_on_bad_file(running):
    new, new_hash = new_token()
    running.principals_path.write_text(principals(new_hash))
    running.daemon.reload()
    assert running.request("POST", "/api/v1/events", EVENT)[0] == 401
    assert running.request("POST", "/api/v1/events", EVENT, token=new)[0] == 201
    running.principals_path.write_text("this is = not [valid")
    running.daemon.reload()
    assert running.request("GET", "/api/v1/events", token=new)[0] == 200
    assert running.service.metrics.counter("blackbox_config_reload_failures_total") == 1


def test_daemon_refuses_to_run_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: 0)
    code = daemon_module.main(
        [
            "--database",
            str(tmp_path / "db.sqlite3"),
            "--socket",
            str(tmp_path / "s.sock"),
            "--principals",
            str(tmp_path / "p.toml"),
        ]
    )
    assert code == 2


def test_socket_path_that_is_not_a_socket_is_refused(tmp_path):
    instance_path = tmp_path / "run"
    instance_path.mkdir()
    (instance_path / "bb.sock").write_text("not a socket")
    registry = Registry(parse_principals(""))
    service = Service(tmp_path / "db" / "bb.sqlite3", registry)
    daemon = Daemon(service, instance_path / "bb.sock")
    with pytest.raises(RuntimeError):
        asyncio.run(daemon.start())
    assert (instance_path / "bb.sock").read_text() == "not a socket"


def start_process(tmp_path, token_hash):
    principals_path = tmp_path / "p.toml"
    principals_path.write_text(principals(token_hash))
    principals_path.chmod(0o600)
    sock = tmp_path / "proc.sock"
    # blackboxd refuses to run as root. When the suite itself runs as root
    # (some containers), present a non-root euid to the child only.
    launcher = (
        ["-m", "blackbox.service.daemon"]
        if os.geteuid() != 0
        else [
            "-c",
            (
                "import os, runpy, sys; os.geteuid = lambda: 65534; "
                "sys.argv = ['blackboxd', *sys.argv[1:]]; "
                "runpy.run_module('blackbox.service.daemon', run_name='__main__')"
            ),
        ]
    )
    process = subprocess.Popen(
        [
            sys.executable,
            *launcher,
            "--database",
            str(tmp_path / "db" / "bb.sqlite3"),
            "--socket",
            str(sock),
            "--principals",
            str(principals_path),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    # A crashed daemon leaves its socket file behind, so wait until the new
    # process actually accepts connections rather than for the path to exist.
    for _ in range(200):
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(sock))
            except OSError:
                time.sleep(0.05)
                continue
        break
    else:
        raise AssertionError("daemon did not start listening")
    return process, sock


def client(sock: Path, token, method, path, body=None):
    holder = type("C", (), {})()
    holder.socket = sock
    holder.token = token
    holder.raw = Running.raw.__get__(holder)
    return Running.request(holder, method, path, body)


def test_records_survive_a_crash_and_a_clean_restart(tmp_path):
    token, token_hash = new_token()
    process, sock = start_process(tmp_path, token_hash)
    try:
        first = client(sock, token, "POST", "/api/v1/events", EVENT)
        assert first[0] == 201
        process.kill()  # no graceful shutdown, no checkpoint
        process.wait(10)
        process, sock = start_process(tmp_path, token_hash)
        again = client(sock, token, "POST", "/api/v1/events", EVENT)
        assert again[0] == 200 and again[2]["event_id"] == first[2]["event_id"]
        assert client(sock, token, "GET", "/api/v1/integrity")[2]["ok"] is True
        process.send_signal(signal.SIGTERM)
        assert process.wait(15) == 0
        assert not sock.exists()
        wal = tmp_path / "db" / "bb.sqlite3-wal"
        assert not wal.exists() or wal.stat().st_size == 0  # checkpointed
        process, sock = start_process(tmp_path, token_hash)
        listing = client(sock, token, "GET", "/api/v1/events")
        assert [e["event_id"] for e in listing[2]["events"]] == [first[2]["event_id"]]
    finally:
        process.kill()
        process.wait(10)


def test_openapi_documents_every_route():
    spec = (Path(__file__).parents[1] / "docs/openapi.yaml").read_text()
    for route in (*daemon_module.ROUTES, "/api/v1/events"):
        assert f"  {route}:" in spec, route

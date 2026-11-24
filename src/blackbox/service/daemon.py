"""`blackboxd`: HTTP/1.1 over a Unix socket, one request per connection.

Hardening at the transport, before any request reaches the service layer:

* Caller identity is the peer uid from SO_PEERCRED, read from the kernel.
* h11 enforces HTTP/1.1 framing; headers are capped (`max_incomplete_event_size`).
* Bodies need Content-Length (chunked or missing is refused) and are capped
  before any byte is read.
* Every connection has a total deadline and an idle deadline, so incomplete
  or slow requests cannot hold a slot.
* Connections are capped; extra ones get an immediate 503 and are closed.
* Writes run on exactly one thread (the single SQLite writer) behind a bounded
  queue; when it is full callers get 503 + Retry-After, never a silent drop.
* Responses are JSON or Prometheus text with `Connection: close`; error bodies
  carry only a code. Logs carry route, status, uid and timing, never payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import json
import logging
import os
import re
import signal
import socket
import stat
import struct
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import h11

from .auth import ConfigError, Registry
from .core import Outcome, Service

log = logging.getLogger("blackboxd")

EVENT_PATH = re.compile(r"^/api/v1/events/(obs_[a-f0-9]{64})$")
ROUTES = {
    "/api/v1/health": "GET",
    "/api/v1/ready": "GET",
    "/api/v1/version": "GET",
    "/metrics": "GET",
    "/api/v1/events/batch": "POST",
    "/api/v1/host-reports": "POST",
    "/api/v1/integrity": "GET",
    "/api/v1/chain-head": "GET",
    "/api/v1/events/{event_id}": "GET",
}
WRITE_ROUTES = {"/api/v1/events/batch", "/api/v1/host-reports"}
INLINE_ROUTES = {"/api/v1/health", "/api/v1/ready", "/api/v1/version"}
REASONS = {
    200: b"OK",
    201: b"Created",
    400: b"Bad Request",
    401: b"Unauthorized",
    403: b"Forbidden",
    404: b"Not Found",
    405: b"Method Not Allowed",
    408: b"Request Timeout",
    409: b"Conflict",
    411: b"Length Required",
    413: b"Content Too Large",
    415: b"Unsupported Media Type",
    422: b"Unprocessable Content",
    429: b"Too Many Requests",
    431: b"Request Header Fields Too Large",
    500: b"Internal Server Error",
    503: b"Service Unavailable",
}


@dataclass(frozen=True)
class Limits:
    header_bytes: int = 16 * 1024
    body_bytes: int = 1024 * 1024
    request_seconds: float = 10.0
    idle_seconds: float = 5.0
    max_connections: int = 64
    max_pending_writes: int = 256
    max_query_parameters: int = 20
    max_query_value: int = 512


DEFAULT_LIMITS = Limits()


class Rejected(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def peer_uid(sock: socket.socket | None) -> int | None:
    if sock is None:
        return None
    try:
        raw = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def load_repositories(path: str | Path | None) -> list[Path]:
    """Operator allowlist of repository roots: `roots = ["/abs/path", ...]`."""
    if path is None:
        return []
    document = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if set(document) - {"roots"}:
        raise ConfigError("unknown key in repositories file")
    roots = document.get("roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise ConfigError("roots must be a list of paths")
    result = []
    for root in roots:
        candidate = Path(root)
        if not candidate.is_absolute() or not candidate.is_dir():
            raise ConfigError("repository roots must be existing absolute directories")
        result.append(candidate.resolve())
    return result


class Daemon:
    def __init__(
        self,
        service: Service,
        socket_path: str | Path,
        *,
        principals_path: str | Path | None = None,
        socket_group: str | None = None,
        limits: Limits = DEFAULT_LIMITS,
    ):
        self.service = service
        self.socket_path = Path(socket_path)
        self.principals_path = principals_path
        self.socket_group = socket_group
        self.limits = limits
        self.active = 0
        self.pending_writes = 0
        self.writer = ThreadPoolExecutor(1, thread_name_prefix="blackbox-writer")
        self.readers = ThreadPoolExecutor(4, thread_name_prefix="blackbox-reader")
        self.server: asyncio.base_events.Server | None = None
        self.stopping: asyncio.Event | None = None

    # --- lifecycle -----------------------------------------------------------

    def _prepare_socket_path(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("socket path exists and is not a socket")
        self.socket_path.unlink()

    async def start(self) -> None:
        self._prepare_socket_path()
        old = os.umask(0o117)
        try:
            self.server = await asyncio.start_unix_server(
                self._connection, path=str(self.socket_path), backlog=128
            )
        finally:
            os.umask(old)
        if self.socket_group is not None:
            os.chown(self.socket_path, -1, grp.getgrnam(self.socket_group).gr_gid)
        os.chmod(self.socket_path, 0o660)
        self.stopping = asyncio.Event()
        log.info(json.dumps({"event": "listening", "socket": str(self.socket_path)}))

    async def serve_forever(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self.reload)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        refresher = asyncio.create_task(self._refresh_gauges())
        await self.stopping.wait()
        refresher.cancel()
        await self.stop()

    async def stop(self, drain_seconds: float = 10.0) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        deadline = time.monotonic() + drain_seconds
        while self.active and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.writer.shutdown(wait=True)
        self.readers.shutdown(wait=True)
        with contextlib.suppress(OSError):
            self.socket_path.unlink()
        self._checkpoint()
        log.info(json.dumps({"event": "stopped"}))

    def _checkpoint(self) -> None:
        import sqlite3

        with contextlib.suppress(sqlite3.Error, OSError):
            connection = sqlite3.connect(self.service.database, timeout=5)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()

    def reload(self) -> None:
        """SIGHUP: swap in a re-read principals file, or keep the current set."""
        if self.principals_path is None:
            return
        try:
            self.service.reload(Registry.load(self.principals_path))
            log.info(
                json.dumps(
                    {"event": "reloaded", "principals": len(self.service.registry)}
                )
            )
        except (ConfigError, OSError) as error:
            self.service.metrics.inc("blackbox_config_reload_failures_total")
            log.error(
                json.dumps({"event": "reload_rejected", "reason": type(error).__name__})
            )

    async def _refresh_gauges(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(60)
            await loop.run_in_executor(self.readers, self.service.refresh_gauges)

    # --- connections ---------------------------------------------------------

    async def _connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        started = time.monotonic()
        uid = peer_uid(writer.get_extra_info("socket"))
        connection = h11.Connection(
            h11.SERVER, max_incomplete_event_size=self.limits.header_bytes
        )
        route, status = "-", 0
        if self.active >= self.limits.max_connections:
            self.service.metrics.inc(
                "blackbox_rejected_total", reason="connection_limit"
            )
            status = 503
            await self._send(
                writer,
                connection,
                503,
                {"error": "busy"},
                {"Retry-After": "1"},
                raw=True,
            )
            await self._close(reader, writer)
            return
        self.active += 1
        try:
            async with asyncio.timeout(self.limits.request_seconds):
                request, body = await self._read_request(reader, connection)
                route, outcome = await self._dispatch(request, body, uid)
            status = outcome.status
            await self._send(
                writer, connection, outcome.status, outcome.body, outcome.headers
            )
        except Rejected as rejection:
            status = rejection.status
            self.service.metrics.inc("blackbox_rejected_total", reason=rejection.code)
            await self._send(
                writer,
                connection,
                rejection.status,
                {"error": rejection.code},
                raw=True,
            )
        except TimeoutError:
            status = 408
            self.service.metrics.inc("blackbox_rejected_total", reason="timeout")
            await self._send(writer, connection, 408, {"error": "timeout"}, raw=True)
        except ConnectionError, h11.LocalProtocolError:
            status = -1
        finally:
            self.active -= 1
            await self._close(reader, writer)
            log.info(
                json.dumps(
                    {
                        "route": route,
                        "status": status,
                        "uid": uid,
                        "ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
            )

    async def _close(self, reader, writer) -> None:
        """Lingering close: unread request bytes would make the kernel reset the
        connection and the client could lose the response already sent."""
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()
            deadline = time.monotonic() + 0.5
            drained = 0
            while drained < self.limits.body_bytes and time.monotonic() < deadline:
                chunk = await asyncio.wait_for(reader.read(65536), 0.2)
                if not chunk:
                    break
                drained += len(chunk)
        with contextlib.suppress(Exception):
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 1)

    async def _next_event(self, reader, connection):
        while True:
            try:
                event = connection.next_event()
            except h11.RemoteProtocolError as error:
                if error.error_status_hint == 431:
                    raise Rejected(431, "headers_too_large") from None
                raise Rejected(400, "bad_request") from None
            if event is h11.NEED_DATA:
                data = await asyncio.wait_for(
                    reader.read(65536), self.limits.idle_seconds
                )
                connection.receive_data(data)
                if not data and connection.their_state is not h11.DONE:
                    raise ConnectionError("client closed")
                continue
            return event

    async def _read_request(self, reader, connection):
        event = await self._next_event(reader, connection)
        if isinstance(event, h11.ConnectionClosed):
            raise ConnectionError("client closed")
        if not isinstance(event, h11.Request):
            raise Rejected(400, "bad_request")
        # h11 caps only a header block still incomplete in its buffer; a large
        # block that arrives in one read is parsed whole, so check the result.
        head_size = (
            len(event.method)
            + len(event.target)
            + sum(len(name) + len(value) + 4 for name, value in event.headers)
        )
        if head_size > self.limits.header_bytes:
            raise Rejected(431, "headers_too_large")
        headers = {}
        for name, value in event.headers:
            key = name.decode("latin-1").lower()
            if key in headers:
                raise Rejected(400, "duplicate_header")
            headers[key] = value.decode("latin-1")
        if "transfer-encoding" in headers:
            raise Rejected(411, "length_required")
        body = b""
        if event.method == b"POST":
            length = headers.get("content-length")
            if length is None:
                raise Rejected(411, "length_required")
            if int(length) > self.limits.body_bytes:
                raise Rejected(413, "body_too_large")
            if (
                headers.get("content-type", "").split(";")[0].strip()
                != "application/json"
            ):
                raise Rejected(415, "unsupported_media_type")
            chunks = []
            while True:
                part = await self._next_event(reader, connection)
                if isinstance(part, h11.Data):
                    chunks.append(part.data)
                elif isinstance(part, h11.EndOfMessage):
                    break
                else:
                    raise Rejected(400, "bad_request")
            body = b"".join(chunks)
        else:
            if headers.get("content-length", "0") != "0":
                raise Rejected(400, "unexpected_body")
            end = await self._next_event(reader, connection)
            if not isinstance(end, h11.EndOfMessage):
                raise Rejected(400, "bad_request")
        return (
            event.method.decode("ascii"),
            event.target.decode("ascii"),
            headers,
        ), body

    def _route(self, method: str, target: str):
        try:
            split = urlsplit(target)
        except ValueError:
            raise Rejected(400, "bad_request") from None
        path, event_id = split.path, None
        match = EVENT_PATH.match(path)
        if match:
            path, event_id = "/api/v1/events/{event_id}", match.group(1)
        if path == "/api/v1/events":
            if method not in ("GET", "POST"):
                raise Rejected(405, "method_not_allowed")
        elif path not in ROUTES:
            raise Rejected(404, "not_found")
        elif ROUTES[path] != method:
            raise Rejected(405, "method_not_allowed")
        try:
            pairs = parse_qsl(
                split.query, keep_blank_values=True, strict_parsing=bool(split.query)
            )
        except ValueError:
            raise Rejected(400, "bad_request") from None
        if len(pairs) > self.limits.max_query_parameters:
            raise Rejected(400, "too_many_parameters")
        parameters = {}
        for key, value in pairs:
            if key in parameters or len(value) > self.limits.max_query_value:
                raise Rejected(400, "bad_query")
            parameters[key] = value
        return path, event_id, parameters

    async def _dispatch(self, request, body: bytes, uid):
        method, target, headers = request
        route, event_id, parameters = self._route(method, target)
        if (
            method == "GET"
            and parameters
            and route not in ("/api/v1/events", "/api/v1/integrity")
        ):
            raise Rejected(400, "bad_query")
        payload = None
        if method == "POST":
            try:
                payload = json.loads(body)
            except ValueError:
                raise Rejected(400, "invalid_json") from None
            if not isinstance(payload, dict):
                raise Rejected(400, "invalid_json")
        authorization = headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else None

        def call():
            return self.service.handle(
                method,
                route,
                token=token,
                uid=uid,
                body=payload,
                parameters=parameters,
                event_id=event_id,
            )

        if route in INLINE_ROUTES:
            return route, call()
        loop = asyncio.get_running_loop()
        if method == "POST":
            if self.pending_writes >= self.limits.max_pending_writes:
                self.service.metrics.inc("blackbox_rejected_total", reason="queue_full")
                return route, Outcome(503, {"error": "busy"}, {"Retry-After": "1"})
            self.pending_writes += 1
            self.service.metrics.set("blackbox_write_queue_depth", self.pending_writes)
            try:
                return route, await loop.run_in_executor(self.writer, call)
            finally:
                self.pending_writes -= 1
                self.service.metrics.set(
                    "blackbox_write_queue_depth", self.pending_writes
                )
        return route, await loop.run_in_executor(self.readers, call)

    async def _send(self, writer, connection, status, body, headers=None, *, raw=False):
        if isinstance(body, str):
            data, content_type = (
                body.encode(),
                "text/plain; version=0.0.4; charset=utf-8",
            )
        else:
            data, content_type = (
                json.dumps(body, separators=(",", ":")).encode(),
                "application/json",
            )
        header_list = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Connection", "close"),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            *((headers or {}).items()),
        ]
        if raw or connection.our_state is not h11.SEND_RESPONSE:
            # Protocol state may not allow a response (bad request line, limit);
            # write a minimal one directly.
            head = f"HTTP/1.1 {status} {REASONS.get(status, b'Error').decode()}\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in header_list) + "\r\n"
            payload = head.encode("latin-1") + data
        else:
            payload = connection.send(
                h11.Response(
                    status_code=status,
                    headers=header_list,
                    reason=REASONS.get(status, b""),
                )
            )
            payload += connection.send(h11.Data(data=data))
            payload += connection.send(h11.EndOfMessage())
        with contextlib.suppress(ConnectionError):
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), self.limits.idle_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blackboxd", description="BlackBox service")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--principals", type=Path, required=True)
    parser.add_argument("--repositories", type=Path)
    parser.add_argument("--socket-group")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    if os.geteuid() == 0:
        log.error(json.dumps({"event": "refused", "reason": "running_as_root"}))
        return 2
    os.umask(0o077)
    try:
        registry = Registry.load(args.principals)
        repositories = load_repositories(args.repositories)
    except (ConfigError, OSError) as error:
        log.error(
            json.dumps({"event": "config_rejected", "reason": type(error).__name__})
        )
        return 2
    service = Service(args.database, registry, repositories=repositories)
    service.start()
    if service.ready_reason is not None:
        log.error(json.dumps({"event": "not_ready", "reason": service.ready_reason}))
    daemon = Daemon(
        service,
        args.socket,
        principals_path=args.principals,
        socket_group=args.socket_group,
    )
    asyncio.run(daemon.serve_forever())
    return 0


if __name__ == "__main__":
    sys.exit(main())

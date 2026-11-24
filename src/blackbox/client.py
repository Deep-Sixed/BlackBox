"""Minimal blackboxd client over the Unix socket (stdlib only).

Tokens are read from a private file, never from the command line or the
environment, so they do not appear in process listings or inherited
environments.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from pathlib import Path
from urllib.parse import urlencode

DEFAULT_TOKEN_FILE = "~/.config/blackbox/token"


class ClientError(Exception):
    """The daemon was unreachable or refused the request; `code` is bounded."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("blackbox", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def read_token(path: str | Path | None) -> str:
    location = Path(path or DEFAULT_TOKEN_FILE).expanduser()
    try:
        fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise ClientError("token_unavailable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ClientError("token_file_not_private")
        token = os.read(fd, 512).decode("ascii", "replace").strip()
    finally:
        os.close(fd)
    if not token:
        raise ClientError("token_unavailable")
    return token


class Client:
    def __init__(self, socket_path: str | Path, token: str | None, timeout=5.0):
        self.socket_path = str(socket_path)
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body=None, query=None):
        if query:
            path = f"{path}?{urlencode(query)}"
        headers = {"Host": "blackbox"}
        payload = None
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        connection = _UnixConnection(self.socket_path, self.timeout)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(8 * 1024 * 1024)
            status = response.status
        except OSError, http.client.HTTPException:
            raise ClientError("daemon_unreachable") from None
        finally:
            connection.close()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            raise ClientError("bad_response", status) from None
        if status >= 400:
            code = data.get("error") if isinstance(data, dict) else None
            raise ClientError(code if isinstance(code, str) else "error", status)
        return data

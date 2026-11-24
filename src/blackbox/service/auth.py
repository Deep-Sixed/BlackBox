"""Principals, scoped tokens and peer-uid binding for the BlackBox service.

A request is authorized only when BOTH hold:

* its bearer token hashes to one of a principal's registered SHA-256 hashes, and
* the connecting process's uid (SO_PEERCRED) is one of that principal's uids.

Neither check alone grants anything, and every failure is the same
`Unauthorized`, so a caller cannot learn which part was wrong. Raw tokens are
never stored: the principals file holds only hashes, so rotating or revoking a
credential is an edit plus a reload, not a redeployment.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

SCOPES = frozenset({"events:write", "events:read", "host:report", "metrics:read"})
PRINCIPAL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
TOKEN_HASH = re.compile(r"^[a-f0-9]{64}$")
TOKEN_PREFIX = "bbx_"
# At most a current and a next hash, so a rotation can overlap without
# accumulating forgotten credentials.
MAX_HASHES = 2
DEFAULT_RATES = {"write": (50.0, 200), "read": (20.0, 60)}


class Unauthorized(Exception):
    """Missing, unknown or misbound credentials. Deliberately uninformative."""


class Forbidden(Exception):
    """Authenticated, but the principal lacks the scope for this operation."""


class ConfigError(ValueError):
    """The principals file is unsafe or invalid; the previous set stays active."""


@dataclass(frozen=True)
class Principal:
    name: str
    uids: frozenset[int]
    scopes: frozenset[str]
    token_hashes: tuple[str, ...]
    # Producers whose events this principal may read; "*" means all.
    can_read: frozenset[str]
    write_rate: tuple[float, int]
    read_rate: tuple[float, int]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise Forbidden(scope)

    def may_read(self, producer: str) -> bool:
        return "*" in self.can_read or producer in self.can_read

    def readable(self) -> frozenset[str] | None:
        """Producers this principal may read, or None for all."""
        return None if "*" in self.can_read else self.can_read


def new_token() -> tuple[str, str]:
    """Return a fresh token and the hash to put in the principals file."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_config_file(path: Path) -> None:
    """Refuse a principals file that another account could have written."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError("principals file is not a regular file")
    if info.st_uid not in (0, os.geteuid()):
        raise ConfigError("principals file must be owned by root or the service")
    if info.st_mode & 0o022:
        raise ConfigError("principals file must not be group- or world-writable")
    if info.st_mode & 0o004:
        raise ConfigError("principals file must not be world-readable")


def _rate(raw, default):
    if raw is None:
        return default
    if (
        not isinstance(raw, dict)
        or set(raw) - {"per_second", "burst"}
        or not isinstance(raw.get("per_second", default[0]), (int, float))
        or not isinstance(raw.get("burst", default[1]), int)
    ):
        raise ConfigError("invalid rate")
    per_second = float(raw.get("per_second", default[0]))
    burst = raw.get("burst", default[1])
    if not 0 < per_second <= 10_000 or not 1 <= burst <= 100_000:
        raise ConfigError("rate out of range")
    return per_second, burst


def parse_principals(text: str) -> dict[str, Principal]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ConfigError("principals file is not valid TOML") from None
    if set(document) - {"principals"}:
        raise ConfigError("unknown top-level key")
    table = document.get("principals", {})
    if not isinstance(table, dict):
        raise ConfigError("principals must be a table")
    principals: dict[str, Principal] = {}
    seen_hashes: set[str] = set()
    for name, entry in table.items():
        if not PRINCIPAL_NAME.match(name) or not isinstance(entry, dict):
            raise ConfigError("invalid principal name")
        unknown = set(entry) - {
            "uids",
            "scopes",
            "token_sha256",
            "can_read",
            "write_rate",
            "read_rate",
        }
        if unknown:
            raise ConfigError("unknown principal key")
        uids = entry.get("uids")
        if (
            not isinstance(uids, list)
            or not uids
            or not all(type(uid) is int and 0 <= uid < 2**32 for uid in uids)
        ):
            raise ConfigError("uids must be a non-empty list of uids")
        scopes = entry.get("scopes")
        if (
            not isinstance(scopes, list)
            or not scopes
            or not all(isinstance(s, str) for s in scopes)
            or set(scopes) - SCOPES
        ):
            raise ConfigError("invalid scopes")
        hashes = entry.get("token_sha256")
        if (
            not isinstance(hashes, list)
            or not 1 <= len(hashes) <= MAX_HASHES
            or not all(isinstance(h, str) and TOKEN_HASH.match(h) for h in hashes)
        ):
            raise ConfigError("token_sha256 must list one or two SHA-256 hashes")
        if seen_hashes & set(hashes) or len(set(hashes)) != len(hashes):
            raise ConfigError("a token hash may belong to one principal only")
        seen_hashes |= set(hashes)
        can_read = entry.get("can_read", [name])
        if not isinstance(can_read, list) or not all(
            isinstance(p, str) and (p == "*" or PRINCIPAL_NAME.match(p))
            for p in can_read
        ):
            raise ConfigError("invalid can_read")
        principals[name] = Principal(
            name=name,
            uids=frozenset(uids),
            scopes=frozenset(scopes),
            token_hashes=tuple(hashes),
            can_read=frozenset(can_read),
            write_rate=_rate(entry.get("write_rate"), DEFAULT_RATES["write"]),
            read_rate=_rate(entry.get("read_rate"), DEFAULT_RATES["read"]),
        )
    return principals


class Registry:
    """An immutable principal set, swapped whole on reload."""

    def __init__(self, principals: dict[str, Principal]):
        self._principals = dict(principals)

    @classmethod
    def load(cls, path: str | Path, *, check_permissions: bool = True) -> Registry:
        path = Path(path)
        if check_permissions:
            check_config_file(path)
        return cls(parse_principals(path.read_text(encoding="utf-8")))

    def __len__(self) -> int:
        return len(self._principals)

    def authenticate(self, token: str | None, uid: int | None) -> Principal:
        if not token or uid is None or len(token) > 256:
            raise Unauthorized()
        offered = hash_token(token)
        match = None
        # Compare against every hash so timing does not reveal which matched.
        for principal in self._principals.values():
            for known in principal.token_hashes:
                if hmac.compare_digest(offered, known):
                    match = principal
        if match is None or uid not in match.uids:
            raise Unauthorized()
        return match

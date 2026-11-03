"""Credentials and where they are kept.

A :class:`TokenStore` lets several programs (an agent harness, a Home Assistant
integration, the CLI) share one login instead of each holding a password.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .const import ENV_TOKEN_FILE


@dataclass
class Credentials:
    """An OAuth2 session with Skylight."""

    access_token: str
    refresh_token: str | None = None
    #: Unix timestamp after which ``access_token`` is no longer valid.
    expires_at: float | None = None
    #: Random id sent with token requests; Skylight ties sessions to a "device".
    device_fingerprint: str = field(default_factory=lambda: uuid.uuid4().hex)

    def is_expired(self, *, leeway: float = 60.0, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at - leeway

    @classmethod
    def from_token_response(
        cls, payload: dict[str, Any], *, device_fingerprint: str | None = None
    ) -> "Credentials":
        access = payload.get("access_token") or payload.get("token")
        if not access:
            raise ValueError("token response has no access_token")
        expires_at: float | None = None
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)):
            created = payload.get("created_at")
            base = float(created) if isinstance(created, (int, float)) else time.time()
            expires_at = base + float(expires_in)
        return cls(
            access_token=str(access),
            refresh_token=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
            expires_at=expires_at,
            device_fingerprint=device_fingerprint or uuid.uuid4().hex,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Credentials":
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            device_fingerprint=data.get("device_fingerprint") or uuid.uuid4().hex,
        )


class TokenStore(Protocol):
    """Persist credentials between runs. Implementations must be cheap to call."""

    def load(self) -> Credentials | None: ...

    def save(self, credentials: Credentials) -> None: ...

    def clear(self) -> None: ...


class MemoryTokenStore:
    """Keeps credentials for the lifetime of the process only."""

    def __init__(self, credentials: Credentials | None = None) -> None:
        self._credentials = credentials

    def load(self) -> Credentials | None:
        return self._credentials

    def save(self, credentials: Credentials) -> None:
        self._credentials = credentials

    def clear(self) -> None:
        self._credentials = None


def default_token_path() -> Path:
    """``$SKYLIGHT_TOKEN_FILE``, else ``$XDG_CONFIG_HOME/skylight-calendar/credentials.json``."""
    env = os.environ.get(ENV_TOKEN_FILE)
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "skylight-calendar" / "credentials.json"


class FileTokenStore:
    """JSON file, created with owner-only permissions."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_token_path()

    def load(self) -> Credentials | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
            return Credentials.from_dict(data)
        except (ValueError, KeyError, TypeError):
            return None

    def save(self, credentials: Credentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(credentials.to_dict(), fh, indent=2)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

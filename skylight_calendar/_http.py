"""HTTP transport: ``httpx`` when it is installed, the standard library otherwise.

The sync client and the login flow only need a small slice of ``httpx.Client``:
``request`` / ``get`` / ``post`` with ``params``, ``json``, ``data`` and
``headers``, redirects *not* followed, cookies kept between calls, and a
response with ``status_code``, ``headers``, ``content``, ``text`` and
``json()``. :class:`StdlibClient` provides exactly that on ``urllib``, so the
package works with no third-party dependency at all (for projects that keep
their dependency list short). ``AsyncSkylightClient`` still needs ``httpx``.

``allowed_hosts`` lets a host application refuse to send anything (a token, a
password) anywhere but Skylight, whatever URL it is handed.
"""

from __future__ import annotations

import http.cookiejar
import json as _json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from email.message import Message
from typing import Any

try:  # optional
    import httpx as _httpx
except ImportError:  # pragma: no cover - exercised when httpx is absent
    _httpx = None  # type: ignore[assignment]


class TransportError(Exception):
    """A request could not be made (DNS, connection, timeout, refused host)."""


#: What the client and login code catch as "the network failed".
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (TransportError,) + ((_httpx.HTTPError,) if _httpx is not None else ())

_MAX_BODY = 32 * 1024 * 1024
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 10


def have_httpx() -> bool:
    return _httpx is not None


def require_httpx(what: str) -> Any:
    if _httpx is None:
        from .errors import SkylightConfigError

        raise SkylightConfigError(f"{what} needs httpx: pip install 'skylight-calendar[httpx]'")
    return _httpx


class Headers:
    """Case-insensitive, read-only response headers (``resp.headers["location"]``, ``.get()``)."""

    def __init__(self, message: Message | None = None) -> None:
        self._items: list[tuple[str, str]] = list(message.items()) if message is not None else []

    def get(self, name: str, default: Any = None) -> Any:
        name = name.lower()
        for key, value in self._items:
            if key.lower() == name:
                return value
        return default

    def get_list(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self._items if k.lower() == name]

    def __getitem__(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None


class Response:
    """The parts of ``httpx.Response`` this package reads."""

    def __init__(self, status_code: int, headers: Headers, content: bytes, url: str = "") -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.url = url

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "") or ""
        for part in ctype.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip('"')
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.content)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None  # hand the 3xx back to the caller, like httpx with follow_redirects=False


def _query_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return ""
    return str(value)


def _with_params(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((key, _query_value(v)) for v in value)
        else:
            pairs.append((key, _query_value(value)))
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return url + sep + urllib.parse.urlencode(pairs)


class StdlibClient:
    """A minimal, synchronous ``httpx.Client`` look-alike on ``urllib``.

    Redirects are returned, not followed, unless ``follow_redirects=True``.
    Cookies set by any response are sent on later requests (the login form
    needs its session cookie). Only ``https`` URLs are allowed unless
    ``allow_http=True`` (tests against a local fake server).
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        allowed_hosts: Iterable[str] | None = None,
        allow_http: bool = False,
        max_body: int = _MAX_BODY,
    ) -> None:
        self.timeout = timeout
        self.allowed_hosts = {h.lower() for h in allowed_hosts} if allowed_hosts is not None else None
        self.allow_http = allow_http
        self.max_body = max_body
        self.cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirects())

    def _check(self, url: str) -> None:
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise TransportError(f"not a valid URL: {url!r}") from exc
        if parts.scheme != "https" and not (self.allow_http and parts.scheme == "http"):
            raise TransportError(f"refusing a non-https URL ({parts.scheme or 'no scheme'})")
        host = (parts.hostname or "").lower()
        if parts.username or parts.password:
            raise TransportError("refusing a URL with credentials in it")
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise TransportError(f"refusing to contact {host or 'an unnamed host'}: not an allowed host")

    def _once(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Response:
        self._check(url)
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as err:  # 3xx (not followed), 4xx, 5xx: still a response
            try:
                content = err.read(self.max_body + 1) if err.fp is not None else b""
            except OSError:
                content = b""
            finally:
                err.close()
            if len(content) > self.max_body:
                raise TransportError("the response was too large")
            return Response(err.code, Headers(err.headers), content, url)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(str(reason) or exc.__class__.__name__) from exc
        with resp:
            try:
                content = resp.read(self.max_body + 1)
            except (socket.timeout, TimeoutError, OSError) as exc:
                raise TransportError(f"reading the response failed: {exc}") from exc
            if len(content) > self.max_body:
                raise TransportError("the response was too large")
            return Response(resp.status, Headers(resp.headers), content, url)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> Response:
        method = method.upper()
        sent = dict(headers or {})
        body: bytes | None = None
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urllib.parse.urlencode({k: _query_value(v) for k, v in data.items()}).encode("utf-8")
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif method in ("POST", "PUT", "PATCH"):
            body = b""
        target = _with_params(url, params)
        resp = self._once(method, target, body, sent)
        hops = 0
        while follow_redirects and resp.status_code in _REDIRECTS and "location" in resp.headers:
            hops += 1
            if hops > _MAX_HOPS:
                raise TransportError("too many redirects")
            target = urllib.parse.urljoin(target, resp.headers["location"])
            if resp.status_code in (307, 308):
                resp = self._once(method, target, body, sent)
            else:
                resp = self._once("GET", target, None, {k: v for k, v in sent.items() if k.lower() != "content-type"})
        return resp

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self.cookies.clear()

    def __enter__(self) -> "StdlibClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def default_client(timeout: float) -> Any:
    """``httpx.Client`` when installed (unchanged behaviour), :class:`StdlibClient` otherwise."""
    if _httpx is not None:
        return _httpx.Client(timeout=timeout)
    return StdlibClient(timeout=timeout)


__all__ = ["Headers", "Response", "StdlibClient", "TRANSPORT_ERRORS", "TransportError", "default_client", "have_httpx", "require_httpx"]

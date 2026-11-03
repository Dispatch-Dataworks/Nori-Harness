"""Logging in to Skylight.

Skylight has no API keys. Its apps use OAuth2 authorization-code + PKCE
against a server-rendered Rails login page:

1. ``GET /oauth/authorize?...`` redirects to the login form (which carries a
   CSRF ``authenticity_token`` and sets a session cookie).
2. ``POST /auth/session`` with the token, email and password. On success the
   server bounces back through ``/oauth/authorize`` and finally redirects to
   ``https://ourskylight.com/welcome?code=...&state=...``.
3. ``POST /oauth/token`` exchanges the code (plus the PKCE verifier) for an
   access token and a refresh token.

:func:`login` / :func:`async_login` drive that headlessly from an email and
password. :func:`refresh` / :func:`async_refresh` use the refresh grant.
For environments where you would rather not hand over a password (e.g. a
Home Assistant config flow) :class:`BrowserLogin` builds the authorize URL
for a real browser and exchanges the pasted-back code.
"""

from __future__ import annotations

import base64
import hashlib
import platform
import re
import secrets
import uuid
from dataclasses import dataclass, field

from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from ._http import TRANSPORT_ERRORS, default_client, require_httpx

from .const import (
    BASE_URL,
    BROWSER_USER_AGENT,
    DEFAULT_TIMEOUT,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    WEB_APP_URL,
)
from .errors import SkylightAuthError, SkylightError
from .tokens import Credentials

if TYPE_CHECKING:
    import httpx

_CSRF_INPUT = re.compile(r'name=["\']authenticity_token["\'][^>]*value=["\']([^"\']+)["\']', re.I)
_CSRF_INPUT_REV = re.compile(r'value=["\']([^"\']+)["\'][^>]*name=["\']authenticity_token["\']', re.I)
_CSRF_META = re.compile(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_CSRF_META_REV = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', re.I)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 10

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": BROWSER_USER_AGENT,
    "Referer": f"{WEB_APP_URL}/",
}
_TOKEN_HEADERS = {
    "Accept": "application/json, text/javascript; q=0.01",
    "User-Agent": BROWSER_USER_AGENT,
    "Origin": WEB_APP_URL,
    "Referer": f"{WEB_APP_URL}/",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """``(verifier, S256 challenge)`` per RFC 7636."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def extract_csrf_token(html: str) -> str | None:
    """The Rails ``authenticity_token`` from a login page (form input or meta tag)."""
    for pattern in (_CSRF_INPUT, _CSRF_INPUT_REV, _CSRF_META, _CSRF_META_REV):
        m = pattern.search(html)
        if m:
            return m.group(1)
    return None


def extract_code(raw: str) -> str | None:
    """Accept a bare code, ``code=...`` or a full callback URL and return the code."""
    raw = raw.strip()
    if not raw:
        return None
    if "://" in raw or raw.startswith("/") or raw.startswith("?"):
        try:
            query = urlparse(raw).query or raw.lstrip("?")
            return parse_qs(query).get("code", [None])[0]
        except ValueError:
            return None
    if raw.lower().startswith("code="):
        return raw.split("=", 1)[1]
    return raw


def device_metadata(fingerprint: str, *, name: str = "skylight-calendar") -> dict[str, str]:
    """Extra form fields the web app sends with token requests."""
    return {
        "skylight_api_client_device_fingerprint": fingerprint,
        "skylight_api_client_device_platform": "web",
        "skylight_api_client_device_name": name,
        "skylight_api_client_device_os_version": platform.release() or "unknown",
        "skylight_api_client_device_app_version": "unknown",
        "skylight_api_client_device_hardware": platform.system() or "unknown",
    }


@dataclass
class _Flow:
    """The pure parts of the login flow; the drivers below do the I/O."""

    base_url: str = BASE_URL
    redirect_uri: str = OAUTH_REDIRECT_URI
    client_id: str = OAUTH_CLIENT_ID
    scope: str = OAUTH_SCOPE
    verifier: str = ""
    challenge: str = ""
    state: str = field(default_factory=lambda: _b64url(secrets.token_bytes(12)))
    device_fingerprint: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.verifier:
            self.verifier, self.challenge = pkce_pair()

    def authorize_url(self, *, prompt_login: bool = True) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": self.scope,
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
        }
        if prompt_login:
            params["prompt"] = "login"
        return f"{self.base_url}/oauth/authorize?{urlencode(params)}"

    @property
    def session_url(self) -> str:
        return f"{self.base_url}/auth/session"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/oauth/token"

    def is_callback(self, location: str) -> bool:
        if location.startswith(self.redirect_uri):
            return True
        # The mobile apps use a custom scheme; accept it too.
        return location.startswith("skylight-family:")

    def absolute(self, location: str) -> str:
        return urljoin(self.base_url + "/", location)

    def parse_callback(self, location: str, *, check_state: bool = True) -> str:
        query = parse_qs(urlparse(location).query)
        if check_state and query.get("state", [None])[0] != self.state:
            raise SkylightAuthError("OAuth state mismatch; refusing the authorization code")
        code = query.get("code", [None])[0]
        if not code:
            error = query.get("error_description", query.get("error", ["no code in callback"]))[0]
            raise SkylightAuthError(f"Skylight did not return an authorization code: {error}")
        return code

    def login_form(self, csrf: str, email: str, password: str) -> dict[str, str]:
        return {"authenticity_token": csrf, "email": email, "password": password}

    def token_form(self, code: str) -> dict[str, str]:
        return {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "scope": self.scope,
            "redirect_uri": self.redirect_uri,
            "code": code,
            "code_verifier": self.verifier,
            **device_metadata(self.device_fingerprint),
        }


def _refresh_form(refresh_token: str, device_fingerprint: str) -> dict[str, str]:
    return {
        "grant_type": "refresh_token",
        "client_id": OAUTH_CLIENT_ID,
        "scope": OAUTH_SCOPE,
        "refresh_token": refresh_token,
        **device_metadata(device_fingerprint),
    }


def _credentials_from(resp: httpx.Response, fingerprint: str, what: str) -> Credentials:
    if resp.status_code >= 400:
        snippet = resp.text[:200].strip()
        raise SkylightAuthError(f"{what} failed (HTTP {resp.status_code}){': ' + snippet if snippet else ''}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SkylightAuthError(f"{what}: response was not JSON") from exc
    try:
        return Credentials.from_token_response(payload, device_fingerprint=fingerprint)
    except ValueError as exc:
        raise SkylightAuthError(f"{what}: {exc}") from exc


def _bad_credentials(resp: httpx.Response) -> SkylightAuthError:
    if resp.status_code in (401, 422) or resp.status_code == 200:
        return SkylightAuthError("Skylight rejected the email or password")
    return SkylightAuthError(f"Unexpected response from the Skylight login form (HTTP {resp.status_code})")


# ---------------------------------------------------------------------------
# Sync driver
# ---------------------------------------------------------------------------


def login(
    email: str,
    password: str,
    *,
    base_url: str = BASE_URL,
    http: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    device_fingerprint: str | None = None,
) -> Credentials:
    """Log in with an email and password and return :class:`Credentials`."""
    flow = _Flow(base_url=base_url, device_fingerprint=device_fingerprint or uuid.uuid4().hex)
    owns = http is None
    client = http or default_client(timeout)
    try:
        try:
            # 1. authorize -> login form
            resp = client.get(flow.authorize_url(), headers=_HTML_HEADERS, follow_redirects=False)
            for _ in range(_MAX_HOPS):
                if resp.status_code not in _REDIRECT_STATUSES:
                    break
                resp = client.get(flow.absolute(resp.headers["location"]), headers=_HTML_HEADERS, follow_redirects=False)
            if resp.status_code != 200:
                raise SkylightAuthError(f"Could not load the Skylight login page (HTTP {resp.status_code})")
            csrf = extract_csrf_token(resp.text)
            if not csrf:
                raise SkylightAuthError("Could not find the CSRF token on the Skylight login page")

            # 2. submit credentials, chase redirects to the callback
            resp = client.post(
                flow.session_url,
                data=flow.login_form(csrf, email, password),
                headers={**_HTML_HEADERS, "Origin": flow.base_url, "Referer": f"{flow.base_url}/auth/session/new"},
                follow_redirects=False,
            )
            location: str | None = None
            for _ in range(_MAX_HOPS):
                if resp.status_code not in _REDIRECT_STATUSES:
                    raise _bad_credentials(resp)
                location = resp.headers.get("location", "")
                if flow.is_callback(location):
                    break
                resp = client.get(flow.absolute(location), headers=_HTML_HEADERS, follow_redirects=False)
            else:
                raise SkylightAuthError("Too many redirects during Skylight login")
            code = flow.parse_callback(location or "")

            # 3. exchange the code
            resp = client.post(flow.token_url, data=flow.token_form(code), headers=_TOKEN_HEADERS)
        except TRANSPORT_ERRORS as exc:
            raise SkylightError(f"Network error during Skylight login: {exc}") from exc
        return _credentials_from(resp, flow.device_fingerprint, "Token exchange")
    finally:
        if owns:
            client.close()


def refresh(
    credentials: Credentials,
    *,
    base_url: str = BASE_URL,
    http: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Credentials:
    """Trade a refresh token for new credentials. Refresh tokens rotate on use."""
    if not credentials.refresh_token:
        raise SkylightAuthError("No refresh token available; log in again")
    owns = http is None
    client = http or default_client(timeout)
    try:
        try:
            resp = client.post(
                f"{base_url.rstrip('/')}/oauth/token",
                data=_refresh_form(credentials.refresh_token, credentials.device_fingerprint),
                headers=_TOKEN_HEADERS,
            )
        except TRANSPORT_ERRORS as exc:
            raise SkylightError(f"Network error during token refresh: {exc}") from exc
        fresh = _credentials_from(resp, credentials.device_fingerprint, "Token refresh")
        if not fresh.refresh_token:
            fresh.refresh_token = credentials.refresh_token
        return fresh
    finally:
        if owns:
            client.close()


# ---------------------------------------------------------------------------
# Async driver
# ---------------------------------------------------------------------------


async def async_login(
    email: str,
    password: str,
    *,
    base_url: str = BASE_URL,
    http: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    device_fingerprint: str | None = None,
) -> Credentials:
    """Async twin of :func:`login`."""
    flow = _Flow(base_url=base_url, device_fingerprint=device_fingerprint or uuid.uuid4().hex)
    owns = http is None
    client = http or require_httpx("async login").AsyncClient(timeout=timeout)
    try:
        try:
            resp = await client.get(flow.authorize_url(), headers=_HTML_HEADERS, follow_redirects=False)
            for _ in range(_MAX_HOPS):
                if resp.status_code not in _REDIRECT_STATUSES:
                    break
                resp = await client.get(flow.absolute(resp.headers["location"]), headers=_HTML_HEADERS, follow_redirects=False)
            if resp.status_code != 200:
                raise SkylightAuthError(f"Could not load the Skylight login page (HTTP {resp.status_code})")
            csrf = extract_csrf_token(resp.text)
            if not csrf:
                raise SkylightAuthError("Could not find the CSRF token on the Skylight login page")

            resp = await client.post(
                flow.session_url,
                data=flow.login_form(csrf, email, password),
                headers={**_HTML_HEADERS, "Origin": flow.base_url, "Referer": f"{flow.base_url}/auth/session/new"},
                follow_redirects=False,
            )
            location: str | None = None
            for _ in range(_MAX_HOPS):
                if resp.status_code not in _REDIRECT_STATUSES:
                    raise _bad_credentials(resp)
                location = resp.headers.get("location", "")
                if flow.is_callback(location):
                    break
                resp = await client.get(flow.absolute(location), headers=_HTML_HEADERS, follow_redirects=False)
            else:
                raise SkylightAuthError("Too many redirects during Skylight login")
            code = flow.parse_callback(location or "")

            resp = await client.post(flow.token_url, data=flow.token_form(code), headers=_TOKEN_HEADERS)
        except TRANSPORT_ERRORS as exc:
            raise SkylightError(f"Network error during Skylight login: {exc}") from exc
        return _credentials_from(resp, flow.device_fingerprint, "Token exchange")
    finally:
        if owns:
            await client.aclose()


async def async_refresh(
    credentials: Credentials,
    *,
    base_url: str = BASE_URL,
    http: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Credentials:
    """Async twin of :func:`refresh`."""
    if not credentials.refresh_token:
        raise SkylightAuthError("No refresh token available; log in again")
    owns = http is None
    client = http or require_httpx("async login").AsyncClient(timeout=timeout)
    try:
        try:
            resp = await client.post(
                f"{base_url.rstrip('/')}/oauth/token",
                data=_refresh_form(credentials.refresh_token, credentials.device_fingerprint),
                headers=_TOKEN_HEADERS,
            )
        except TRANSPORT_ERRORS as exc:
            raise SkylightError(f"Network error during token refresh: {exc}") from exc
        fresh = _credentials_from(resp, credentials.device_fingerprint, "Token refresh")
        if not fresh.refresh_token:
            fresh.refresh_token = credentials.refresh_token
        return fresh
    finally:
        if owns:
            await client.aclose()


# ---------------------------------------------------------------------------
# Browser-assisted flow (no password handling in your code)
# ---------------------------------------------------------------------------


class BrowserLogin:
    """Send a person to Skylight's login page and exchange what they paste back.

    Skylight's OAuth client only allows one redirect URI, which belongs to the
    web app, so the browser lands on ``https://ourskylight.com/welcome?code=...``
    and the person copies that URL (or just the code) back to you.

    >>> flow = BrowserLogin()
    >>> print(flow.url)                      # open this in a browser
    >>> creds = flow.exchange(input("Paste the URL you landed on: "))
    """

    def __init__(self, *, base_url: str = BASE_URL) -> None:
        self._flow = _Flow(base_url=base_url)

    @property
    def url(self) -> str:
        return self._flow.authorize_url()

    @property
    def state(self) -> str:
        return self._flow.state

    @property
    def device_fingerprint(self) -> str:
        return self._flow.device_fingerprint

    def _code(self, pasted: str) -> str:
        if "://" in pasted:
            # A full callback URL carries the state; verify it.
            return self._flow.parse_callback(pasted.strip())
        code = extract_code(pasted)
        if not code:
            raise SkylightAuthError("No authorization code found in the pasted text")
        return code

    def exchange(self, pasted: str, *, http: httpx.Client | None = None, timeout: float = DEFAULT_TIMEOUT) -> Credentials:
        code = self._code(pasted)
        owns = http is None
        client = http or default_client(timeout)
        try:
            try:
                resp = client.post(self._flow.token_url, data=self._flow.token_form(code), headers=_TOKEN_HEADERS)
            except TRANSPORT_ERRORS as exc:
                raise SkylightError(f"Network error during token exchange: {exc}") from exc
            return _credentials_from(resp, self._flow.device_fingerprint, "Token exchange")
        finally:
            if owns:
                client.close()

    async def async_exchange(
        self, pasted: str, *, http: httpx.AsyncClient | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> Credentials:
        code = self._code(pasted)
        owns = http is None
        client = http or require_httpx("async login").AsyncClient(timeout=timeout)
        try:
            try:
                resp = await client.post(self._flow.token_url, data=self._flow.token_form(code), headers=_TOKEN_HEADERS)
            except TRANSPORT_ERRORS as exc:
                raise SkylightError(f"Network error during token exchange: {exc}") from exc
            return _credentials_from(resp, self._flow.device_fingerprint, "Token exchange")
        finally:
            if owns:
                await client.aclose()


__all__: list[str] = [
    "BrowserLogin",
    "async_login",
    "async_refresh",
    "device_metadata",
    "extract_code",
    "extract_csrf_token",
    "login",
    "pkce_pair",
    "refresh",
]


